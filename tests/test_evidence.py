from __future__ import annotations

import json
from pathlib import Path

import pytest

from meta_harness.config import EvidenceConfig, HarnessConfig, ScopeConfig
from meta_harness import evidence


def _cfg(tmp_path: Path, **kwargs) -> HarnessConfig:
    cfg = HarnessConfig(project_root=tmp_path)
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def test_collect_logs_reads_matching_files(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log").write_text("line1\nline2\n", encoding="utf-8")
    cfg = _cfg(
        tmp_path,
        evidence=EvidenceConfig(log_patterns=["logs/*.log"], max_age_hours=24, max_log_lines=10),
    )
    ev = evidence.collect(cfg)
    assert "line2" in ev.log_tail


def test_collect_logs_error_pattern_extraction(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    msg = "ValueError: bad input\n"
    (tmp_path / "logs" / "e.log").write_text(msg * 3, encoding="utf-8")
    cfg = _cfg(
        tmp_path,
        evidence=EvidenceConfig(log_patterns=["logs/*.log"], max_age_hours=24, max_log_lines=50),
    )
    ev = evidence.collect(cfg)
    pat = next((p for p in ev.error_patterns if "ValueError" in p["pattern"]), None)
    assert pat is not None
    assert pat["count"] == 3


def test_collect_metrics_reads_json(tmp_path: Path):
    (tmp_path / "metrics.json").write_text(json.dumps({"accuracy": 0.85}), encoding="utf-8")
    cfg = _cfg(
        tmp_path,
        evidence=EvidenceConfig(metrics_patterns=["metrics.json"], max_age_hours=24),
    )
    ev = evidence.collect(cfg)
    assert ev.metrics.current.get("accuracy") == 0.85


def test_collect_metrics_anomaly_detection(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    hist = cfg.harness_dir / "metrics_history.jsonl"
    hist.write_text(
        json.dumps({"accuracy": 0.5, "ts": 1}) + "\n" + json.dumps({"accuracy": 0.9, "ts": 2}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "m.json").write_text(json.dumps({"accuracy": 0.9}), encoding="utf-8")
    cfg.evidence.metrics_patterns = ["m.json"]
    ev = evidence.collect(cfg)
    assert any("accuracy" in a for a in ev.metrics.anomalies)


def test_collect_ast_python_file(tmp_path: Path):
    py = tmp_path / "mod.py"
    py.write_text(
        "def a():\n    pass\n\ndef b():\n    pass\n\nclass C:\n    pass\n",
        encoding="utf-8",
    )
    cfg = _cfg(tmp_path, scope=ScopeConfig(modifiable=["**/*.py"], protected=[]))
    ev = evidence.collect(cfg)
    assert "a" in ev.ast.functions and "b" in ev.ast.functions
    assert "C" in ev.ast.classes


def test_collect_ast_syntax_error(tmp_path: Path):
    (tmp_path / "bad.py").write_text("def x(:\n", encoding="utf-8")
    cfg = _cfg(tmp_path, scope=ScopeConfig(modifiable=["**/*.py"], protected=[]))
    ev = evidence.collect(cfg)
    assert ev.ast.syntax_errors


def test_collect_ast_long_function(tmp_path: Path):
    body = "\n".join(["    pass"] * 55)
    src = f"def long_one():\n{body}\n"
    (tmp_path / "long.py").write_text(src, encoding="utf-8")
    cfg = _cfg(tmp_path, scope=ScopeConfig(modifiable=["**/*.py"], protected=[]))
    ev = evidence.collect(cfg)
    assert any("long_one" in x for x in ev.ast.long_functions)


def test_collect_tests_from_xml(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    xml = """<?xml version="1.0"?>
<testsuite tests="3" failures="1" errors="0">
  <testcase name="ok" classname="t"/>
  <testcase name="ok2" classname="t"/>
  <testcase name="bad" classname="t"><failure message="e">e</failure></testcase>
</testsuite>
"""
    (cfg.harness_dir / "test_results.xml").write_text(xml, encoding="utf-8")
    ev = evidence.collect(cfg)
    assert ev.tests.passed == 2
    assert ev.tests.failed == 1


def test_collect_tests_from_raw_output(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    (cfg.harness_dir / "test_results.xml").unlink(missing_ok=True)
    (cfg.harness_dir / "last_test_output.txt").write_text(
        "===== 2 passed, 1 failed in 1s =====\n",
        encoding="utf-8",
    )
    ev = evidence.collect(cfg)
    assert ev.tests.passed == 2 and ev.tests.failed == 1


def test_collect_cursor_cli_failure_excerpt(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    (cfg.harness_dir / "last_cursor_failure.txt").write_text(
        "timestamp_utc: 2026-01-01T00:00:00Z\nlabel: json_call:parse\nexit_code: 0\n",
        encoding="utf-8",
    )
    ev = evidence.collect(cfg)
    assert "json_call:parse" in ev.cursor_cli_failure_excerpt


def test_collect_cursor_cli_failure_excerpt_truncates(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    long_body = "x" * 5000
    (cfg.harness_dir / "last_cursor_failure.txt").write_text(long_body, encoding="utf-8")
    ev = evidence.collect(cfg)
    assert len(ev.cursor_cli_failure_excerpt) <= 4100
    assert "(truncated)" in ev.cursor_cli_failure_excerpt


def test_collect_deps_from_requirements(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text(
        "requests>=2.0\n#c\nflask\n",
        encoding="utf-8",
    )
    cfg = _cfg(tmp_path)
    ev = evidence.collect(cfg)
    assert "requests" in ev.deps.packages and "flask" in ev.deps.packages


def test_has_sufficient_evidence_true(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("x\n", encoding="utf-8")
    (tmp_path / "metrics.json").write_text('{"x": 1}', encoding="utf-8")
    cfg = _cfg(tmp_path)
    ev = evidence.collect(cfg)
    assert evidence.has_sufficient_evidence(ev, 1) is True


def test_has_sufficient_evidence_false(tmp_path: Path):
    cfg = _cfg(tmp_path)
    ev = evidence.Evidence()
    assert evidence.has_sufficient_evidence(ev, 1) is False


def test_has_sufficient_evidence_metrics_and_junit_only(tmp_path: Path):
    """Bootstrap (D002): metrics.json + junit XML without last_test_output.txt yet."""
    harness = tmp_path / ".metaharness"
    harness.mkdir(parents=True)
    (tmp_path / "metrics.json").write_text('{"test_pass_rate": 1.0}', encoding="utf-8")
    junit = """<?xml version="1.0"?>
<testsuite tests="2" failures="0" errors="0" skipped="0" time="0.1">
  <testcase name="a" classname="t" time="0.05"/>
  <testcase name="b" classname="t" time="0.05"/>
</testsuite>
"""
    (harness / "test_results.xml").write_text(junit, encoding="utf-8")
    cfg = _cfg(tmp_path)
    ev = evidence.collect(cfg)
    assert evidence.has_sufficient_evidence(ev, 2) is True


def test_to_prompt_sections_budget(tmp_path: Path):
    ev = evidence.Evidence(
        log_tail="x" * 5000,
        metrics=evidence.MetricsBundle(current={"a": 1.0}),
        test_results="passed",
    )
    ev.tests.passed = 1
    out = evidence.to_prompt_sections(ev, max_chars=200)
    assert len(out) <= 220


def test_to_prompt_sections_priority():
    ev = evidence.Evidence(log_tail="Z" * 100)
    ev.tests.failed = 2
    ev.tests.failed_names = ["test_a"]
    out = evidence.to_prompt_sections(ev, max_chars=5000)
    ti = out.find("Test results")
    li = out.find("Runtime logs")
    assert ti != -1 and li != -1 and ti < li


def test_format_evidence_for_diagnosis_compact_metrics(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.evidence.metrics_json_compact = True
    ev = evidence.Evidence(metrics=evidence.MetricsBundle(current={"a": 1.0, "b": 2.0}))
    sec = evidence.format_evidence_for_diagnosis(cfg, ev)
    assert sec["Metrics"] == '{"a":1.0,"b":2.0}'


def test_reconcile_metrics_when_pytest_collection_fails(tmp_path: Path):
    """Stale metrics.json must not imply a green tree when last_test_output shows collection error."""
    cfg = _cfg(tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "metrics.json").write_text(
        json.dumps({"test_pass_rate": 1.0, "test_failed": 0.0, "test_count": 10.0}),
        encoding="utf-8",
    )
    cfg.evidence.metrics_patterns = ["metrics.json"]
    excerpt = (
        "_________________ ERROR collecting tests/test_broken.py _________________\n"
        "ImportError while importing test module ...\n"
        "E   ModuleNotFoundError: No module named 'missing_pkg'\n"
    )
    (cfg.harness_dir / "last_test_output.txt").write_text(excerpt, encoding="utf-8")
    ev = evidence.collect(cfg)
    assert ev.metrics.current.get("test_pass_rate") == 0.0
    assert ev.metrics.current.get("test_failed", 0) >= 1.0
    assert any("reconciled" in a.lower() and "last_test_output" in a.lower() for a in ev.metrics.anomalies)
    assert ev.tests.failed >= 1
    assert "collection" in ev.tests.failed_names


def test_reconcile_metrics_unchanged_on_successful_pytest_tail(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "metrics.json").write_text(json.dumps({"test_pass_rate": 1.0}), encoding="utf-8")
    cfg.evidence.metrics_patterns = ["metrics.json"]
    (cfg.harness_dir / "last_test_output.txt").write_text(
        "tests/test_ok.py::test_a PASSED\n===== 3 passed in 0.12s =====\n",
        encoding="utf-8",
    )
    ev = evidence.collect(cfg)
    assert ev.metrics.current.get("test_pass_rate") == 1.0
    assert not any("reconciled" in a.lower() for a in ev.metrics.anomalies)
