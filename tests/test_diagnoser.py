from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meta_harness.config import HarnessConfig, MemoryConfig, ProjectConfig
from meta_harness import diagnoser as _diagnoser_module
from meta_harness.diagnoser import Diagnosis, _build_prompt, run
from meta_harness.evidence import Evidence, MetricsBundle
from meta_harness.knowledge_graph import KnowledgeGraph


def _patch_diagnoser_json_call(*args, **kwargs):
    """Patch json_call on the cursor_client module object diagnoser.run actually uses.

    After tests/conftest purges ``sys.modules['meta_harness.*']``, a string patch on
    ``meta_harness.cursor_client.json_call`` can target a reloaded module while
    ``run`` still closes over the pre-purge submodule; patch the bound object instead.
    """
    return patch.object(_diagnoser_module.cursor_client, "json_call", *args, **kwargs)


def _base_cfg(tmp_path: Path) -> HarnessConfig:
    cfg = HarnessConfig(project_root=tmp_path)
    cfg.project = ProjectConfig(name="tproj", description="desc")
    cfg.goals.objectives = ["goal1"]
    cfg.memory.enabled = False
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_run_success_parses_diagnosis_list_of_dict(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    ev = Evidence()
    fake_resp = MagicMock()
    fake_resp.success = True
    fake_resp.data = [
        {
            "summary": "from list",
            "strengths": [],
            "weaknesses": [],
            "patterns": [],
            "opportunities": [],
            "risk_areas": [],
        }
    ]
    fake_resp.raw = "[]"
    fake_resp.error = ""
    with _patch_diagnoser_json_call(return_value=fake_resp):
        d = run(cfg, ev, kg=None)
    assert d.summary == "from list"


def test_run_success_normalizes_title_case_keys(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    ev = Evidence()
    fake_resp = MagicMock()
    fake_resp.success = True
    fake_resp.data = {
        "Summary": "titled",
        "Strengths": ["s"],
        "Weaknesses": [],
        "Patterns": [],
        "Opportunities": [],
        "Risk Areas": ["x"],
    }
    fake_resp.raw = "{}"
    fake_resp.error = ""
    with _patch_diagnoser_json_call(return_value=fake_resp):
        d = run(cfg, ev, kg=None)
    assert d.summary == "titled"
    assert d.strengths == ["s"]
    assert d.risk_areas == ["x"]


def test_run_success_parses_diagnosis_dict(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    ev = Evidence(
        collected_at="2020-01-01Z",
        metrics=MetricsBundle(current={"x": 1.0}),
    )
    fake_resp = MagicMock()
    fake_resp.success = True
    fake_resp.data = {
        "summary": "ok",
        "strengths": ["a"],
        "weaknesses": ["b"],
        "patterns": ["p"],
        "opportunities": ["o"],
        "risk_areas": ["r"],
    }
    fake_resp.raw = '{"summary": "ignored"}'
    fake_resp.error = ""
    with _patch_diagnoser_json_call(return_value=fake_resp):
        d = run(cfg, ev, kg=None)
    assert isinstance(d, Diagnosis)
    assert d.summary == "ok"
    assert d.strengths == ["a"]
    assert d.weaknesses == ["b"]
    assert d.patterns == ["p"]
    assert d.opportunities == ["o"]
    assert d.risk_areas == ["r"]


def test_run_fallback_uses_extract_json_on_raw(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    ev = Evidence()
    fake_resp = MagicMock()
    fake_resp.success = False
    fake_resp.data = None
    fake_resp.raw = 'noise\n```json\n{"summary": "from raw", "strengths": []}\n```'
    fake_resp.error = "bad"
    with _patch_diagnoser_json_call(return_value=fake_resp):
        d = run(cfg, ev, kg=None)
    assert d.summary == "from raw"


def test_run_no_json_uses_error_summary(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    ev = Evidence()
    fail = MagicMock()
    fail.success = False
    fail.data = None
    fail.raw = ""
    fail.error = "agent failed completely"
    fail2 = MagicMock()
    fail2.success = False
    fail2.data = None
    fail2.raw = ""
    fail2.error = "repair also failed"
    with _patch_diagnoser_json_call(side_effect=[fail, fail2]):
        d = run(cfg, ev, kg=None)
    assert "agent failed" in d.summary


def test_run_includes_memory_when_enabled(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    cfg.memory = MemoryConfig(enabled=True)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "project_memory.json").write_text(
        '{"total_cycles": 1, "completed": 1, "failed": 0, "vetoed": 0, "directives": []}',
        encoding="utf-8",
    )
    ev = Evidence()
    fake_resp = MagicMock()
    fake_resp.success = True
    fake_resp.data = {"summary": "s", "strengths": [], "weaknesses": [], "patterns": [], "opportunities": [], "risk_areas": []}
    fake_resp.raw = "{}"
    fake_resp.error = ""
    kg = KnowledgeGraph(tmp_path / "kg.db")
    with _patch_diagnoser_json_call(return_value=fake_resp) as jc:
        run(cfg, ev, kg=kg)
    assert jc.called
    call_kw = jc.call_args
    assert "Harness Memory" in call_kw[0][2] or "Harness Memory" in str(call_kw)


def test_build_prompt_includes_cursor_cli_failure_excerpt(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    ex = "exit_code: 1\nstderr: boom\n"
    ev = Evidence(cursor_cli_failure_excerpt=ex)
    prompt = _build_prompt(cfg, ev, kg=None)
    assert "Last Cursor / Agent CLI failure (most recent)" in prompt
    assert "boom" in prompt


def test_run_inline_parse_retry_succeeds_without_diagnose_repair_call(tmp_path: Path):
    """First stdout is prose; second attempt (same json_call) returns fenced diagnosis JSON."""
    cfg = _base_cfg(tmp_path)
    cfg.cursor.json_retries = 1
    ev = Evidence()
    prose = MagicMock(
        returncode=0,
        stdout="Status update: discussed D014 and file paths; no JSON here.",
        stderr="",
    )
    inner = {
        "summary": "inline retry ok",
        "strengths": ["s"],
        "weaknesses": ["w"],
        "patterns": ["p"],
        "opportunities": ["o"],
        "risk_areas": ["r"],
    }
    fenced = "```json\n" + json.dumps(inner) + "\n```\n"
    good = MagicMock(returncode=0, stdout=fenced, stderr="")
    with patch.object(
        _diagnoser_module.cursor_client.subprocess,
        "run",
        side_effect=[prose, good],
    ) as run_mock:
        d = run(cfg, ev, kg=None)
    assert run_mock.call_count == 2
    assert d.summary == "inline retry ok"
    assert d.strengths == ["s"]


def test_diagnose_repair_succeeds_after_prose_first_call(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    ev = Evidence()
    first = MagicMock()
    first.success = False
    first.failure_kind = "JSON_PARSE"
    first.data = None
    first.raw = "Here is my analysis in prose only, no JSON."
    first.error = "[agent_fail:json_parse] No valid JSON in agent output"
    second = MagicMock()
    second.success = True
    second.data = {
        "summary": "repaired summary",
        "strengths": ["s"],
        "weaknesses": ["w"],
        "patterns": ["p"],
        "opportunities": ["o"],
        "risk_areas": ["r"],
    }
    second.raw = "{}"
    second.error = ""
    with _patch_diagnoser_json_call(side_effect=[first, second]) as jc:
        d = run(cfg, ev, kg=None)
    assert jc.call_count == 2
    assert d.summary == "repaired summary"
    assert jc.call_args_list[1].kwargs.get("label") == "diagnose_repair"
    assert jc.call_args_list[1].kwargs.get("max_retries") == 0


def test_diagnose_repair_still_fails_degrades_like_before(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    ev = Evidence()
    first = MagicMock()
    first.success = False
    first.data = None
    first.raw = "Still just prose from the model."
    first.error = "parse failed"
    second = MagicMock()
    second.success = False
    second.data = None
    second.raw = "More prose, still no JSON fence."
    second.error = "parse failed again"
    with _patch_diagnoser_json_call(side_effect=[first, second]) as jc:
        d = run(cfg, ev, kg=None)
    assert jc.call_count == 2
    assert d.summary == first.raw[:500]
    assert d.raw == first.raw


def test_run_json_timeout_uses_cursor_json_timeout(tmp_path: Path):
    cfg = _base_cfg(tmp_path)
    cfg.cursor.json_timeout = 99
    ev = Evidence()
    fake_resp = MagicMock(success=True, data={"summary": ""}, raw="{}", error="")
    with _patch_diagnoser_json_call(return_value=fake_resp) as jc:
        run(cfg, ev, None)
    assert jc.call_args.kwargs.get("timeout_seconds") == 99
