"""Canonical pytest-coverage policy for this repository's flat layout.

This module is the single source of truth for recommended ``pytest-cov`` flags
when measuring coverage from the repository root. Keep it aligned with:

- ``.coveragerc`` (the ``[run]`` section and the header comment)
- ``tests/test_coveragerc_layout.py``, which guards against drift in that file

Use ``--cov=.`` (not ``--cov=meta_harness`` alone) so pytest-cov traces the
working tree correctly on this layout; see the ``.coveragerc`` header.
"""

from __future__ import annotations

# Paths and flags are relative to repository root (typical pytest cwd).
TESTS_PATH = "tests/"
COVERAGE_CONFIG_FILE = ".coveragerc"
COVERAGE_XML_REPORT = "coverage.xml"

COVERAGE_SOURCE = "."
COVERAGE_FLAG = f"--cov={COVERAGE_SOURCE}"
COVERAGE_CONFIG_FLAG = f"--cov-config={COVERAGE_CONFIG_FILE}"
COVERAGE_XML_REPORT_FLAG = f"--cov-report=xml:{COVERAGE_XML_REPORT}"


def canonical_pytest_coverage_argv() -> list[str]:
    """Fragments to pass after ``pytest`` for the recommended coverage run (cwd = repo root)."""
    return [
        TESTS_PATH,
        COVERAGE_FLAG,
        COVERAGE_CONFIG_FLAG,
        COVERAGE_XML_REPORT_FLAG,
    ]
