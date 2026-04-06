"""Guard tests: keep .coveragerc [run] settings aligned with flat-layout coverage policy."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGERC = REPO_ROOT / ".coveragerc"


def _run_section(text: str) -> str:
    m = re.search(r"\[run\](.*?)(?=\n\[|\Z)", text, flags=re.DOTALL | re.IGNORECASE)
    if not m:
        pytest.fail(".coveragerc: missing [run] section")
    return m.group(1)


def test_coveragerc_run_branch_and_source():
    run = _run_section(COVERAGERC.read_text(encoding="utf-8"))
    assert re.search(r"^\s*branch\s*=\s*true\s*$", run, re.MULTILINE)
    assert re.search(r"^\s*source\s*=\s*\.\s*$", run, re.MULTILINE)


def test_coveragerc_run_omit_includes_tests_glob():
    run = _run_section(COVERAGERC.read_text(encoding="utf-8"))
    assert re.search(r"^\s*omit\s*=", run, re.MULTILINE)
    assert "tests/*" in run


def test_coveragerc_header_documents_cov_dot_vs_package():
    """Lock intent with .coveragerc header comment (flat layout vs --cov=meta_harness)."""
    head = "\n".join(COVERAGERC.read_text(encoding="utf-8").splitlines()[:12])
    assert "--cov=." in head
    assert "--cov=meta_harness" in head
