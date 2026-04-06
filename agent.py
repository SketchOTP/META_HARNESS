"""
meta_harness/agent.py

Single-phase coding agent: one `agent -p --yolo` call with the directive.
Cursor reads and edits the workspace directly; the harness does not stream file
contents or parse a JSON plan.

Safety (still your responsibility in directives + scope):
  - Configure `scope.protected` for paths the agent must never touch
  - The agent runs with cwd = project root
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from rich.console import Console

from . import cursor_client
from .config import HarnessConfig
from .proposer import Directive

if TYPE_CHECKING:
    from .evidence import Evidence

console = Console()

# Paths that look like repo-relative source files in model summaries / logs
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s`\"'(\[])"
    r"((?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|toml|md|txt|tsx?|jsx?|rs|go|json|ya?ml|ini|cfg))"
    r"(?:[\s`\"')\],]|$)",
    re.IGNORECASE | re.MULTILINE,
)


def _format_phase_error(
    phase: Literal["analyze", "execute", "apply"], message: str
) -> str:
    """Prefix AgentResult / phase failures with [phase:...] for stable truncation in memory."""
    m = (message or "").strip()
    if m.startswith("[phase:"):
        return m
    return f"[phase:{phase}] {m}"


# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class FileChange:
    action: Literal["write", "delete"]
    path: str
    content: str = ""


@dataclass
class PlannedChange:
    action: Literal["write", "delete", "create"]
    path: str
    rationale: str = ""
    description: str = ""


@dataclass
class Observation:
    """Legacy shape; unused in single-phase runs (kept for API compatibility)."""

    architecture_summary: str = ""
    directive_requirements: list[str] = field(default_factory=list)
    affected_areas: list[str] = field(default_factory=list)
    gaps_identified: list[str] = field(default_factory=list)
    new_components_needed: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class Plan:
    summary: str = ""
    changes: list[PlannedChange] = field(default_factory=list)
    raw: str = ""


@dataclass
class AgentResult:
    success: bool
    changes: list[FileChange] = field(default_factory=list)
    observation: Optional[Observation] = None
    plan: Optional[Plan] = None
    reasoning: str = ""
    error: str = ""
    phases_completed: int = 0


# ── Safety helpers (used by path filtering and tests) ──────────────────────────

def _is_protected(rel_path: str, protected: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pat) for pat in protected)


def _is_readable(rel_path: str) -> bool:
    skip_exts = {
        ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib",
        ".png", ".jpg", ".jpeg", ".gif", ".ico",
        ".woff", ".woff2", ".ttf", ".eot",
        ".zip", ".tar", ".gz", ".bz2",
        ".db", ".sqlite", ".sqlite3", ".lock",
    }
    skip_dirs = {
        "__pycache__", ".git", "node_modules", ".venv", "venv",
        "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".metaharness",
    }
    parts = Path(rel_path).parts
    if any(p in skip_dirs for p in parts):
        return False
    if Path(rel_path).suffix.lower() in skip_exts:
        return False
    return True


def _fnmatch_rel(rel: str, pattern: str) -> bool:
    """fnmatch with forward slashes so Windows paths match TOML globs."""
    r = rel.replace("\\", "/")
    p = pattern.replace("\\", "/")
    return fnmatch.fnmatch(r, p)


def _path_mentioned_in_directive(rel: str, directive_text: str) -> bool:
    if not directive_text or not rel:
        return False
    rel_n = rel.replace("\\", "/")
    text = directive_text.replace("\\", "/")
    if rel_n in text:
        return True
    base = Path(rel).name
    return bool(base and base in text)


def _git_rank_map(git_recent_paths: list[str]) -> dict[str, int]:
    return {p.replace("\\", "/"): i for i, p in enumerate(git_recent_paths)}


def _order_analyze_paths(
    paths: list[str],
    directive_text: str,
    git_recent_paths: list[str],
    max_files: int,
) -> list[str]:
    if max_files <= 0 or len(paths) <= max_files:
        return sorted(paths)
    ranks = _git_rank_map(git_recent_paths)

    def sort_key(rel: str) -> tuple[int, int, str]:
        rel_n = rel.replace("\\", "/")
        mentioned = 0 if _path_mentioned_in_directive(rel, directive_text) else 1
        gr = ranks.get(rel_n, 10**9)
        return (mentioned, gr, rel_n)

    ordered = sorted(paths, key=sort_key)
    return ordered[:max_files]


def _iter_readable_non_protected_paths(root: Path, protected: list[str]) -> list[str]:
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if not _is_readable(rel) or _is_protected(rel, protected):
            continue
        out.append(rel)
    return out


def _select_analyze_paths(
    root: Path,
    cfg: HarnessConfig,
    directive_text: str,
    git_recent_paths: list[str],
) -> list[str]:
    modifiable = cfg.scope.modifiable
    protected = cfg.scope.protected
    selected: list[str] = []
    for rel in _iter_readable_non_protected_paths(root, protected):
        in_modifiable = any(_fnmatch_rel(rel, pat) for pat in modifiable)
        mentioned = _path_mentioned_in_directive(rel, directive_text)
        if in_modifiable or mentioned:
            selected.append(rel)

    if not selected:
        selected = _iter_readable_non_protected_paths(root, protected)

    max_n = cfg.cursor.max_files_per_cycle
    return _order_analyze_paths(selected, directive_text, git_recent_paths, max_n)


def _load_analyze_files(
    root: Path,
    cfg: HarnessConfig,
    directive_text: str,
    git_recent_paths: Optional[list[str]] = None,
) -> dict[str, str]:
    paths = _select_analyze_paths(
        root, cfg, directive_text, git_recent_paths or []
    )
    files: dict[str, str] = {}
    for rel in paths:
        p = root / rel
        if not p.is_file():
            continue
        try:
            files[rel] = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return files


def _load_planned_files(root: Path, plan: Plan) -> dict[str, str]:
    """Load only the files touched by the plan (tests / legacy)."""
    files: dict[str, str] = {}
    for ch in plan.changes:
        if ch.action == "delete":
            continue
        target = root / ch.path
        if target.exists() and target.is_file():
            try:
                files[ch.path] = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                files[ch.path] = "(unreadable)"
        else:
            files[ch.path] = "(new file — does not exist yet)"
    return files


def _save_reasoning(reasoning_dir: Path, cycle_id: str, phase: str, content: str) -> None:
    reasoning_dir.mkdir(parents=True, exist_ok=True)
    (reasoning_dir / f"{cycle_id}_{phase}.md").write_text(content, encoding="utf-8")


# ── Yolo agent (single phase) ─────────────────────────────────────────────────

_AGENT_SYSTEM = """\
You are an autonomous coding agent with full access to this repository.
Implement the user's directive by editing files in the workspace. Use the tools
available to you. Do not modify any path listed as protected. Prefer small,
correct changes; run or follow project tests if the directive asks for it.
"""


def _build_execute_prompt_with_scope(directive: Directive, protected: list[str]) -> str:
    prot = "\n".join(f"  - {p}" for p in protected)
    return (
        "Implement this directive exactly as specified.\n\n"
        f"Protected paths (never modify or delete):\n{prot}\n\n"
        f"{directive.content.strip()}"
    )


def _extract_files_from_log(text: str) -> list[str]:
    """Best-effort paths mentioned in the model's stdout (markdown / summary)."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _FILE_PATH_RE.finditer(text):
        p = m.group(1).replace("\\", "/").strip().lstrip("./")
        if p and p not in seen:
            seen.add(p)
            found.append(p)
    return found


def _git_worktree_changed_paths(root: Path) -> list[str]:
    """When the log lists no paths, fall back to `git status --porcelain`."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root.resolve()),
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:].strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[-1].strip()
        if (rest.startswith('"') and rest.endswith('"')) or (
            rest.startswith("'") and rest.endswith("'")
        ):
            rest = rest[1:-1]
        p = rest.replace("\\", "/")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _finalize_changed_paths(
    root: Path,
    candidates: list[str],
    protected: list[str],
) -> list[str]:
    root_r = root.resolve()
    out: list[str] = []
    seen: set[str] = set()
    for p in candidates:
        p = p.replace("\\", "/").strip().lstrip("./")
        if not p or p in seen:
            continue
        if _is_protected(p, protected):
            continue
        try:
            (root_r / p).resolve().relative_to(root_r)
        except ValueError:
            continue
        seen.add(p)
        out.append(p)
    return out


def _changes_from_paths(paths: list[str]) -> list[FileChange]:
    return [FileChange(action="write", path=p, content="") for p in paths]


def _apply_changes(cfg: HarnessConfig, changes: list[FileChange]) -> None:
    root = cfg.project_root

    for change in changes:
        target = (root / change.path).resolve()

        try:
            target.relative_to(root.resolve())
        except ValueError:
            raise PermissionError(f"Refusing to write outside project root: {change.path}")

        if _is_protected(change.path, cfg.scope.protected):
            raise PermissionError(f"Refusing to touch protected file: {change.path}")

        if change.action == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content, encoding="utf-8")
            console.print(
                f"  [green]wrote[/green]   {change.path}  "
                f"[dim]({len(change.content):,} chars)[/dim]"
            )
        elif change.action == "delete":
            if target.exists():
                target.unlink()
                console.print(f"  [red]deleted[/red] {change.path}")


# ── Main entry point ───────────────────────────────────────────────────────────

def run(
    cfg: HarnessConfig,
    directive: Directive,
    cycle_id: str = "",
    evidence: Optional["Evidence"] = None,
    *,
    reasoning_dir: Optional[Path] = None,
    scope_modifiable: Optional[list[str]] = None,
    scope_protected: Optional[list[str]] = None,
) -> AgentResult:
    """
    Run a single yolo agent pass: directive + protected paths → Cursor edits repo.

    ``evidence`` is accepted for API compatibility; file preloading is not used.
    Changed files are inferred from the model log and, if needed, ``git status``.
    """
    _ = evidence

    if not cycle_id:
        cycle_id = f"cycle_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    rdir = reasoning_dir if reasoning_dir is not None else cfg.reasoning_dir
    modifiable = scope_modifiable if scope_modifiable is not None else cfg.scope.modifiable
    protected = scope_protected if scope_protected is not None else cfg.scope.protected

    result = AgentResult(success=False)

    console.print("\n[bold cyan]Agent — direct implementation (yolo)[/bold cyan]")

    user_prompt = _build_execute_prompt_with_scope(directive, protected)
    label = f"{cycle_id}_execute" if cycle_id else "execute"

    resp = cursor_client.agent_call(
        cfg,
        _AGENT_SYSTEM,
        user_prompt,
        label=label,
        timeout_seconds=cfg.cursor.timeout_seconds * 4,
    )

    if not resp.success:
        result.error = _format_phase_error("execute", resp.error or "agent_call failed")
        result.phases_completed = 0
        return result

    raw = resp.raw or ""
    _save_reasoning(
        rdir,
        cycle_id,
        "execute",
        f"# Agent (yolo)\n\n{raw[:100_000]}",
    )

    from_log = _extract_files_from_log(raw)
    paths = _finalize_changed_paths(cfg.project_root, from_log, protected)
    if not paths:
        git_paths = _git_worktree_changed_paths(cfg.project_root)
        paths = _finalize_changed_paths(cfg.project_root, git_paths, protected)

    result.changes = _changes_from_paths(paths)
    result.reasoning = raw[:500]
    result.phases_completed = 1
    result.success = True
    return result
