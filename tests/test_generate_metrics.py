from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_generate_metrics():
    path = REPO_ROOT / "scripts" / "generate_metrics.py"
    spec = importlib.util.spec_from_file_location("_generate_metrics", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_read_test_results_skipped_does_not_lower_pass_rate(tmp_path, monkeypatch):
    gm = _load_generate_metrics()
    monkeypatch.setattr(gm, "ROOT", tmp_path)
    (tmp_path / ".metaharness").mkdir(parents=True)
    xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuite tests="68" failures="0" errors="0" skipped="1" time="12.3">
</testsuite>
"""
    (tmp_path / ".metaharness" / "test_results.xml").write_text(xml, encoding="utf-8")
    d = gm.read_test_results()
    assert d["test_pass_rate"] == 1.0
    assert d["test_count"] == 68
    assert d["test_passed"] == 67
    assert d["test_skipped"] == 1


def test_read_test_results_failures(tmp_path, monkeypatch):
    gm = _load_generate_metrics()
    monkeypatch.setattr(gm, "ROOT", tmp_path)
    (tmp_path / ".metaharness").mkdir(parents=True)
    xml = """<?xml version="1.0"?>
<testsuite tests="10" failures="2" errors="0" skipped="0" time="1.0">
</testsuite>
"""
    (tmp_path / ".metaharness" / "test_results.xml").write_text(xml, encoding="utf-8")
    d = gm.read_test_results()
    assert d["test_pass_rate"] == 0.8
    assert d["test_failed"] == 2


def test_cursor_json_fields_from_toml(tmp_path: Path):
    from meta_harness.config import load_config

    (tmp_path / "metaharness.toml").write_text(
        "[cursor]\nagent_timeout = 111\njson_timeout = 222\njson_retries = 3\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.cursor.timeout_seconds == 111
    assert cfg.cursor.json_timeout == 222
    assert cfg.cursor.json_retries == 3
