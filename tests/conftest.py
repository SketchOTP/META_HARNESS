"""Register the flat-layout `meta_harness` package after coverage starts tracing.

With `python -m coverage run ... -m pytest`, tracing is active before imports.
For `pytest --cov=...`, load in `pytest_configure` (trylast) so pytest-cov starts first.
"""
from __future__ import annotations

import importlib
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

_AGENT_STUB_EXE = shutil.which("true") or "/usr/bin/true"


def _sync_test_cursor_client_module(cc) -> None:
    """CLI tests reload ``meta_harness.*``; keep the test module's ``cursor_client`` binding current."""
    for name, mod in list(sys.modules.items()):
        if not name.endswith("test_cursor_client"):
            continue
        if hasattr(mod, "cursor_client"):
            mod.cursor_client = cc  # type: ignore[attr-defined]


def _apply_cursor_agent_stub() -> None:
    """Point Cursor CLI resolution at a real executable so patched subprocess.run tests work."""
    cc = importlib.import_module("meta_harness.cursor_client")

    def _fake(_bin_name: str) -> str:
        return _AGENT_STUB_EXE

    cc._resolve_agent_executable = _fake  # type: ignore[assignment]
    _sync_test_cursor_client_module(cc)


def _restore_cursor_agent_resolution() -> None:
    """Use real PATH-based resolution (for tests of ``_resolve_agent_executable``)."""
    cc = importlib.import_module("meta_harness.cursor_client")
    import meta_harness.platform_runtime as pr

    cc._resolve_agent_executable = pr.resolve_cursor_agent_executable  # type: ignore[assignment]
    _sync_test_cursor_client_module(cc)


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


@pytest.fixture(autouse=True)
def _stub_cursor_agent_for_subprocess_tests(request) -> None:
    """json_call/agent_call tests patch subprocess.run; CI often has no Cursor CLI on PATH.

    Stub resolution to a real executable so _cursor_cmd reaches the mock. Tests that exercise
    ``_resolve_agent_executable`` itself are excluded.

    Re-apply after each test: some suites reload ``meta_harness`` (e.g. CLI tests), which
    replaces ``cursor_client`` and would otherwise drop the stub for subsequent tests.
    """
    if request.node.name.startswith("test_resolve_agent_executable"):
        _restore_cursor_agent_resolution()
        yield
        _apply_cursor_agent_stub()
        return
    _apply_cursor_agent_stub()
    yield
    _apply_cursor_agent_stub()
