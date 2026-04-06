"""Guard tests: ``meta_harness.coverage_policy`` stays aligned with ``.coveragerc``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meta_harness import coverage_policy as cp

REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGERC = REPO_ROOT / ".coveragerc"


def _run_section(text: str) -> str:
    m = re.search(r"\[run\](.*?)(?=\n\[|\Z)", text, flags=re.DOTALL | re.IGNORECASE)
    if not m:
        pytest.fail(".coveragerc: missing [run] section")
    return m.group(1)


def test_canonical_cov_is_dot_not_package_name():
    assert cp.COVERAGE_SOURCE == "."
    assert cp.COVERAGE_FLAG == "--cov=."
    assert "meta_harness" not in cp.COVERAGE_FLAG


def test_canonical_argv_order_and_flags():
    argv = cp.canonical_pytest_coverage_argv()
    assert argv == [
        cp.TESTS_PATH,
        cp.COVERAGE_FLAG,
        cp.COVERAGE_CONFIG_FLAG,
        cp.COVERAGE_XML_REPORT_FLAG,
    ]
    assert not any(a.startswith("--cov=meta_harness") for a in argv)


def test_policy_matches_coveragerc_run_section():
    run = _run_section(COVERAGERC.read_text(encoding="utf-8"))
    assert re.search(r"^\s*branch\s*=\s*true\s*$", run, re.MULTILINE)
    assert re.search(r"^\s*source\s*=\s*\.\s*$", run, re.MULTILINE)
    assert "tests/*" in run


def test_policy_matches_coveragerc_header_intent():
    head = "\n".join(COVERAGERC.read_text(encoding="utf-8").splitlines()[:12])
    assert "--cov=." in head
    assert "--cov=meta_harness" in head
    assert cp.COVERAGE_FLAG in head
