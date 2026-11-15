"""The single network client. The only module in hera allowed to import a network library.

Fail closed: any exception in the decision path ends in a "blocked" record, never a send.
Blocked results carry fixed notices that never include segment content or matched terms.
"""
from __future__ import annotations
import ipaddress
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence
from ..types import Label
from . import translog
from .gate import LOCAL, permitted_destinations
from .grants import GrantStore
from .labels import LabelStore, content_hash


@dataclass(frozen=True)
class Segment:
    artefact_id: str
    data: bytes


@dataclass(frozen=True)
class Destination:
    name: str
    url: str
    headers: dict = field(default_factory=dict, repr=False)   # holds the API key; never log
    local: bool = False       # loopback endpoint on this machine; see is_loopback_url


def is_loopback_url(url: str) -> bool:
    """True only if the URL is http(s) and its host is literally 127.0.0.0/8, ::1 or localhost."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port                                   # raises ValueError on a malformed port
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback and (ip.version == 6 or ip in ipaddress.ip_network("127.0.0.0/8"))


@dataclass(frozen=True)
class SendResult:
    sent: bool
    reason: str = ""
    status: int | None = None
    body: bytes = b""


class _Blocked(Exception):
    def __init__(self, reason: str, scanner: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.scanner = scanner or {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):      # a redirect would leave the approved host
        return None


Scanner = Callable[[bytes], list]
Blocking = Callable[[list], list]      # all findings of one payload -> the ones that block
Envelope = Callable[[Sequence[Segment]], bytes]


class Gateway:
    def __init__(self, conn, label_store: LabelStore, grant_store: GrantStore,
                 destinations: dict[str, Destination], *, max_payload_bytes: int = 2_000_000,
                 scanners: Iterable[Scanner] = (), blocking: Blocking = list,
                 clock: Callable[[], float] = time.time,
                 timeout: float = 60) -> None:
        self.conn = conn
        self.labels = label_store
        self.grants = grant_store
        self.destinations = dict(destinations)
        for d in self.destinations.values():
            if d.local and not is_loopback_url(d.url):
                raise ValueError(f"destination {d.name!r} is marked local but its URL is not loopback")
            if (not d.local and d.url.lower().startswith("http://")
                    and not is_loopback_url(d.url)):
                # a key or payload must never travel in cleartext to another machine
                raise ValueError(f"destination {d.name!r} is remote and must use https")
        self.max_payload_bytes = max_payload_bytes
        self.scanners = tuple(scanners)
        self.blocking = blocking          # default: every finding blocks
        self.clock = clock
        self.timeout = timeout

    def send(self, destination_name: str, segments: Sequence[Segment], *, purpose: str,
             task_id: str | None = None, envelope: Envelope | None = None) -> SendResult:
        artefacts: list[dict] = []
        grant_ids: list[str] = []
        joined = b""
        scanner_record: dict = {}
        try:
            segments = list(segments)
            joined = b"\n".join(s.data for s in segments)
            payload = self._decide(destination_name, segments, envelope, artefacts, grant_ids,
                                   scanner_record)
            local = self.destinations[destination_name].local
            self._log("sent", destination_name, purpose, task_id, artefacts, grant_ids,
                      scanner_record, "loopback" if local else "", payload)
        except _Blocked as b:
            return self._block(b.reason, destination_name, purpose, task_id, artefacts,
                               grant_ids, b.scanner, joined)
        except Exception:
            return self._block("internal error", destination_name, purpose, task_id, artefacts,
                               grant_ids, {}, joined)
        return self._post(self.destinations[destination_name], payload)

    # -- decision path ---------------------------------------------------------------
    def _decide(self, name: str, segments: list[Segment], envelope: Envelope | None,
                artefacts: list[dict], grant_ids: list[str], scanner_record: dict) -> bytes:
        if name == LOCAL:
            raise _Blocked("local is not a network destination")
        dest = self.destinations.get(name)
        if dest is None:
            raise _Blocked("unknown destination")
        if not segments:
            raise _Blocked("no labelled segments")
        if dest.local:
            return self._decide_local(dest, segments, envelope, artefacts)
        labels: list[Label] = []
        missing = changed = False
        for seg in segments:
            label = self.labels.get(seg.artefact_id)
            cls = label.repo_class.value if label and label.repo_class else None
            artefacts.append({"id": seg.artefact_id, "repo_class": cls})
            if label is None:
                missing = True
            else:
                labels.append(label)
                if content_hash(seg.data) != label.content_hash:
                    changed = True
        if missing:
            raise _Blocked("unlabelled segment")
        if changed:
            raise _Blocked("content changed since labelling")
        found: dict[str, list[str]] = {}
        for scanner in self.scanners:
            for seg in segments:
                try:
                    names = [str(n) for n in scanner(seg.data)]
                except Exception:
                    raise _Blocked("scanner error") from None
                if names:
                    found.setdefault(seg.artefact_id, []).extend(names)
        if found:
            everything = [n for names in found.values() for n in names]
            blocking = [str(n) for n in self.blocking(everything)]
            if blocking:
                reason = ("held for user review" if all(n.startswith("term:review") for n in blocking)
                          else "restricted data finding")
                raise _Blocked(reason, {"findings": found, "blocking": sorted(set(blocking))})
            scanner_record["findings"] = found       # recorded, did not block on their own
        if sum(len(s.data) for s in segments) > self.max_payload_bytes:
            raise _Blocked("oversize payload")
        now = self.clock()
        grants = self.grants.all()
        allowed = permitted_destinations(labels, grants, set(self.destinations), now)
        hashes = {l.content_hash for l in labels}
        grant_ids.extend(g.grant_id for g in grants
                         if g.expires_at > now and name in g.destinations and g.content_hashes & hashes)
        if name not in allowed:
            raise _Blocked("destination not permitted for these labels")
        if not dest.url.lower().startswith(("http://", "https://")):
            raise _Blocked("unsupported destination url")
        try:
            payload = envelope(segments) if envelope else b"\n".join(s.data for s in segments)
        except Exception:
            raise _Blocked("envelope error") from None
        if not isinstance(payload, bytes):
            raise _Blocked("envelope error")
        if len(payload) > self.max_payload_bytes:
            raise _Blocked("oversize payload")
        return payload

    def _decide_local(self, dest: Destination, segments: list[Segment], envelope: Envelope | None,
                      artefacts: list[dict]) -> bytes:
        """Loopback: data stays on this machine, so no label, grant, scanner or gate check.
        Size limit and the no-redirect rule still apply; the record notes reason "loopback"."""
        artefacts.extend({"id": s.artefact_id} for s in segments)
        if sum(len(s.data) for s in segments) > self.max_payload_bytes:
            raise _Blocked("oversize payload")
        try:
            payload = envelope(segments) if envelope else b"\n".join(s.data for s in segments)
        except Exception:
            raise _Blocked("envelope error") from None
        if not isinstance(payload, bytes):
            raise _Blocked("envelope error")
        if len(payload) > self.max_payload_bytes:
            raise _Blocked("oversize payload")
        return payload

    def _log(self, decision: str, name: str, purpose: str, task_id, artefacts, grant_ids,
             scanner: dict, reason: str, payload: bytes) -> None:
        translog.append(self.conn, destination=name, purpose=purpose, decision=decision,
                        payload=payload, task_id=task_id, artefacts=artefacts,
                        grant_ids=grant_ids, scanner=scanner, reason=reason, at=self.clock())

    def _block(self, reason: str, name: str, purpose: str, task_id, artefacts, grant_ids,
               scanner: dict, payload: bytes) -> SendResult:
        try:
            self._log("blocked", name, purpose, task_id, artefacts, grant_ids, scanner, reason, payload)
        except Exception:
            pass          # still blocked; nothing was sent
        return SendResult(sent=False, reason=reason)

    # -- the only network call -------------------------------------------------------
    def _post(self, dest: Destination, payload: bytes) -> SendResult:
        req = urllib.request.Request(dest.url, data=payload, method="POST",
                                     headers=dict(dest.headers))
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return SendResult(sent=True, status=resp.status, body=resp.read())
        except urllib.error.HTTPError as e:
            return SendResult(sent=True, status=e.code, body=e.read())
        except Exception as e:
            return SendResult(sent=False, reason=f"transport error: {type(e).__name__}")
