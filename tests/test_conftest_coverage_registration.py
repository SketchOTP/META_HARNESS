"""Ensure tests/conftest `_ensure_meta_harness` replaces a stale `sys.modules` entry."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# `tests` is not a package (`from tests.conftest` fails). `from conftest` loads the
# repo-root conftest.py instead of this directory's — load sibling explicitly.
_TESTS_CONFTEST = Path(__file__).resolve().parent / "conftest.py"
_spec = importlib.util.spec_from_file_location(
    "_metaharness_tests_conftest", _TESTS_CONFTEST
)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load {_TESTS_CONFTEST}")
_tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tc)
_REPO = _tc._REPO
_ensure_meta_harness = _tc._ensure_meta_harness


def test_ensure_meta_harness_replaces_stale_sys_modules_entry() -> None:
    dummy = types.ModuleType("meta_harness")
    dummy.__path__ = []  # type: ignore[attr-defined]
    sys.modules["meta_harness"] = dummy

    _ensure_meta_harness()

    import meta_harness  # noqa: PLC0415 — must run after _ensure_meta_harness

    assert meta_harness.__file__ is not None
    assert Path(meta_harness.__file__).resolve() == (_REPO / "__init__.py").resolve()
    assert sys.modules["meta_harness"] is not dummy
