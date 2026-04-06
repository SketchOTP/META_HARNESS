from __future__ import annotations

import json
from pathlib import Path
import pytest

import meta_harness.cycle as cycle_mod
from meta_harness.config import load_config
from meta_harness.cycle import CycleStatus, run_cycle
from meta_harness.rollback import RollbackResult
from meta_harness.diagnoser import Diagnosis
from meta_harness.evidence import Evidence, MetricsBundle
from meta_harness.proposer import Directive
from meta_harness.agent import AgentResult, FileChange


@pytest.fixture
def cfg_project(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    return tmp_path


def test_full_cycle_success(cfg_project: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(cfg_project)

    def fake_collect(c):
        return Evidence(
            log_tail="log",
            metrics=MetricsBundle(current={"x": 1.0}),
        )

    monkeypatch.setattr(cycle_mod.evidence, "collect", fake_collect)
    monkeypatch.setattr(cycle_mod.evidence, "has_sufficient_evidence", lambda ev, n: True)
    monkeypatch.setattr(
        cycle_mod,
        "_read_primary_metric",
        lambda c: None,
    )

    monkeypatch.setattr(
        cycle_mod.diagnoser,
        "run",
        lambda c, ev, kg=None: Diagnosis(
            summary="s",
            opportunities=["o"],
        ),
    )

    dpath = cfg.directives_dir / "D001_auto.md"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text("---\nid: D001_auto\n---\n# body\n", encoding="utf-8")
    directive = Directive(
        id="D001_auto",
        path=dpath,
        title="t",
        content=dpath.read_text(encoding="utf-8"),
    )

    monkeypatch.setattr(
        cycle_mod.proposer,
        "run",
        lambda c, dx, kg=None: directive,
    )

    monkeypatch.setattr(
        cycle_mod.agent,
        "run",
        lambda *a, **k: AgentResult(
            success=True,
            changes=[FileChange("write", "a.py", "x=1\n")],
            phases_completed=1,
        ),
    )

    monkeypatch.setattr(cycle_mod, "_run_tests", lambda c: (True, "ok"))
    monkeypatch.setattr(cycle_mod, "_restart_project", lambda c: None)

    outcome = run_cycle(cfg)
    assert outcome.status == CycleStatus.COMPLETED
    logs = list(cfg.maintenance_cycles_dir.glob("*.json"))
    assert logs


def test_cycle_insufficient_evidence(cfg_project: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(cfg_project)

    monkeypatch.setattr(
        cycle_mod.evidence,
        "collect",
        lambda c: Evidence(),
    )
    monkeypatch.setattr(
        cycle_mod.evidence,
        "has_sufficient_evidence",
        lambda ev, n: False,
    )

    outcome = run_cycle(cfg)
    assert outcome.status == CycleStatus.INSUFFICIENT_EVIDENCE


def test_cycle_vetoed(cfg_project: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(cfg_project)
    cfg.cycle.veto_seconds = 5

    monkeypatch.setattr(
        cycle_mod.evidence,
        "collect",
        lambda c: Evidence(log_tail="a", metrics=MetricsBundle(current={"m": 1})),
    )
    monkeypatch.setattr(cycle_mod.evidence, "has_sufficient_evidence", lambda ev, n: True)
    monkeypatch.setattr(cycle_mod, "_read_primary_metric", lambda c: None)
    monkeypatch.setattr(
        cycle_mod.diagnoser,
        "run",
        lambda *a, **k: Diagnosis(summary="s", opportunities=["o"]),
    )
    dpath = cfg.directives_dir / "Dx.md"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text("x", encoding="utf-8")
    dr = Directive(id="Dx", path=dpath, title="t", content="c")
    monkeypatch.setattr(cycle_mod.proposer, "run", lambda *a, **k: dr)
    monkeypatch.setattr(cycle_mod, "_veto_window", lambda *a, **k: False)

    outcome = run_cycle(cfg)
    assert outcome.status == CycleStatus.VETOED

    from meta_harness.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(cfg.kg_path)
    node = kg.get_node("Dx")
    assert node is not None
    assert node["status"] == "VETOED"

    mem_path = cfg.memory_dir / "project_memory.json"
    mem_data = json.loads(mem_path.read_text(encoding="utf-8"))
    assert mem_data["total_cycles"] == 1
    assert mem_data["vetoed"] == 1
    assert mem_data["failed"] == 0
    assert any(d.get("id") == "Dx" and d.get("status") == "VETOED" for d in mem_data["directives"])


def test_cycle_agent_failed(cfg_project: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(cfg_project)

    monkeypatch.setattr(
        cycle_mod.evidence,
        "collect",
        lambda c: Evidence(log_tail="a", metrics=MetricsBundle(current={"m": 1})),
    )
    monkeypatch.setattr(cycle_mod.evidence, "has_sufficient_evidence", lambda ev, n: True)
    monkeypatch.setattr(cycle_mod, "_read_primary_metric", lambda c: None)
    monkeypatch.setattr(
        cycle_mod.diagnoser,
        "run",
        lambda *a, **k: Diagnosis(summary="s", opportunities=["o"]),
    )
    dpath = cfg.directives_dir / "Dy.md"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text("x", encoding="utf-8")
    dr = Directive(id="Dy", path=dpath, title="t", content="c")
    monkeypatch.setattr(cycle_mod.proposer, "run", lambda *a, **k: dr)
    monkeypatch.setattr(cycle_mod.agent, "run", lambda *a, **k: AgentResult(success=False, error="nope"))

    outcome = run_cycle(cfg)
    assert outcome.status == CycleStatus.AGENT_FAILED

    mem_path = cfg.memory_dir / "project_memory.json"
    mem_data = json.loads(mem_path.read_text(encoding="utf-8"))
    assert any(
        d.get("id") == "Dy" and d.get("status") == "AGENT_FAILED" and d.get("err") == "nope"
        for d in mem_data["directives"]
    )


def test_cycle_test_failed(cfg_project: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(cfg_project)

    monkeypatch.setattr(
        cycle_mod.evidence,
        "collect",
        lambda c: Evidence(log_tail="a", metrics=MetricsBundle(current={"m": 1})),
    )
    monkeypatch.setattr(cycle_mod.evidence, "has_sufficient_evidence", lambda ev, n: True)
    monkeypatch.setattr(cycle_mod, "_read_primary_metric", lambda c: None)
    monkeypatch.setattr(
        cycle_mod.diagnoser,
        "run",
        lambda *a, **k: Diagnosis(summary="s", opportunities=["o"]),
    )
    dpath = cfg.directives_dir / "Dz.md"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text("x", encoding="utf-8")
    dr = Directive(id="Dz", path=dpath, title="t", content="c")
    monkeypatch.setattr(cycle_mod.proposer, "run", lambda *a, **k: dr)
    monkeypatch.setattr(
        cycle_mod.agent,
        "run",
        lambda *a, **k: AgentResult(
            success=True,
            changes=[FileChange("write", "f.py", "x=1\n")],
        ),
    )
    monkeypatch.setattr(cycle_mod, "_run_tests", lambda c: (False, "FAILED tests/x.py"))

    outcome = run_cycle(cfg)
    assert outcome.status == CycleStatus.TEST_FAILED


def test_cycle_logs_json(cfg_project: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(cfg_project)
    monkeypatch.setattr(
        cycle_mod.evidence,
        "collect",
        lambda c: Evidence(),
    )
    monkeypatch.setattr(cycle_mod.evidence, "has_sufficient_evidence", lambda ev, n: False)
    run_cycle(cfg)
    logs = list(cfg.maintenance_cycles_dir.glob("*.json"))
    assert logs
    data = json.loads(logs[0].read_text(encoding="utf-8"))
    assert "status" in data and "cycle_id" in data


def test_cycle_updates_kg_on_complete(cfg_project: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(cfg_project)

    monkeypatch.setattr(
        cycle_mod.evidence,
        "collect",
        lambda c: Evidence(log_tail="a", metrics=MetricsBundle(current={"m": 1})),
    )
    monkeypatch.setattr(cycle_mod.evidence, "has_sufficient_evidence", lambda ev, n: True)
    monkeypatch.setattr(cycle_mod, "_read_primary_metric", lambda c: None)
    monkeypatch.setattr(
        cycle_mod.diagnoser,
        "run",
        lambda *a, **k: Diagnosis(summary="s", opportunities=["o"]),
    )
    dpath = cfg.directives_dir / "DK.md"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text("x", encoding="utf-8")
    did = "DK"
    dr = Directive(id=did, path=dpath, title="t", content="c")
    monkeypatch.setattr(cycle_mod.proposer, "run", lambda *a, **k: dr)
    monkeypatch.setattr(
        cycle_mod.agent,
        "run",
        lambda *a, **k: AgentResult(
            success=True,
            changes=[FileChange("write", "z.py", "1")],
        ),
    )
    monkeypatch.setattr(cycle_mod, "_run_tests", lambda c: (True, "ok"))
    monkeypatch.setattr(cycle_mod, "_restart_project", lambda c: None)

    run_cycle(cfg)
    from meta_harness.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(cfg.kg_path)
    node = kg.get_node(did)
    assert node is not None
    assert node["status"] == "COMPLETED"


def test_cycle_test_failed_rollback_records_success(
    cfg_project: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = load_config(cfg_project)
    cfg.cycle.rollback_enabled = True

    monkeypatch.setattr(
        cycle_mod.evidence,
        "collect",
        lambda c: Evidence(log_tail="a", metrics=MetricsBundle(current={"m": 1})),
    )
    monkeypatch.setattr(cycle_mod.evidence, "has_sufficient_evidence", lambda ev, n: True)
    monkeypatch.setattr(cycle_mod, "_read_primary_metric", lambda c: None)
    monkeypatch.setattr(
        cycle_mod.diagnoser,
        "run",
        lambda *a, **k: Diagnosis(summary="s", opportunities=["o"]),
    )
    dpath = cfg.directives_dir / "Drb.md"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text("x", encoding="utf-8")
    dr = Directive(id="Drb", path=dpath, title="t", content="c")
    monkeypatch.setattr(cycle_mod.proposer, "run", lambda *a, **k: dr)
    monkeypatch.setattr(
        cycle_mod.agent,
        "run",
        lambda *a, **k: AgentResult(
            success=True,
            changes=[FileChange("write", "f.py", "x=1\n")],
        ),
    )
    monkeypatch.setattr(cycle_mod, "_run_tests", lambda c: (False, "FAILED tests/x.py"))
    monkeypatch.setattr(
        cycle_mod.rollback,
        "attempt_restore",
        lambda *a, **k: RollbackResult(True, True, "restored agent paths to HEAD"),
    )

    outcome = run_cycle(cfg)
    assert outcome.status == CycleStatus.TEST_FAILED
    assert outcome.rollback_attempted is True
    assert outcome.rollback_succeeded is True
    logs = list(cfg.maintenance_cycles_dir.glob("*.json"))
    data = json.loads(logs[-1].read_text(encoding="utf-8"))
    assert data.get("rollback_succeeded") is True


def test_cycle_metric_regression_rollback(
    cfg_project: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = load_config(cfg_project)
    cfg.cycle.rollback_enabled = True
    cfg.goals.primary_metric = "m"
    cfg.goals.optimization_direction = "maximize"

    monkeypatch.setattr(
        cycle_mod.evidence,
        "collect",
        lambda c: Evidence(log_tail="a", metrics=MetricsBundle(current={"m": 1.0})),
    )
    monkeypatch.setattr(cycle_mod.evidence, "has_sufficient_evidence", lambda ev, n: True)

    metric_calls = iter([1.0, 0.5])

    def _read_m(c):
        return next(metric_calls)

    monkeypatch.setattr(cycle_mod, "_read_primary_metric", _read_m)
    monkeypatch.setattr(
        cycle_mod.diagnoser,
        "run",
        lambda *a, **k: Diagnosis(summary="s", opportunities=["o"]),
    )
    dpath = cfg.directives_dir / "Dmr.md"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text("x", encoding="utf-8")
    dr = Directive(id="Dmr", path=dpath, title="t", content="c")
    monkeypatch.setattr(cycle_mod.proposer, "run", lambda *a, **k: dr)
    monkeypatch.setattr(
        cycle_mod.agent,
        "run",
        lambda *a, **k: AgentResult(
            success=True,
            changes=[FileChange("write", "z.py", "1")],
        ),
    )
    monkeypatch.setattr(cycle_mod, "_run_tests", lambda c: (True, "ok"))
    monkeypatch.setattr(cycle_mod, "_restart_project", lambda c: None)
    monkeypatch.setattr(
        cycle_mod.rollback,
        "attempt_restore",
        lambda *a, **k: RollbackResult(True, True, "restored agent paths to HEAD"),
    )

    outcome = run_cycle(cfg)
    assert outcome.status == CycleStatus.METRIC_REGRESSION
    assert outcome.delta == pytest.approx(-0.5)
    assert outcome.rollback_succeeded is True
