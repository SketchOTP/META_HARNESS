from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from meta_harness import memory as mem_mod
from meta_harness import vision as vision_mod
from meta_harness.config import HarnessConfig, load_config
from meta_harness.knowledge_graph import KnowledgeGraph


def test_daemon_waits_for_schedule_on_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from meta_harness import daemon as dm

    (tmp_path / "metaharness.toml").write_text(
        '[cycle]\ninterval_seconds = 0\nschedule = ["23:59"]\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    fixed_now = datetime(2026, 4, 6, 12, 0, 0)
    future = fixed_now + timedelta(minutes=10)
    monkeypatch.setattr(dm, "_next_scheduled_time", lambda sched, now=None: future)
    sleeps: list[float] = []

    def track_sleep(w: float, c: HarnessConfig) -> None:
        sleeps.append(w)
        dm._running = False

    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(dm, "datetime", _FixedNow)
    monkeypatch.setattr(dm, "_interruptible_sleep", track_sleep)
    monkeypatch.setattr(dm, "run_cycle", lambda c: None)
    dm._running = True
    dm.run_daemon(cfg)
    assert sleeps and abs(sleeps[0] - 600.0) < 0.01


def test_daemon_fires_immediately_when_catch_up_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from meta_harness import daemon as dm

    (tmp_path / "metaharness.toml").write_text(
        '[cycle]\ninterval_seconds = 0\ncatch_up = true\nschedule = ["23:59"]\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.cycle.catch_up is True
    fixed_now = datetime(2026, 4, 6, 12, 0, 0)
    post_cycle_wait = 123.45
    next_sched = fixed_now + timedelta(seconds=post_cycle_wait)

    def sched(schedule: list[str], now=None):
        return next_sched

    monkeypatch.setattr(dm, "_next_scheduled_time", sched)
    sleeps: list[float] = []
    cycles_ran: list[int] = []

    def track_sleep(w: float, c: HarnessConfig) -> None:
        sleeps.append(w)
        dm._running = False

    monkeypatch.setattr(dm, "_interruptible_sleep", track_sleep)
    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(dm, "datetime", _FixedNow)
    monkeypatch.setattr(dm, "run_cycle", lambda c: cycles_ran.append(1))
    dm._running = True
    dm.run_daemon(cfg)
    assert cycles_ran == [1]
    assert len(sleeps) == 1
    assert abs(sleeps[0] - post_cycle_wait) < 0.01


def test_product_loop_waits_for_schedule_on_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from meta_harness import daemon as dm

    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\ninterval_seconds = 60\n"
        "[product]\nenabled = true\nschedule = [\"18:00\"]\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    fixed_now = datetime(2026, 4, 6, 12, 0, 0)
    future = fixed_now + timedelta(minutes=10)
    monkeypatch.setattr(dm, "_next_scheduled_time", lambda sched, now=None: future)
    sleeps: list[float] = []

    def track_sleep(w: float, c: HarnessConfig) -> None:
        sleeps.append(w)
        dm._running = False

    monkeypatch.setattr(dm, "_interruptible_sleep", track_sleep)
    monkeypatch.setattr(
        "meta_harness.product_agent.run_product_cycle",
        lambda c: None,
    )

    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(dm, "datetime", _FixedNow)
    dm._running = True
    dm._run_product_loop(cfg)
    assert sleeps and abs(sleeps[0] - 600.0) < 0.01


def test_infer_patterns_completed_zero_delta_is_success():
    m = mem_mod.ProjectMemory()
    m.directives = [
        {"status": "COMPLETED", "delta": 0, "files": ["a.py"], "title": "a"},
        {"status": "COMPLETED", "delta": 0, "files": ["b.py"], "title": "b"},
        {"status": "COMPLETED", "delta": 0, "files": ["c.py"], "title": "c"},
    ]
    sp, fp, _ = mem_mod.infer_patterns(m)
    assert not any("fails" in p for p in fp)
    assert any("py" in p and "gains" in p for p in sp)


def test_infer_patterns_requires_3_samples():
    m = mem_mod.ProjectMemory()
    m.directives = [
        {"status": "COMPLETED", "delta": 0, "files": ["a.py"], "title": "a"},
        {"status": "COMPLETED", "delta": 0, "files": ["a.py"], "title": "b"},
    ]
    sp, fp, _ = mem_mod.infer_patterns(m)
    assert not sp and not fp


def test_infer_patterns_only_flags_true_failures():
    m = mem_mod.ProjectMemory()
    m.directives = [
        {"status": "TEST_FAILED", "delta": None, "files": ["x.toml"], "title": "f1"},
        {"status": "TEST_FAILED", "delta": None, "files": ["y.toml"], "title": "f2"},
        {"status": "TEST_FAILED", "delta": None, "files": ["z.toml"], "title": "f3"},
        {"status": "COMPLETED", "delta": 0, "files": ["w.toml"], "title": "ok"},
    ]
    _, fp, _ = mem_mod.infer_patterns(m)
    assert any(".toml-changes→fails" in p for p in fp)


def test_feature_covered_matches_short_words():
    assert vision_mod._feature_covered(
        "Git-based KG sync",
        ["Git-anchored knowledge graph sync for human commits"],
    )


def test_feature_covered_matches_multi_project():
    assert vision_mod._feature_covered(
        "Multi-project management from single daemon",
        ["Multi-project control plane for the harness daemon"],
    )


def test_sync_blocks_on_existing_daemon_cycle(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    cfg.maintenance_cycles_dir.mkdir(parents=True, exist_ok=True)
    existing = cfg.maintenance_cycles_dir / "daemon_P007.json"
    existing.write_text(
        json.dumps(
            {
                "directive": "P007_auto",
                "directive_title": "daemon",
                "status": "COMPLETED",
                "synced": False,
            }
        ),
        encoding="utf-8",
    )
    before = list(cfg.maintenance_cycles_dir.glob("*.json"))
    runner = CliRunner()
    from meta_harness.cli import main

    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(tmp_path),
            "--id",
            "P007_auto",
            "--title",
            "human",
        ],
    )
    assert r.exit_code == 0
    assert "Warning" in (r.output or "")
    after = list(cfg.maintenance_cycles_dir.glob("*.json"))
    assert len(after) == len(before)


def test_sync_force_bypasses_guard(tmp_path: Path):
    from meta_harness.cli import main

    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    cfg.maintenance_cycles_dir.mkdir(parents=True, exist_ok=True)
    existing = cfg.maintenance_cycles_dir / "daemon_P007.json"
    existing.write_text(
        json.dumps(
            {
                "directive": "P007_auto",
                "directive_title": "daemon",
                "status": "COMPLETED",
                "synced": False,
            }
        ),
        encoding="utf-8",
    )
    before_n = len(list(cfg.maintenance_cycles_dir.glob("*.json")))
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(tmp_path),
            "--id",
            "P007_auto",
            "--title",
            "human",
            "--force",
        ],
    )
    assert r.exit_code == 0
    after_n = len(list(cfg.maintenance_cycles_dir.glob("*.json")))
    assert after_n == before_n + 1


def test_sync_blocks_on_existing_kg_node(tmp_path: Path):
    from meta_harness.cli import main

    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    kg = KnowledgeGraph(cfg.kg_path)
    try:
        kg.upsert_node(
            "P007_auto",
            "directive",
            name="Already done",
            status="COMPLETED",
            data={},
        )
    finally:
        kg.close()
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(tmp_path),
            "--id",
            "P007_auto",
            "--title",
            "retry",
        ],
    )
    assert r.exit_code == 0
    assert "Warning" in (r.output or "")


def test_catch_up_config_defaults_to_false(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.cycle.catch_up is False
    assert cfg.product.catch_up is False


def test_catch_up_config_reads_from_toml(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        '[project]\nname = "x"\n[cycle]\ncatch_up = true\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.cycle.catch_up is True
