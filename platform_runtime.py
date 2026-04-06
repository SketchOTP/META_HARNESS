"""
Cross-platform runtime helpers for Meta-Harness (Windows and Linux first-class).

Centralizes OS detection, Python launcher choice, Cursor ``agent`` resolution,
and Windows-only subprocess flags (e.g. CREATE_NO_WINDOW).
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Optional override: absolute path to Python for subprocesses / diagnostics.
_ENV_PYTHON = "META_HARNESS_PYTHON"


class CursorAgentBinaryNotFound(FileNotFoundError):
    """Raised when the configured Cursor CLI executable cannot be resolved."""

    def __init__(self, agent_bin: str, attempted: list[str]) -> None:
        self.agent_bin = agent_bin
        self.attempted = attempted
        detail = ", ".join(attempted) if attempted else "(none)"
        msg = (
            f"Cursor agent CLI not found (configured agent_bin={agent_bin!r}). "
            f"Checked: {detail}. "
            "Install the Cursor CLI, ensure `agent` is on PATH, or set [cursor] agent_bin "
            "in metaharness.toml to the full path of the agent executable."
        )
        super().__init__(msg)


@dataclass(frozen=True)
class PlatformInfo:
    system: str
    release: str
    machine: str
    is_windows: bool
    is_linux: bool
    python_launcher: str


def is_windows() -> bool:
    return os.name == "nt"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def subprocess_creationflags_no_window() -> int | None:
    """Return ``creationflags`` for subprocess to avoid an extra console on Windows; ``None`` on POSIX."""
    if not is_windows():
        return None
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def merge_subprocess_no_window_kwargs() -> dict[str, Any]:
    """Kwargs fragment for ``subprocess.run`` / ``Popen``: hidden child window on Windows only."""
    cf = subprocess_creationflags_no_window()
    if cf is None:
        return {}
    return {"creationflags": cf}


def resolve_python_launcher() -> str:
    """
    Return a Python executable suitable for spawning subprocesses on this OS.

    - ``META_HARNESS_PYTHON`` (if set and executable) wins.
    - Windows: ``py`` launcher when on PATH, else ``sys.executable``.
    - Linux/macOS: ``python3`` or ``python`` on PATH, else ``sys.executable``.
    """
    override = (os.environ.get(_ENV_PYTHON) or "").strip()
    if override:
        p = Path(override)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
        w = shutil.which(override)
        if w:
            return w
    if is_windows():
        py = shutil.which("py")
        if py:
            return py
        return sys.executable
    for name in ("python3", "python"):
        w = shutil.which(name)
        if w:
            return w
    return sys.executable


def get_platform_info() -> PlatformInfo:
    uname = platform.uname()
    return PlatformInfo(
        system=uname.system,
        release=uname.release,
        machine=uname.machine,
        is_windows=is_windows(),
        is_linux=is_linux(),
        python_launcher=resolve_python_launcher(),
    )


def _attempt_record(attempted: list[str], label: str) -> None:
    if label not in attempted:
        attempted.append(label)


def _linux_extra_candidates(agent_bin: str) -> list[Path]:
    names = [agent_bin]
    base = Path(agent_bin).name
    if base == "agent":
        names.extend(["cursor-agent", "cursor"])
    # De-duplicate preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    dirs = [
        Path.home() / ".local/bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/opt/cursor"),
    ]
    out: list[Path] = []
    for d in dirs:
        for n in uniq:
            out.append(d / n)
    return out


def _resolve_windows_shim(found: str, agent_bin: str, attempted: list[str]) -> str | None:
    if not (found.lower().endswith(".ps1") and is_windows()):
        return None
    _attempt_record(attempted, f"shim:{found}")
    w_exe = shutil.which(f"{agent_bin}.exe")
    if w_exe:
        pe = Path(w_exe)
        if pe.is_file():
            _attempt_record(attempted, f"which:{agent_bin}.exe->{w_exe}")
            return str(pe.resolve())
    sibling_exe = Path(found).with_suffix(".exe")
    if sibling_exe.is_file():
        _attempt_record(attempted, f"sibling_exe:{sibling_exe}")
        return str(sibling_exe.resolve())
    cmd = Path(found).with_suffix(".cmd")
    if cmd.is_file():
        _attempt_record(attempted, f"sibling_cmd:{cmd}")
        return str(cmd.resolve())
    _attempt_record(attempted, f"fallback_ps1:{found}")
    return found


def resolve_cursor_agent_executable(agent_bin: str) -> str:
    """
    Resolve the Cursor ``agent`` CLI to an executable path.

    Prefer a configured absolute path; otherwise PATH and platform-specific install
    locations. Raises :class:`CursorAgentBinaryNotFound` if nothing usable exists.
    """
    attempted: list[str] = []
    raw = (agent_bin or "").strip()
    if not raw:
        raise CursorAgentBinaryNotFound(agent_bin, ["(empty agent_bin)"])

    p = Path(raw)
    if p.is_file():
        _attempt_record(attempted, f"file:{p.resolve()}")
        return str(p.resolve())
    if p.is_absolute():
        _attempt_record(attempted, f"missing:{raw}")
        raise CursorAgentBinaryNotFound(agent_bin, attempted)

    found = shutil.which(raw)
    if found:
        resolved = _resolve_windows_shim(found, raw, attempted)
        if resolved is not None:
            return resolved
        _attempt_record(attempted, f"which:{raw}->{found}")
        return found

    if is_windows():
        for suffix in (".exe", ".cmd"):
            name = f"{raw}{suffix}"
            w = shutil.which(name)
            if w:
                _attempt_record(attempted, f"which:{name}->{w}")
                return w
            _attempt_record(attempted, f"which:{name}->(not found)")

    if is_linux() or sys.platform == "darwin":
        for cand in _linux_extra_candidates(raw):
            _attempt_record(attempted, f"candidate:{cand}")
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand.resolve())

    if not attempted:
        _attempt_record(attempted, f"PATH:`{raw}`")
    raise CursorAgentBinaryNotFound(agent_bin, attempted)


def runtime_status_line() -> str:
    """One-line summary for daemon / logs (no config required)."""
    info = get_platform_info()
    return (
        f"Runtime: {info.system} {info.release} ({info.machine}) | "
        f"Python: {info.python_launcher}"
    )


def describe_runtime_for_harness(agent_bin: str) -> str:
    """Multi-line description including resolved Cursor agent (or error)."""
    info = get_platform_info()
    lines = [
        f"OS: {info.system} {info.release} ({info.machine})",
        f"Python launcher: {info.python_launcher}",
    ]
    try:
        exe = resolve_cursor_agent_executable(agent_bin)
        lines.append(f"Cursor agent: {exe}")
    except CursorAgentBinaryNotFound as e:
        lines.append(f"Cursor agent: not resolved — {e}")
    return "\n".join(lines)


def format_slack_runtime_block(agent_bin: str) -> str:
    """Short mrkdwn for Slack slash ``platform``."""
    return describe_runtime_for_harness(agent_bin)
