"""Providers from configuration: construction, missing keys, and the `hera providers` commands."""
from pathlib import Path

import pytest

from hera import cli, config, paths
from hera.config import Budget, Config, ProviderSpec, TierPrice
from hera.ledger import Ledger
from hera.privacy.gateway import Gateway
from hera.privacy.grants import GrantStore
from hera.privacy.labels import LabelStore, label_builtin
from hera.providers import AnthropicProvider, GrokProvider, Message, OpenAICompatProvider, build_providers
from scripted_llm import ScriptedLLM, step

SECRET = "sk-invented-" + "registry-test-key-0042"
PRICES = {"cheap": TierPrice(1.0, 4.0), "default": TierPrice(2.0, 10.0), "local": TierPrice(0.0, 0.0)}


def cfg(**specs) -> Config:
    return Config(Budget(), PRICES, "", {}, {}, {n: ProviderSpec(name=n, **kw) for n, kw in specs.items()})


@pytest.fixture
def parts(conn):
    gw = Gateway(conn, LabelStore(conn), GrantStore(conn), {})
    return gw, Ledger(conn, Budget(), PRICES)


def test_example_config_has_a_default_provider_but_no_model(tmp_path):
    c = config.load(Path(__file__).resolve().parents[1] / "config" / "hera.example.toml")
    s = c.providers["default"]
    assert (s.kind, s.key_env, s.tier, s.model) == ("anthropic", "ANTHROPIC_API_KEY", "default", "")
    assert s.extra_headers == {"anthropic-version": "2023-06-01"} and not s.local
    assert list(c.providers) == ["default"]            # the other examples are commented out


def test_config_rejects_unknown_or_malformed_provider_settings(tmp_path):
    def load(body):
        p = tmp_path / "c.toml"
        p.write_text(body)
        return config.load(p)
    assert load("[providers.a]\nkind='grok'\nmodel='m'\n").providers["a"].kind == "grok"
    for body in ("[providers.a]\nkind='nope'\n", "[providers.a]\nkind='grok'\nmodle='m'\n",
                 "[providers.a]\nkind='grok'\nextra_headers={x=1}\n", "[providers]\na=1\n",
                 "[providers.a]\nkind='grok'\nurl=5\n"):
        with pytest.raises(ValueError):
            load(body)


def test_builds_each_kind_and_puts_the_key_only_in_destination_headers(parts):
    gw, ledger = parts
    env = {"AK": SECRET, "OK": SECRET + "b", "XK": SECRET + "c"}
    got = build_providers(cfg(
        a=dict(kind="anthropic", url="https://api.example.invalid/v1/messages", model="m1", key_env="AK",
               extra_headers={"anthropic-version": "2023-06-01"}),
        o=dict(kind="openai_compat", url="https://api.example.invalid/v1/chat/completions", model="m2",
               key_env="OK", tier="cheap", send_store_false=True),
        g=dict(kind="grok", model="m3", key_env="XK", tier="cheap"),
        l=dict(kind="openai_compat", url="http://127.0.0.1:9/v1/chat/completions", model="m4", local=True,
               tier="local")), gw, ledger, env)
    assert got.absent == {}
    assert isinstance(got["a"], AnthropicProvider) and isinstance(got["o"], OpenAICompatProvider)
    assert isinstance(got["g"], GrokProvider) and got["l"].tier == "local"
    assert gw.destinations["a"].headers == {"content-type": "application/json", "anthropic-version": "2023-06-01",
                                            "x-api-key": SECRET}
    assert gw.destinations["o"].headers["Authorization"] == "Bearer " + SECRET + "b"
    assert gw.destinations["g"].url == "https://api.x.ai/v1/chat/completions"
    assert gw.destinations["l"].local and "Authorization" not in gw.destinations["l"].headers
    assert got["o"].send_store_false is True and got["a"].model_id == "m1"
    for d in gw.destinations.values():
        assert SECRET not in repr(d)


def test_missing_key_variable_means_absent_with_a_reason_that_holds_no_key(parts):
    gw, ledger = parts
    env = {"OTHER": SECRET, "EMPTY": ""}
    got = build_providers(cfg(
        a=dict(kind="anthropic", url="https://x.example.invalid/v1/messages", model="m", key_env="NOPE"),
        b=dict(kind="grok", model="m", key_env="EMPTY", tier="cheap"),
        c=dict(kind="grok", model="m", tier="cheap"),
        d=dict(kind="anthropic", url="https://x.example.invalid/v1/messages", key_env="OTHER"),
        e=dict(kind="grok", model="m", key_env="OTHER", tier="nonexistent"),
        f=dict(kind="openai_compat", model="m", tier="cheap", key_env="OTHER"),
        g=dict(kind="openai_compat", url="http://api.example.invalid/v1/x", model="m", key_env="OTHER"),
        h=dict(kind="openai_compat", url="https://api.example.invalid/v1/x", model="m", local=True, tier="local"),
        i=dict(kind="openai_compat", url="http://127.0.0.1:9/v1/x", model="m", local=True, tier="local",
               key_env="OTHER"),
        j=dict(kind="grok", model="m", key_env="OTHER", tier="cheap",
               extra_headers={"Authorization": "Bearer " + SECRET})), gw, ledger, env)
    assert set(got) == {"i"}
    assert got.absent["a"] == "environment variable NOPE is not set"
    assert got.absent["b"] == "environment variable EMPTY is not set"
    assert "key_env" in got.absent["c"]
    assert got.absent["d"] == "no model id configured"
    assert "nonexistent" in got.absent["e"] and got.absent["f"] == "no url configured"
    assert "https" in got.absent["g"] and "loopback" in got.absent["h"]
    assert "credentials" in got.absent["j"]
    assert all(SECRET not in r for r in got.absent.values())
    assert not (set(gw.destinations) - {"i"})                 # absent providers leave no destination


def test_local_tier_must_match_a_loopback_destination(parts):
    gw, ledger = parts
    got = build_providers(cfg(
        x=dict(kind="openai_compat", url="http://127.0.0.1:9/v1/x", model="m", local=True, tier="cheap"),
        y=dict(kind="openai_compat", url="https://api.example.invalid/v1/x", model="m", key_env="K",
               tier="local")), gw, ledger, {"K": SECRET})
    assert set(got) == set() and "local" in got.absent["x"] and "local" in got.absent["y"]
    assert gw.destinations == {}


def test_a_built_provider_completes_through_the_gateway(conn, parts):
    llm = ScriptedLLM("openai", [step("ready", usage=(20, 2))])
    try:
        gw, ledger = parts
        got = build_providers(cfg(p=dict(kind="openai_compat", url=llm.url, model="m", key_env="K", tier="cheap")),
                              gw, ledger, {"K": SECRET})
        aid = "x"
        label_builtin(gw.labels, artefact_id=aid, data=b"hi")
        reply = got["p"].complete([Message("user", aid, b"hi")], max_tokens=5, task_id="t")
        assert reply.text == "ready" and llm.headers[0]["authorization"] == "Bearer " + SECRET
        assert ledger.spent_task("t") == pytest.approx((20 * 1.0 + 2 * 4.0) / 1e6)
    finally:
        llm.close()


# ---- the CLI ------------------------------------------------------------------------------------------
def write_home(tmp_path, monkeypatch, providers_toml: str):
    monkeypatch.setenv("HERA_HOME", str(tmp_path / "h"))
    assert cli.main(["init"]) == 0
    paths.config_path().write_text(
        "[prices.tiers.cheap]\ninput = 1.0\noutput = 4.0\n[prices.tiers.default]\ninput = 2.0\noutput = 10.0\n"
        "[prices.tiers.local]\ninput = 0.0\noutput = 0.0\n" + providers_toml, encoding="utf-8")


def test_providers_list_shows_status_and_never_a_key(tmp_path, monkeypatch, capsys):
    write_home(tmp_path, monkeypatch, f"""
[providers.cloud]
kind = "anthropic"
url = "https://api.example.invalid/v1/messages"
model = "m"
key_env = "CLOUD_KEY_VAR"
[providers.nokey]
kind = "grok"
model = "m"
key_env = "UNSET_VAR_XYZ"
tier = "cheap"
[providers.mine]
kind = "openai_compat"
url = "http://127.0.0.1:8080/v1/chat/completions"
model = "m"
local = true
tier = "local"
""")
    monkeypatch.setenv("CLOUD_KEY_VAR", SECRET)
    monkeypatch.delenv("UNSET_VAR_XYZ", raising=False)
    capsys.readouterr()
    assert cli.main(["providers", "list"]) == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    lines = {l.split()[0]: l for l in out.splitlines()}
    assert "kind=anthropic" in lines["cloud"] and "host=api.example.invalid" in lines["cloud"]
    assert "tier=default" in lines["cloud"] and lines["cloud"].endswith("key present: yes")
    assert "host=api.x.ai" in lines["nokey"] and lines["nokey"].endswith("key present: no")
    assert "host=127.0.0.1" in lines["mine"] and "n/a (local)" in lines["mine"]


def test_providers_list_without_config_or_providers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERA_HOME", str(tmp_path / "h"))
    assert cli.main(["init"]) == 0
    assert cli.main(["providers", "list"]) == 2
    write_home(tmp_path / "x", monkeypatch, "")
    capsys.readouterr()
    assert cli.main(["providers", "list"]) == 0 and "no providers" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_providers_check_against_a_mock_server(tmp_path, monkeypatch, capsys, kind):
    llm = ScriptedLLM(kind, [step("ready", usage=(30, 3))])
    try:
        write_home(tmp_path, monkeypatch, f"""
[providers.p]
kind = "{'anthropic' if kind == 'anthropic' else 'openai_compat'}"
url = "{llm.url}"
model = "mock-model"
key_env = "P_KEY_VAR"
tier = "cheap"
""")
        monkeypatch.setenv("P_KEY_VAR", SECRET)
        capsys.readouterr()
        assert cli.main(["providers", "check", "p"]) == 0
        out = capsys.readouterr()
        assert "reply: ready" in out.out and "tokens: 30 in, 3 out" in out.out
        assert f"cost: ${(30 * 1.0 + 3 * 4.0) / 1e6:.6f}" in out.out
        assert SECRET not in out.out + out.err
        assert len(llm.requests) == 1
        sent = llm.requests[0]
        text = sent["messages"][0]["content"]
        assert text == "Reply with the single word: ready" and sent["max_tokens"] == 16
        key_header = llm.headers[0].get("x-api-key") or llm.headers[0]["authorization"]
        assert SECRET in key_header
        from hera.store import db
        conn = db.connect(paths.db_path())
        (row,) = conn.execute("SELECT * FROM model_calls").fetchall()
        assert row["purpose"] == "provider-check" and row["destination"] == "p"
        assert [r["decision"] for r in conn.execute("SELECT decision FROM transmissions")] == ["sent"]
        conn.close()
        capsys.readouterr()
        assert cli.main(["status"]) == 0 and "model calls: 1" in capsys.readouterr().out
    finally:
        llm.close()


def test_providers_check_failures(tmp_path, monkeypatch, capsys):
    llm = ScriptedLLM("openai", [], fail_on={1: 500})
    try:
        write_home(tmp_path, monkeypatch, f"""
[providers.p]
kind = "openai_compat"
url = "{llm.url}"
model = "m"
key_env = "P_KEY_VAR"
tier = "cheap"
[providers.nokey]
kind = "openai_compat"
url = "{llm.url}"
model = "m"
key_env = "UNSET_VAR_XYZ"
tier = "cheap"
""")
        monkeypatch.setenv("P_KEY_VAR", SECRET)
        monkeypatch.delenv("UNSET_VAR_XYZ", raising=False)
        capsys.readouterr()
        assert cli.main(["providers", "check", "p"]) == 3
        err = capsys.readouterr().err
        assert "HTTP 500" in err and SECRET not in err and "scripted failure" not in err
        assert cli.main(["providers", "check", "nokey"]) == 2
        assert "UNSET_VAR_XYZ is not set" in capsys.readouterr().err
        assert cli.main(["providers", "check", "missing"]) == 2
        assert len(llm.requests) == 1
    finally:
        llm.close()


def test_providers_check_respects_the_budget(tmp_path, monkeypatch, capsys):
    llm = ScriptedLLM("openai", [step("ready")])
    try:
        write_home(tmp_path, monkeypatch, f"""
[budget]
per_day_usd = 0.0000001
[providers.p]
kind = "openai_compat"
url = "{llm.url}"
model = "m"
key_env = "P_KEY_VAR"
tier = "default"
""")
        monkeypatch.setenv("P_KEY_VAR", SECRET)
        capsys.readouterr()
        assert cli.main(["providers", "check", "p"]) == 3
        assert "cap reached" in capsys.readouterr().err and llm.requests == []
    finally:
        llm.close()


def test_a_provider_named_local_is_refused_with_a_clear_reason(parts):
    gw, ledger = parts
    got = build_providers(cfg(local=dict(kind="openai_compat", url="http://127.0.0.1:9/v1/x", model="m",
                                         local=True, tier="local")), gw, ledger, env={})
    assert "local" not in got and "reserved" in got.absent["local"]
