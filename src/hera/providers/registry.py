"""Build providers from the ``[providers.<name>]`` sections of hera.toml.

The API key is read from the environment variable named by ``key_env`` and goes only into the
Destination's headers (which are never logged and are hidden from ``repr``). A provider whose key
variable is missing is left out and the reason is reported; no message here ever contains a key
value."""
from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlsplit

from .. import paths
from ..config import Config, ProviderSpec
from ..ledger import Ledger
from ..modelhost import LOOPBACK, HostSettings, ModelHost
from ..privacy.gateway import Destination, Gateway
from .anthropic import AnthropicProvider
from .base import BaseProvider
from .grok import GROK_CHAT_URL, GrokProvider
from .openai_compat import OpenAICompatProvider

_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "x-goog-api-key",
                                 "proxy-authorization", "cookie"})


HOST_ABSENT_KEY = "[local_model].command"


class ProviderSet(dict):
    """name -> provider, plus ``absent``: name -> reason a configured provider is not available, plus
    ``host``: the supervised local model host (or None) that local providers share."""
    def __init__(self) -> None:
        super().__init__()
        self.absent: dict[str, str] = {}
        self.host: ModelHost | None = None


def url_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def effective_url(spec: ProviderSpec) -> str:
    return spec.url or (GROK_CHAT_URL if spec.kind == "grok" else "")


def is_host_url(url: str, port: int) -> bool:
    """Is ``url`` exactly http://127.0.0.1:<port>/...? (scheme http, host literally 127.0.0.1, same port)"""
    try:
        parts = urlsplit(url)
        return (parts.scheme == "http" and parts.hostname == LOOPBACK and parts.port == port)
    except ValueError:
        return False


def host_attachments(config: Config) -> list[str]:
    """Names of the providers a supervised host would attach to: tier ``local``, kind
    ``openai_compat``, URL http://127.0.0.1:<[local_model].port>. Empty when no command is set."""
    settings = HostSettings.from_config(config)
    if not settings.command:
        return []
    return sorted(n for n, spec in config.providers.items()
                  if n != "local" and spec.tier == "local" and spec.kind == "openai_compat"
                  and is_host_url(effective_url(spec), settings.port))


def key_present(spec: ProviderSpec, env: Mapping[str, str]) -> bool | None:
    """True/False for a provider that needs a key, None for one that needs none (local)."""
    if spec.local or not spec.key_env:
        return None
    return bool(env.get(spec.key_env))


def _headers(spec: ProviderSpec, env: Mapping[str, str]) -> dict:
    headers = {"content-type": "application/json", **spec.extra_headers}
    key = env.get(spec.key_env, "") if spec.key_env else ""
    if key:
        if spec.kind == "anthropic":
            headers["x-api-key"] = key
        else:
            headers["Authorization"] = "Bearer " + key
    return headers


def _problem(spec: ProviderSpec, env: Mapping[str, str], tiers) -> str:
    """Why this provider cannot be built, or "" if it can. Never contains a key value."""
    if not effective_url(spec):
        return "no url configured"
    if not spec.model:
        return "no model id configured"
    if spec.tier not in tiers:
        return f"tier {spec.tier!r} has no price in the configuration"
    if {k.lower() for k in spec.extra_headers} & _CREDENTIAL_HEADERS:
        return "extra_headers must not carry credentials; use key_env"
    if not spec.local:
        if not spec.key_env:
            return "no key_env configured for a remote provider"
        if not env.get(spec.key_env):
            return f"environment variable {spec.key_env} is not set"
    return ""


def build_providers(config: Config, gateway: Gateway, ledger: Ledger,
                    env: Mapping[str, str] = os.environ) -> ProviderSet:
    """Register a destination for every usable provider on ``gateway`` and construct the provider.
    Providers that cannot be built are listed in ``result.absent`` with a reason."""
    out = ProviderSet()
    settings = HostSettings.from_config(config)
    attach = set(host_attachments(config))
    if settings.command:
        out.host = ModelHost(settings, home=paths.hera_home())
        if not attach:
            out.absent[HOST_ABSENT_KEY] = (f"a command is set but no provider of tier 'local' points at "
                                           f"http://{LOOPBACK}:{settings.port}; the host would never be used")
    for name, spec in config.providers.items():
        # "local" is the gateway's reserved name for "never leaves this process"; a provider called
        # that would be refused on every call, which looks like a privacy block. Say so up front.
        why = ("the name 'local' is reserved; call the provider something else, e.g. 'local-llm'"
               if name == "local" else _problem(spec, env, config.prices))
        if not why:
            try:
                dest = Destination(name, effective_url(spec), _headers(spec, env), local=spec.local)
                gateway.add_destination(dest)
                args = (name, spec.tier, spec.model, gateway, ledger)
                if spec.kind == "anthropic":
                    out[name] = AnthropicProvider(*args)
                elif spec.kind == "grok":
                    out[name] = GrokProvider(*args)
                else:
                    out[name] = OpenAICompatProvider(*args, send_store_false=spec.send_store_false,
                                                     host=out.host if name in attach else None)
            except ValueError as e:        # these messages come from our own checks and name no key
                gateway.destinations.pop(name, None)
                why = str(e)
        if why:
            out.absent[name] = why
    return out
