from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import meta_harness.cycle as cycle_mod
from meta_harness.cli import main
from meta_harness.config import load_config
from meta_harness.cycle import CycleOutcome, CycleStatus
from meta_harness.research import PaperContent, ResearchEvaluation


@pytest.fixture
def cfg_project(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    return tmp_path


def test_help_shows_purpose():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "Meta-Harness" in combined and "self-improving" in combined


def test_init_creates_layout(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "metaharness.toml").is_file()
    assert (tmp_path / ".metaharness" / "directives").is_dir()
    assert (tmp_path / ".metaharness" / "cycles").is_dir()


def test_init_idempotent_warns(tmp_path: Path):
    runner = CliRunner()
    r1 = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert r1.exit_code == 0
    r2 = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert r2.exit_code == 0
    out = (r2.output or "") + (getattr(r2, "stderr", "") or "")
    assert "already exists" in out
    assert "--force" in out


def test_init_force_overwrites(tmp_path: Path):
    runner = CliRunner()
    assert runner.invoke(main, ["init", "--dir", str(tmp_path)]).exit_code == 0
    cfg = tmp_path / "metaharness.toml"
    cfg.write_text("# corrupted\n", encoding="utf-8")
    assert runner.invoke(main, ["init", "--dir", str(tmp_path), "--force"]).exit_code == 0
    text = cfg.read_text(encoding="utf-8")
    assert "Drop this file in your project root" in text


def test_run_missing_config_exits_nonzero(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--dir", str(tmp_path)])
    assert result.exit_code == 1
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "metaharness.toml" in out
    assert "metaharness init" in out


def test_run_success_monkeypatched_cycle(cfg_project: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_run(c):
        return CycleOutcome(
            cycle_id="c1",
            timestamp="2026-01-01T00:00:00Z",
            status=CycleStatus.COMPLETED,
        )

    monkeypatch.setattr(cycle_mod, "run_cycle", fake_run)
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--dir", str(cfg_project)])
    assert result.exit_code == 0


def test_status_no_cycles(cfg_project: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["status", "--dir", str(cfg_project)])
    assert result.exit_code == 0
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "No cycles recorded yet." in out
    assert "OS:" in out and "Python launcher:" in out


def test_platform_command_prints_runtime(cfg_project: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["platform", "--dir", str(cfg_project)])
    assert result.exit_code == 0
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "Python launcher:" in out
    assert "Cursor agent:" in out


def test_status_with_minimal_cycle_log(cfg_project: Path):
    cfg = load_config(cfg_project)
    payload = {
        "timestamp": "2026-04-04T12:00:00Z",
        "directive": "D001",
        "status": "COMPLETED",
    }
    (cfg.cycles_dir / "cycle_test.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(main, ["status", "--dir", str(cfg_project)])
    assert result.exit_code == 0


def test_run_resolves_config_from_parent_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = tmp_path / "root"
    sub = root / "nested" / "sub"
    sub.mkdir(parents=True)
    (root / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )

    def fake_run(c):
        return CycleOutcome(
            cycle_id="c1",
            timestamp="2026-01-01T00:00:00Z",
            status=CycleStatus.COMPLETED,
        )

    monkeypatch.setattr(cycle_mod, "run_cycle", fake_run)
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--dir", str(sub)])
    assert result.exit_code == 0
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "No metaharness.toml found" not in out


def test_sync_happy_path_maintenance_layer(cfg_project: Path):
    runner = CliRunner()
    did = "HUMAN_001"
    result = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(cfg_project),
            "--id",
            did,
            "--title",
            "Human sync test",
            "--status",
            "COMPLETED",
            "--files",
            "foo.py",
            "--note",
            "note",
        ],
    )
    assert result.exit_code == 0
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "Synced" in out and did in out

    cfg = load_config(cfg_project)
    matches = sorted(cfg.maintenance_cycles_dir.glob(f"sync_{did}_*.json"))
    assert matches, "expected sync_<id>_*.json under maintenance cycles"
    data = json.loads(matches[-1].read_text(encoding="utf-8"))
    assert data["directive"] == did
    assert data["directive_title"] == "Human sync test"
    assert data["status"] == "COMPLETED"
    assert data["synced"] is True
    assert data["layer"] == "maintenance"
    assert data["note"] == "note"


def test_sync_happy_path_product_layer(cfg_project: Path):
    runner = CliRunner()
    did = "HUMAN_PROD_001"
    result = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(cfg_project),
            "--id",
            did,
            "--title",
            "Product layer sync",
            "--layer",
            "product",
        ],
    )
    assert result.exit_code == 0
    cfg = load_config(cfg_project)
    matches = sorted(cfg.product_cycles_dir.glob(f"sync_{did}_*.json"))
    assert matches
    data = json.loads(matches[-1].read_text(encoding="utf-8"))
    assert data["layer"] == "product"
    assert not list(cfg.maintenance_cycles_dir.glob(f"sync_{did}_*.json"))


def test_sync_missing_config_exits_nonzero(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(tmp_path),
            "--id",
            "X001",
            "--title",
            "No config",
        ],
    )
    assert result.exit_code == 1
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "metaharness.toml" in out
    assert "metaharness init" in out


def _ensure_fresh_meta_harness_package() -> None:
    _tpath = Path(__file__).resolve().parent / "conftest.py"
    _spec = importlib.util.spec_from_file_location("_mh_tests_conftest_cli", _tpath)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Cannot load {_tpath}")
    _tmod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tmod)
    _tmod._ensure_meta_harness()


def test_z_cli_research_eval_calls_notify_after_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`metaharness research eval` calls ``notify_research_queue_from_evaluation`` once when enqueue succeeds."""
    _ensure_fresh_meta_harness_package()
    main_fresh = importlib.import_module("meta_harness.cli").main
    research_live = importlib.import_module("meta_harness.research")
    si_live = importlib.import_module("meta_harness.slack_integration")

    (tmp_path / "metaharness.toml").write_text(
        '[project]\nname = "cli-p"\n[cycle]\nveto_seconds = 120\n[slack]\nenabled = true\n',
        encoding="utf-8",
    )
    notified: list[str] = []

    def capture_notify(c, ev):
        notified.append(ev.title)

    monkeypatch.setattr(si_live, "notify_research_queue_from_evaluation", capture_notify)
    ev = ResearchEvaluation(
        url="https://example.com/cli-hook",
        title="CliHookTitle",
        relevant=True,
        confidence=0.9,
        applicable_to="w",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(research_live, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(research_live, "evaluate_paper", lambda c, p: ev)

    runner = CliRunner()
    r = runner.invoke(
        main_fresh, ["research", "eval", ev.url, "--dir", str(tmp_path)]
    )
    assert r.exit_code == 0
    assert notified == ["CliHookTitle"]


def test_z_cli_research_eval_skips_notify_when_enqueue_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _ensure_fresh_meta_harness_package()
    main_fresh = importlib.import_module("meta_harness.cli").main
    research_live = importlib.import_module("meta_harness.research")
    si_live = importlib.import_module("meta_harness.slack_integration")

    (tmp_path / "metaharness.toml").write_text(
        '[project]\nname = "cli-p2"\n[cycle]\nveto_seconds = 120\n[slack]\nenabled = true\n',
        encoding="utf-8",
    )
    notified: list[str] = []

    def capture_notify(c, ev):
        notified.append(ev.title)

    monkeypatch.setattr(si_live, "notify_research_queue_from_evaluation", capture_notify)
    ev = ResearchEvaluation(
        url="https://example.com/cli-full",
        title="NoEnqueue",
        relevant=True,
        confidence=0.9,
        applicable_to="w",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(research_live, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(research_live, "evaluate_paper", lambda c, p: ev)
    monkeypatch.setattr(research_live, "queue_paper", lambda c, e: False)

    runner = CliRunner()
    r = runner.invoke(
        main_fresh, ["research", "eval", ev.url, "--dir", str(tmp_path)]
    )
    assert r.exit_code == 0
    assert notified == []
