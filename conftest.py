"""Clear premature `meta_harness` imports so pytest-cov can trace first real load.

`pytest_load_initial_conftests` runs before test modules import. Removing stale
`sys.modules` entries avoids CoverageWarning: module-not-measured when the
package was imported before the active tracer started.
"""
from __future__ import annotations

import sys

import pytest


def _purge_meta_harness_from_sys_modules() -> None:
    for key in list(sys.modules):
        if key == "meta_harness" or key.startswith("meta_harness."):
            del sys.modules[key]


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config) -> None:  # noqa: ARG001
    _purge_meta_harness_from_sys_modules()
