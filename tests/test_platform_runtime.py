from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import meta_harness.platform_runtime as platform_runtime

from meta_harness.platform_runtime import (
    CursorAgentBinaryNotFound,
    merge_subprocess_no_window_kwargs,
    resolve_cursor_agent_executable,
    resolve_python_launcher,
    subprocess_creationflags_no_window,
)


def test_merge_subprocess_no_window_empty_on_posix_when_not_windows():
    with patch.object(platform_runtime, "is_windows", lambda: False):
        assert merge_subprocess_no_window_kwargs() == {}


def test_merge_subprocess_no_window_sets_flags_when_windows():
    expected = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    with patch.object(platform_runtime, "is_windows", lambda: True):
        assert merge_subprocess_no_window_kwargs() == {"creationflags": expected}


def test_subprocess_creationflags_none_when_not_windows():
    with patch.object(platform_runtime, "is_windows", lambda: False):
        assert subprocess_creationflags_no_window() is None


def test_resolve_python_launcher_meta_harness_python_override(tmp_path):
    fake_py = tmp_path / "custom-python"
    fake_py.write_text("", encoding="utf-8")
    fake_py.chmod(0o755)
    with patch.dict(os.environ, {"META_HARNESS_PYTHON": str(fake_py)}, clear=False):
        assert resolve_python_launcher() == str(fake_py.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="Linux-specific install path")
def test_resolve_cursor_agent_under_local_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "agent"
    exe.write_text("#!/bin/sh\necho\n", encoding="utf-8")
    exe.chmod(0o755)
    with patch("meta_harness.platform_runtime.is_windows", return_value=False):
        with patch("meta_harness.platform_runtime.shutil.which", return_value=None):
            out = resolve_cursor_agent_executable("agent")
    assert out == str(exe.resolve())


def test_cursor_agent_not_found_lists_attempts():
    with patch.object(platform_runtime.shutil, "which", return_value=None):
        with pytest.raises(CursorAgentBinaryNotFound) as ei:
            resolve_cursor_agent_executable("nonexistent-agent-xyz")
    assert "nonexistent-agent-xyz" in str(ei.value)
    assert ei.value.attempted
