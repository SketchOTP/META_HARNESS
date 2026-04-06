"""
meta_harness/rollback.py

Opt-in per-path restore of agent-touched files to HEAD after failed tests or metric
regression. Uses git when available; skips conservatively when the worktree has
uncommitted changes outside the agent path list (when rollback_require_git is true).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .agent import FileChange
    from .config import HarnessConfig as HarnessConfigType


@dataclass
class RollbackResult:
    """Outcome of a rollback attempt (not attempted = enabled but skipped)."""

    attempted: bool
    succeeded: bool
    detail: str


def _norm_rel(path: str) -> str:
    return path.replace("\\", "/")


def _is_harness_state_path(rel: str) -> bool:
    """Paths under .metaharness/ are harness artifacts, not operator edits."""
    parts = _norm_rel(rel).split("/")
    return ".metaharness" in parts


def _run_git(repo_root: Path, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    return r.returncode, out


def is_git_repository(repo_root: Path) -> bool:
    code, _ = _run_git(repo_root, "rev-parse", "--is-inside-work-tree")
    return code == 0


def _collect_dirty_paths(repo_root: Path) -> set[str]:
    """Paths differing from HEAD (modified tracked + untracked), normalized."""
    code1, out1 = _run_git(repo_root, "diff", "--name-only", "HEAD")
    code2, out2 = _run_git(repo_root, "ls-files", "--others", "--exclude-standard")
    dirty: set[str] = set()
    if code1 == 0 and out1:
        for line in out1.splitlines():
            line = line.strip()
            if line:
                dirty.add(_norm_rel(line))
    if code2 == 0 and out2:
        for line in out2.splitlines():
            line = line.strip()
            if line:
                dirty.add(_norm_rel(line))
    return dirty


def _agent_path_set(changes: list["FileChange"]) -> set[str]:
    return {_norm_rel(c.path) for c in changes if c.path}


def _is_tracked(repo_root: Path, rel: str) -> bool:
    code, _ = _run_git(repo_root, "ls-files", "--error-unmatch", "--", rel)
    return code == 0


def _ambiguous_worktree(
    repo_root: Path, agent_paths: set[str], require_git: bool
) -> tuple[bool, str]:
    """
    If there are uncommitted changes outside agent-touched paths, rollback is ambiguous.
    When require_git is False, skip this check (operator accepts risk).
    """
    if not require_git:
        return False, ""
    dirty = _collect_dirty_paths(repo_root)
    dirty = {p for p in dirty if not _is_harness_state_path(p)}
    extra = dirty - agent_paths
    if not extra:
        return False, ""
    sample = ", ".join(sorted(extra)[:8])
    more = "…" if len(extra) > 8 else ""
    return True, f"uncommitted changes outside agent paths ({sample}{more})"


def attempt_restore(
    cfg: "HarnessConfigType",
    changes: list["FileChange"],
    *,
    kind: Literal["test_failure", "metric_regression"],
) -> RollbackResult:
    """
    Restore agent-touched paths toward HEAD when cycle rollback options allow `kind`.
    Caller must gate on cfg.cycle.rollback_enabled and per-kind flags.
    """
    c = cfg.cycle
    if not c.rollback_enabled:
        return RollbackResult(False, False, "rollback disabled")
    if kind == "test_failure" and not c.rollback_on_test_failure:
        return RollbackResult(False, False, "rollback on test failure disabled")
    if kind == "metric_regression" and not c.rollback_on_metric_regression:
        return RollbackResult(False, False, "rollback on metric regression disabled")

    repo = cfg.project_root
    if not is_git_repository(repo):
        return RollbackResult(
            True,
            False,
            "rollback skipped: not a git worktree or git unavailable",
        )

    agent_paths = _agent_path_set(changes)
    if not agent_paths:
        return RollbackResult(True, True, "nothing to restore (no agent paths)")

    ambiguous, amb_msg = _ambiguous_worktree(repo, agent_paths, c.rollback_require_git)
    if ambiguous:
        return RollbackResult(True, False, f"rollback skipped: {amb_msg}")

    # Per-path restore to minimize blast radius.
    for ch in changes:
        rel = _norm_rel(ch.path)
        if not rel or rel.startswith("../"):
            continue
        full = (repo / rel).resolve()
        try:
            full.relative_to(repo.resolve())
        except ValueError:
            return RollbackResult(True, False, f"rollback aborted: path outside repo ({rel})")

        if ch.action == "delete":
            code, err = _run_git(repo, "checkout", "HEAD", "--", rel)
            if code != 0:
                return RollbackResult(
                    True,
                    False,
                    f"git checkout failed for {rel}: {err[:200]}",
                )
        elif ch.action == "write":
            if _is_tracked(repo, rel):
                code, err = _run_git(repo, "checkout", "HEAD", "--", rel)
                if code != 0:
                    return RollbackResult(
                        True,
                        False,
                        f"git checkout failed for {rel}: {err[:200]}",
                    )
            else:
                try:
                    if full.is_file():
                        full.unlink()
                except OSError as e:
                    return RollbackResult(True, False, f"remove untracked file failed {rel}: {e}")
        else:
            return RollbackResult(True, False, f"unknown change action for {rel}")

    return RollbackResult(True, True, "restored agent paths to HEAD")


def is_metric_regression(cfg: HarnessConfig, pre: float, post: float) -> bool:
    """True if post is strictly worse than pre for the configured optimization direction."""
    direction = (cfg.goals.optimization_direction or "maximize").strip().lower()
    delta = post - pre
    if direction == "minimize":
        return delta > 0
    return delta < 0
