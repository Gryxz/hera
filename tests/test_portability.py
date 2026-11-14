"""Device-agnostic rules: no machine-specific paths in the core, and the CLI works from HERA_HOME."""
import re
from pathlib import Path
from hera import cli, config, paths

SRC = Path(__file__).resolve().parents[1] / "src" / "hera"


def test_no_absolute_paths_in_source():
    bad = re.compile(r"""["'](/Users/|/home/|[A-Za-z]:\\\\)""")
    hits = [str(p) for p in SRC.rglob("*.py") if bad.search(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_cli_init_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERA_HOME", str(tmp_path / "h"))
    assert cli.main(["init"]) == 0
    assert paths.db_path().exists() and paths.policy_dir().is_dir()
    assert cli.main(["status"]) == 0
    assert "chain: intact" in capsys.readouterr().out


def test_example_config_loads():
    cfg = config.load(Path(__file__).resolve().parents[1] / "config" / "hera.example.toml")
    assert cfg.budget.per_month_usd == 45.0 and cfg.prices["strong"].output == 40.0
    assert cfg.local_model["enable_thinking"] is False
