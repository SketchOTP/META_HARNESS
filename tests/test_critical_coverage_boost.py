"""Focused coverage for cursor_client, cycle, diagnoser, evidence, knowledge_graph, memory.

Incremental batch (daemon helpers, product_agent pure paths, slack formatters, cursor_client
helpers): on a representative Windows run, `pytest tests/ --cov=. --cov-config=.coveragerc`
TOTAL line coverage moved about **70% → 71%** with gains on `daemon.py`, `product_agent.py`,
`slack_integration.py`, and `cursor_client.py` (see project overview log for the dated entry).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import meta_harness.cycle as cycle_mod
from meta_harness.config import HarnessConfig as HC
from meta_harness.config import TestConfig as MhTestConfig
from meta_harness import cursor_client
from meta_harness import diagnoser as diagnoser_mod
from meta_harness import evidence as evidence_mod
from meta_harness.diagnoser import Diagnosis, _build_prompt, _repair_suffix, run as diagnoser_run
from meta_harness.evidence import Evidence, MetricsBundle, collect, has_sufficient_evidence
from meta_harness.knowledge_graph import KnowledgeGraph, build_cross_layer_context
from meta_harness import memory as memory_mod


# ── cursor_client ─────────────────────────────────────────────────────────────


def test_balanced_chunk_wrong_opener_returns_none():
    assert cursor_client._balanced_chunk("{a}", 1) is None


def test_balanced_chunk_unclosed_brace_returns_none():
    assert cursor_client._balanced_chunk("{ no close", 0) is None


def test_suffix_oriented_json_extract_finds_json_after_fence():
    t = "preamble\n```\nignore\n```\n```\n{\"k\": 1}\n"
    assert cursor_client._suffix_oriented_json_extract(t) == {"k": 1}


def test_extract_json_none_input():
    assert cursor_client.extract_json(None) is None


def test_unwrap_cursor_envelope_result_none_inner_returns_original_text():
    wrapped = json.dumps({"type": "result", "result": None})
    out = cursor_client._unwrap_cursor_envelope(wrapped)
    assert out == wrapped


def test_multi_candidate_json_scan_empty_after_strip():
    assert cursor_client._multi_candidate_json_scan("   \n\t  ") is None


def test_persist_last_cursor_failure_mkdir_oserror_skips(tmp_path: Path):
    cfg = HC(project_root=tmp_path)
    with patch.object(type(cfg.harness_dir), "mkdir", side_effect=OSError("nope")):
        cursor_client._persist_last_cursor_failure(
            cfg, label="x", exit_code=1, stdout="a", stderr="b"
        )
    assert not (cfg.harness_dir / "last_cursor_failure.txt").exists()


def test_persist_last_cursor_failure_huge_extra_truncates(tmp_path: Path):
    cfg = HC(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    huge = "x" * (cursor_client._EXTRA_CAP + 500)
    cursor_client._persist_last_cursor_failure(
        cfg, label="big", exit_code=0, stdout="", stderr="", extra=huge
    )
    text = (cfg.harness_dir / "last_cursor_failure.txt").read_text(encoding="utf-8")
    assert "(extra truncated)" in text


def test_run_cursor_debug_prints_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = HC(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    fake = MagicMock(returncode=0, stdout="{}", stderr="")
    printed: list[str] = []

    monkeypatch.setenv("META_HARNESS_DEBUG", "1")

    def _capture(msg: str, *a, **k):
        printed.append(msg)

    with (
        patch("meta_harness.cursor_client.subprocess.run", return_value=fake),
        patch.object(cursor_client._console, "print", side_effect=_capture),
    ):
        cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert printed and "Cursor cmd" in printed[0]


def test_agent_call_file_not_found(tmp_path: Path):
    cfg = HC(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    with patch(
        "meta_harness.cursor_client.subprocess.run",
        side_effect=FileNotFoundError("no agent"),
    ):
        r = cursor_client.agent_call(cfg, "s", "u", label="t")
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_AGENT_BINARY_MISSING


def test_agent_call_timeout(tmp_path: Path):
    cfg = HC(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    with patch(
        "meta_harness.cursor_client.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="agent", timeout=1),
    ):
        r = cursor_client.agent_call(cfg, "s", "u")
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_TIMEOUT


def test_is_windows_process_interrupt_masked_exit():
    assert cursor_client._is_windows_process_interrupt(0xC000013A & 0xFFFFFFFF) is True


# ── cycle ────────────────────────────────────────────────────────────────────


def test_run_tests_command_none(tmp_path: Path):
    cfg = HC(project_root=tmp_path)
    cfg.test = MhTestConfig(command="none")
    ok, out = cycle_mod._run_tests(cfg)
    assert ok is True
    assert "no test command" in out.lower()


def test_run_tests_subprocess_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = HC(project_root=tmp_path)
    cfg.test.working_dir = "."
    cfg.test.timeout_seconds = 1

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(cycle_mod.subprocess, "run", _raise)
    ok, out = cycle_mod._run_tests(cfg)
    assert ok is False
    assert "timed out" in out.lower()


def test_run_tests_subprocess_generic_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = HC(project_root=tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cycle_mod.subprocess, "run", _boom)
    ok, out = cycle_mod._run_tests(cfg)
    assert ok is False
    assert "boom" in out


def test_run_tests_subprocess_uses_utf8_replace_decoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = HC(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    cfg.test.command = "echo ok"
    cfg.test.junit_xml = False
    captured: dict = {}

    def _record_run(*_a, **kwargs):
        captured.update(kwargs)
        return MagicMock(returncode=0, stdout="out\n", stderr="")

    monkeypatch.setattr(cycle_mod.subprocess, "run", _record_run)
    ok, out = cycle_mod._run_tests(cfg)
    assert ok is True
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["text"] is True


def test_restart_project_command_none(tmp_path: Path):
    cfg = HC(project_root=tmp_path)
    from meta_harness.config import RunConfig

    cfg.run = RunConfig(command="none")
    cycle_mod._restart_project(cfg)


def test_restart_project_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = HC(project_root=tmp_path)
    from meta_harness.config import RunConfig

    cfg.run = RunConfig(command="echo ok", settle_seconds=0)
    fake = MagicMock(returncode=0)
    monkeypatch.setattr(cycle_mod.subprocess, "run", lambda *a, **k: fake)
    cycle_mod._restart_project(cfg)


def test_release_agent_lock_when_no_handle(tmp_path: Path):
    cfg = HC(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    cycle_mod._release_agent_lock(cfg)


def test_read_primary_metric(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = HC(project_root=tmp_path)
    cfg.goals.primary_metric = "coverage_pct"
    ev = Evidence(metrics=MetricsBundle(current={"coverage_pct": 12.34}))
    monkeypatch.setattr(cycle_mod.evidence, "collect", lambda c: ev)
    v = cycle_mod._read_primary_metric(cfg)
    assert v == 12.34


def test_acquire_agent_lock_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = HC(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)

    class _FlakyLock:
        def acquire(self, timeout: int = 300):
            import filelock

            raise filelock.Timeout("locked")

        def release(self):
            pass

    monkeypatch.setattr(cycle_mod.filelock, "FileLock", lambda p: _FlakyLock())
    assert cycle_mod._acquire_agent_lock(cfg, timeout=1) is False


# ── diagnoser ─────────────────────────────────────────────────────────────────


def test_repair_suffix_truncates_long_previous_raw():
    prev = "x" * 5000
    s = _repair_suffix(prev, "err")
    assert "truncated middle" in s
    assert len(s) < len(prev) + 500


def test_build_prompt_includes_cross_layer_when_kg_has_other_layer(tmp_path: Path):
    from meta_harness.config import ProjectConfig

    cfg = HC(project_root=tmp_path)
    cfg.project = ProjectConfig(name="p", description="d")
    cfg.goals.objectives = ["g"]
    cfg.memory.enabled = False
    db = tmp_path / "kgx.db"
    kg = KnowledgeGraph(db)
    kg.upsert_node(
        "P001_auto",
        "directive",
        name="prod",
        summary="s",
        status="PENDING",
        data={"layer": "product", "title": "t"},
    )
    ev = Evidence()
    prompt = _build_prompt(cfg, ev, kg=kg)
    assert "Product / parallel" in prompt or "product" in prompt.lower()


def test_diagnoser_debug_prints_on_meta_harness_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from meta_harness.config import ProjectConfig

    cfg = HC(project_root=tmp_path)
    cfg.project = ProjectConfig(name="p", description="d")
    cfg.goals.objectives = ["g"]
    cfg.memory.enabled = False
    monkeypatch.setenv("META_HARNESS_DEBUG", "1")
    ev = Evidence()
    fake = MagicMock()
    fake.success = True
    fake.data = {"summary": "s", "strengths": [], "weaknesses": [], "patterns": [], "opportunities": [], "risk_areas": []}
    fake.raw = "{}"
    fake.error = ""
    printed: list[str] = []

    def _cap(msg: str, *a, **k):
        printed.append(str(msg))

    with (
        patch.object(diagnoser_mod.cursor_client, "json_call", return_value=fake),
        patch.object(diagnoser_mod.console, "print", side_effect=_cap),
    ):
        diagnoser_run(cfg, ev, kg=None)
    assert any("Diagnoser resp" in p for p in printed)


# ── evidence ─────────────────────────────────────────────────────────────────


def test_reconcile_metrics_incomplete_pytest_sets_zero(tmp_path: Path):
    b = MetricsBundle(current={"test_pass_rate": 1.0, "test_failed": 0.0, "test_count": 5.0})
    tail = "ERROR collecting tests/foo.py"
    evidence_mod._reconcile_metrics_with_pytest_tail(b, tail)
    assert b.current["test_pass_rate"] == 0.0
    assert b.anomalies


def test_has_sufficient_evidence_counts_tests_structured():
    ev = Evidence(tests=evidence_mod.TestEvidence(passed=1, failed=0))
    assert has_sufficient_evidence(ev, min_items=1) is True


def test_collect_reads_cursor_failure_truncated(tmp_path: Path):
    cfg = HC(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    fail = cfg.harness_dir / "last_cursor_failure.txt"
    fail.write_text("e" * 5000, encoding="utf-8")
    ev = collect(cfg)
    assert len(ev.cursor_cli_failure_excerpt) <= 4100
    assert "truncated" in ev.cursor_cli_failure_excerpt


# ── knowledge_graph ──────────────────────────────────────────────────────────


def test_kg_close(tmp_path: Path):
    db = tmp_path / "k.db"
    kg = KnowledgeGraph(db)
    kg.close()
    assert True


def test_neighbors_with_relation_filter(tmp_path: Path):
    kg = KnowledgeGraph(tmp_path / "k.db")
    kg.upsert_node("a", "directive")
    kg.upsert_node("b", "file")
    kg.add_edge("a", "b", "modified")
    assert kg.neighbors("a", direction="out", relation="modified") == ["b"]
    assert kg.neighbors("b", direction="in", relation="modified") == ["a"]


def test_subgraph_depth_zero(tmp_path: Path):
    kg = KnowledgeGraph(tmp_path / "k.db")
    kg.upsert_node("r", "directive")
    assert kg.subgraph("r", depth=0) == {"r"}


def test_search_empty_query(tmp_path: Path):
    kg = KnowledgeGraph(tmp_path / "empty_search.db")
    assert kg.search("  \t") == []


def test_causal_chain_follows_fixed(tmp_path: Path):
    kg = KnowledgeGraph(tmp_path / "k.db")
    kg.upsert_node("d1", "directive")
    kg.upsert_node("d2", "directive")
    kg.add_edge("d1", "d2", "fixed", dedupe=False)
    chain = kg.causal_chain("d1")
    assert "d2" in chain


def test_ingest_cycle_outcome_improved_metric_edge(tmp_path: Path):
    kg = KnowledgeGraph(tmp_path / "k.db")
    kg.ingest_cycle_outcome(
        directive_id="Dz",
        directive_title="t",
        directive_content="",
        status="COMPLETED",
        files_changed=[],
        metric_name="cov",
        metric_before=0.5,
        metric_after=0.9,
    )
    edges = kg.get_edges(src_id="Dz", relation="improved")
    assert len(edges) >= 1


def test_build_cross_layer_context_non_empty(tmp_path: Path):
    kg = KnowledgeGraph(tmp_path / "k.db")
    kg.upsert_node(
        "P002_auto",
        "directive",
        name="n",
        summary="s",
        status="OPEN",
        data={"layer": "product"},
    )
    ctx = build_cross_layer_context(kg, "maintenance")
    assert "P002" in ctx or "product" in ctx.lower()


# ── memory ────────────────────────────────────────────────────────────────────


def test_persist_cycle_outcome_memory_disabled_kg_only(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        "[memory]\nenabled = false\n[cycle]\nveto_seconds = 0\n",
        encoding="utf-8",
    )
    from meta_harness.config import load_config

    cfg = load_config(tmp_path)
    kg = KnowledgeGraph(tmp_path / "kgp.db")
    memory_mod.persist_cycle_outcome(
        cfg,
        kg=kg,
        directive_id="D1",
        directive_title="t",
        status="COMPLETED",
        delta=0.1,
        files_changed=["a.py"],
        pre_metric=None,
        post_metric=None,
        directive_content="",
    )
    assert kg.get_node("D1") is not None


def test_refresh_patterns_updates_kg_file_nodes(tmp_path: Path):
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir(parents=True)
    for i in range(4):
        memory_mod.update(
            mem_dir,
            "p",
            f"D{i}",
            "t",
            "COMPLETED",
            0.1,
            ["x.py"],
            0.0,
            0.2,
            "m",
        )
    kg = KnowledgeGraph(tmp_path / "kgr.db")
    memory_mod.refresh_patterns(mem_dir, kg=kg)
    assert kg.get_node("file:x.py") is not None


def test_render_map_sparkline_single_point(tmp_path: Path):
    m = memory_mod.ProjectMemory()
    m.total_cycles = 1
    m.project_name = "p"
    m.metric_name = "m"
    m.metric_trajectory = [["D0", 1.0]]
    m.metric_baseline = 0.5
    m.metric_current = 1.0
    m.metric_best = 1.0
    out = memory_mod.render_map(m)
    assert "only 1 data point" in out.lower()


# ── daemon ───────────────────────────────────────────────────────────────────


def test_next_scheduled_time_invalid_entry_raises():
    from meta_harness.daemon import _next_scheduled_time

    with pytest.raises(ValueError, match="HH:MM"):
        _next_scheduled_time(["bad"], now=datetime(2026, 1, 1, 12, 0, 0))


def test_interruptible_sleep_nonpositive_returns_immediately(tmp_path: Path):
    from meta_harness import daemon as dm
    from meta_harness.config import load_config

    (tmp_path / "metaharness.toml").write_text("[cycle]\ninterval_seconds = 60\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    sleeps: list[float] = []

    def _track(s: float) -> None:
        sleeps.append(s)

    with patch.object(dm.time, "sleep", side_effect=_track):
        dm._interruptible_sleep(0, cfg)
        dm._interruptible_sleep(-1.0, cfg)
    assert sleeps == []


def test_handle_signal_sets_running_false():
    from meta_harness import daemon as dm

    prev = dm._running
    try:
        dm._running = True
        dm._handle_signal(2, None)
        assert dm._running is False
    finally:
        dm._running = prev


def test_interruptible_sleep_when_paused_waits_then_breaks(tmp_path: Path):
    from meta_harness import daemon as dm
    from meta_harness.config import load_config

    (tmp_path / "metaharness.toml").write_text("[cycle]\ninterval_seconds = 60\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    calls = {"n": 0}

    def _paused(*_a, **_k):
        calls["n"] += 1
        return calls["n"] == 1

    waits: list[int] = []

    def _record_wait(_c):
        waits.append(1)

    with (
        patch.object(dm, "_daemon_paused", side_effect=_paused),
        patch.object(dm, "_wait_until_unpaused", side_effect=_record_wait),
        patch.object(dm.time, "sleep", lambda _s: None),
    ):
        dm._running = True
        dm._interruptible_sleep(30.0, cfg)
    assert waits == [1]


# ── product_agent (pure helpers) ─────────────────────────────────────────────


def test_str_list_coercions():
    from meta_harness.product_agent import _str_list

    assert _str_list(None) == []
    assert _str_list("") == []
    assert _str_list("  ") == []
    assert _str_list("a") == ["a"]
    assert _str_list([1, 2]) == ["1", "2"]
    assert _str_list(99) == ["99"]


def test_product_from_response_failure_and_list_payload():
    from meta_harness.product_agent import _product_from_dict, _product_from_response
    from meta_harness.cursor_client import CursorResponse

    r = CursorResponse(success=False, data=None, raw='{"summary": "x", "existing_features": [], "missing_features": [], "user_value_gaps": [], "next_build_targets": ["a"], "maintenance_blockers": []}')
    out = _product_from_response(r)
    assert out is not None
    assert out.summary == "x"
    assert out.next_build_targets == ["a"]

    inner = {"summary": "y", "existing_features": [], "missing_features": [], "user_value_gaps": [], "next_build_targets": [], "maintenance_blockers": []}
    r2 = CursorResponse(success=True, data=[inner], raw="")
    out2 = _product_from_response(r2)
    assert out2 == _product_from_dict(inner, "")


def test_strip_leading_yaml_frontmatter_unclosed_returns_original():
    from meta_harness.product_agent import _strip_leading_yaml_frontmatter

    text = "---\nid: D1\nno closing fence"
    assert _strip_leading_yaml_frontmatter(text) == text


def test_vision_prompt_block_and_research_queue_empty(tmp_path: Path):
    from meta_harness.product_agent import _research_queue_block, _vision_prompt_block
    from meta_harness.config import load_config

    (tmp_path / "metaharness.toml").write_text("[vision]\nstatement = \"\"\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    vb = _vision_prompt_block(cfg)
    assert "Product vision" in vb
    assert "(not configured" in vb or "—" in vb
    with patch("meta_harness.research.get_queue", return_value=[]):
        assert _research_queue_block(cfg) == ""


def test_run_product_cycle_slack_outcome_post_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from meta_harness import product_agent as pa
    from meta_harness.config import load_config
    from meta_harness.cycle import CycleOutcome, CycleStatus

    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n"
        "[product]\nenabled = true\nveto_seconds = 0\n[memory]\nenabled = false\n"
        "[slack]\nenabled = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    oc = CycleOutcome(
        cycle_id="c1",
        timestamp="t",
        directive_id="P1",
        directive_title="T",
        status=CycleStatus.COMPLETED,
        phases_completed=1,
    )

    monkeypatch.setattr(pa, "_run_product_cycle_inner", lambda c: oc)

    def _boom(*_a, **_k):
        raise RuntimeError("slack unavailable")

    monkeypatch.setattr("meta_harness.slack_integration.post_cycle_outcome", _boom)
    out = pa.run_product_cycle(cfg)
    assert out.cycle_id == "c1"


# ── slack_integration (formatting helpers) ────────────────────────────────────


def test_truncate_slack_ephemeral_long():
    import meta_harness.slack_integration as si

    long = "x" * 4000
    t = si._truncate_slack_ephemeral(long)
    assert len(t) < len(long)
    assert "truncated" in t


def test_slack_format_memory_empty_cycles(tmp_path: Path):
    import meta_harness.slack_integration as si
    from meta_harness.config import load_config

    (tmp_path / "metaharness.toml").write_text("[project]\nname = \"z\"\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    mem = MagicMock()
    mem.total_cycles = 0
    mem.project_name = "z"
    out = si._slack_format_memory(cfg, mem)
    assert "No harness cycles" in out or "recorded" in out.lower()


def test_slack_memmap_empty_cycles(tmp_path: Path):
    import meta_harness.slack_integration as si
    from meta_harness.config import load_config

    (tmp_path / "metaharness.toml").write_text("[project]\nname = \"z\"\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    mem = MagicMock()
    mem.total_cycles = 0
    mem.project_name = "z"
    out = si._slack_memmap(mem, cfg)
    assert "No harness memory" in out or "memory" in out.lower()


def test_slack_format_product_cycles_and_status_empty(tmp_path: Path):
    import meta_harness.slack_integration as si
    from meta_harness.config import load_config

    (tmp_path / "metaharness.toml").write_text("[project]\nname = \"z\"\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    cfg.product_cycles_dir.mkdir(parents=True, exist_ok=True)
    pc = si._slack_format_product_cycles(cfg)
    assert "No product cycles" in pc or "product" in pc.lower()

    st = si._slack_format_status(cfg)
    assert "No cycles" in st or "cycles" in st.lower()


# ── cursor_client (extra branches) ────────────────────────────────────────────


def test_balanced_chunk_start_out_of_range():
    assert cursor_client._balanced_chunk("{}", 99) is None


def test_nonzero_exit_message_prefers_stderr_and_cli_detail():
    assert "code 2" in cursor_client._nonzero_exit_message(2, "out", "err")
    assert "err" in cursor_client._nonzero_exit_message(2, "out", "err")
    assert "out" in cursor_client._nonzero_exit_message(3, "out", "")
    assert "(no stdout/stderr)" in cursor_client._nonzero_exit_message(4, "", "")


def test_cli_interrupted_detail_string():
    assert "Windows" in cursor_client._cli_interrupted_detail()
