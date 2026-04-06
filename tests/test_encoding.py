from __future__ import annotations

import json
import locale
from pathlib import Path

import pytest
from click.testing import CliRunner

from meta_harness import memory as mem_mod
from meta_harness.cli import main
from meta_harness.config import load_config


def test_memory_load_survives_unicode_in_json(tmp_path: Path):
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    payload = {
        "project_name": "Proj\u2192Unicode",
        "total_cycles": 7,
        "failure_patterns": ["\u2192 oops", "\u2026 trail"],
        "directives": [
            {
                "id": "D001",
                "title": "\u2026 title",
                "status": "COMPLETED",
                "delta": 0.1,
                "files_changed": ["a.py"],
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
    }
    (mem_dir / "project_memory.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    m = mem_mod.load(mem_dir)
    assert m.total_cycles == 7
    assert m.project_name == "Proj\u2192Unicode"
    assert "\u2192 oops" in m.failure_patterns


def test_memory_load_returns_blank_on_missing_file(tmp_path: Path):
    mem_dir = tmp_path / "no_file_here"
    mem_dir.mkdir()
    m = mem_mod.load(mem_dir)
    assert m.total_cycles == 0
    assert m.project_name == ""


def test_status_command_survives_unicode_in_cycle_json(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    payload = {
        "timestamp": "2026-04-04T12:00:00Z",
        "directive": "D088",
        "directive_title": "\u2192 ModuleNotFoundError: \u2026",
        "status": "AGENT_FAILED",
        "changes_applied": "\u2192 stderr excerpt \u2026",
    }
    (cfg.maintenance_cycles_dir / "cycle_unicode.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(main, ["status", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "D088" in out


def test_init_command_reads_gitignore_as_utf8(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("# Ünïcödé\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--force"])
    assert result.exit_code == 0
    # init reads UTF-8 then appends via write_text() without encoding (locale default);
    # re-read with the same default the platform used for that write.
    enc = locale.getpreferredencoding(False) or "utf-8"
    text = (tmp_path / ".gitignore").read_text(encoding=enc)
    assert ".metaharness/" in text
    assert "Ünïcödé" in text
