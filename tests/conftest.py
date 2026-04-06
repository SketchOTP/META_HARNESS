"""Register the flat-layout `meta_harness` package after coverage starts tracing.

With `python -m coverage run ... -m pytest`, tracing is active before imports.
For `pytest --cov=...`, load in `pytest_configure` (trylast) so pytest-cov starts first.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _purge_meta_harness_from_sys_modules() -> None:
    """Drop stale imports so the package can be bound from the repo tree (see root conftest)."""
    for key in list(sys.modules):
        if key == "meta_harness" or key.startswith("meta_harness."):
            del sys.modules[key]


def _ensure_meta_harness() -> None:
    # Always purge before registering: if meta_harness was imported before pytest-cov
    # traced, an early return would skip spec_from_file_location and break coverage.
    _purge_meta_harness_from_sys_modules()
    _init = _REPO / "__init__.py"
    _spec = importlib.util.spec_from_file_location(
        "meta_harness",
        _init,
        submodule_search_locations=[str(_REPO)],
    )
    if _spec and _spec.loader:
        _mh = importlib.util.module_from_spec(_spec)
        sys.modules["meta_harness"] = _mh
        _spec.loader.exec_module(_mh)


@pytest.hookimpl(trylast=True)
def pytest_configure(config) -> None:
    _ensure_meta_harness()
