"""
meta_harness/git_kg_sync.py

Ingest human git commits into the SQLite knowledge graph with a durable cursor
stored as a `meta` node (`git_sync_cursor`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from git import Commit

from .knowledge_graph import KnowledgeGraph

CURSOR_NODE_ID = "git_sync_cursor"
CURSOR_NODE_TYPE = "meta"
COMMIT_NODE_PREFIX = "git_commit:"


@dataclass
class GitKgSyncResult:
    commits_processed: int
    initialized_cursor: bool = False
    cursor_sha: str | None = None
    dry_run: bool = False
    warning: str = ""


_DIRECTIVE_ID_RE = re.compile(r"\b[DMP]\d{3}[a-zA-Z0-9_]*\b")


def _directive_ids_in_text(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _DIRECTIVE_ID_RE.finditer(text or ""):
        s = m.group(0)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _list_touched_files(repo: Any, commit: "Commit") -> list[dict[str, str]]:
    """Paths changed in `commit`, diff vs first parent (merge: first-parent)."""
    if commit.parents:
        parent = commit.parents[0]
        out: list[dict[str, str]] = []
        for d in parent.diff(commit):
            path = d.b_path or d.a_path or ""
            if not path:
                continue
            st = getattr(d, "change_type", None) or "M"
            out.append({"path": path, "status": str(st)})
        return out
    blobs: list[dict[str, str]] = []
    for item in commit.tree.traverse():
        if getattr(item, "type", None) == "blob":
            blobs.append({"path": item.path, "status": "A"})
    return blobs


def _collect_first_parent_chain(
    head: Any,
    stop_sha: str | None,
    *,
    max_commits: int | None,
) -> tuple[list[Any], bool]:
    """
    Walk first-parent from `head` until `stop_sha` (exclusive).
    Returns (commits newest-first, stop_found).
    If ``stop_sha`` is None, walk to the initial commit (full history cap).
    """
    chain: list[Any] = []
    cur = head
    found_stop = False
    while True:
        if stop_sha is not None and cur.hexsha == stop_sha:
            found_stop = True
            break
        chain.append(cur)
        if max_commits is not None and len(chain) >= max_commits:
            break
        if not cur.parents:
            break
        cur = cur.parents[0]
    return chain, found_stop


def _repo_from_project_root(project_root: Path) -> Any:
    from git import Repo
    from git.exc import InvalidGitRepositoryError

    try:
        return Repo(project_root, search_parent_directories=True)
    except InvalidGitRepositoryError as e:
        raise RuntimeError(f"Not a git repository (or parent): {project_root}") from e


def sync_git_to_kg(
    cfg: Any,
    *,
    dry_run: bool = False,
    full: bool = False,
    max_commits: int | None = None,
    since: int | None = None,
) -> GitKgSyncResult:
    """
    Sync new git commits on the current branch (first-parent chain) into the KG.

    - First run (no cursor, default): sets cursor to HEAD without ingesting history.
    - ``full``: first-parent walk from HEAD toward root; ingests up to ``max_commits``.
    - ``since``: after resolving the chain, keep only the ``since`` newest commits.
    """
    from git.exc import BadName, InvalidGitRepositoryError

    project_root = Path(cfg.project_root).resolve()
    repo = _repo_from_project_root(project_root)
    try:
        head = repo.head.commit
    except (ValueError, InvalidGitRepositoryError) as e:
        raise RuntimeError(f"Could not read HEAD: {e}") from e

    git_root = Path(repo.working_tree_dir or project_root).resolve()
    warning = ""

    kg_path = Path(cfg.kg_path)
    if not dry_run:
        kg_path.parent.mkdir(parents=True, exist_ok=True)

    kg = KnowledgeGraph(kg_path)
    try:
        cursor_node = kg.get_node(CURSOR_NODE_ID)
        cursor_data: dict[str, Any] = (
            dict(cursor_node["data"]) if cursor_node and cursor_node.get("data") else {}
        )
        last_sha = (cursor_data.get("last_processed_sha") or "").strip() or None

        if not full and cursor_node is None:
            if dry_run:
                return GitKgSyncResult(
                    0,
                    initialized_cursor=True,
                    cursor_sha=head.hexsha,
                    dry_run=True,
                    warning="Would initialize cursor to HEAD (no commits ingested).",
                )
            cursor_data = {
                "last_processed_sha": head.hexsha,
                "last_processed_iso": head.committed_datetime.isoformat(),
                "repo_root": str(git_root),
            }
            kg.upsert_node(
                CURSOR_NODE_ID,
                CURSOR_NODE_TYPE,
                name="git_sync_cursor",
                summary="Git → KG sync cursor",
                status="active",
                data=cursor_data,
            )
            return GitKgSyncResult(
                0,
                initialized_cursor=True,
                cursor_sha=head.hexsha,
                dry_run=False,
            )

        if not full and last_sha == head.hexsha:
            return GitKgSyncResult(
                0,
                initialized_cursor=False,
                cursor_sha=head.hexsha,
                dry_run=dry_run,
            )

        stop_sha: str | None = None
        if full:
            stop_sha = None
        else:
            if last_sha:
                try:
                    repo.commit(last_sha)
                except (BadName, ValueError):
                    warning = (
                        f"Cursor SHA {last_sha[:12]}… not in repo; resetting cursor to HEAD."
                    )
                    if not dry_run:
                        cursor_data = {
                            "last_processed_sha": head.hexsha,
                            "last_processed_iso": head.committed_datetime.isoformat(),
                            "repo_root": str(git_root),
                        }
                        kg.upsert_node(
                            CURSOR_NODE_ID,
                            CURSOR_NODE_TYPE,
                            name="git_sync_cursor",
                            summary="Git → KG sync cursor",
                            status="active",
                            data=cursor_data,
                        )
                    return GitKgSyncResult(
                        0,
                        initialized_cursor=False,
                        cursor_sha=head.hexsha,
                        dry_run=dry_run,
                        warning=warning,
                    )
                stop_sha = last_sha
            else:
                stop_sha = None

        walk_cap: int | None = max_commits
        if since is not None:
            walk_cap = since if walk_cap is None else min(walk_cap, since)

        chain, found_stop = _collect_first_parent_chain(head, stop_sha, max_commits=walk_cap)

        if not full and stop_sha and not found_stop:
            warning = (
                "Cursor commit is not on the first-parent path from HEAD; "
                "skipping ingest. Use --full to backfill or fix the cursor node."
            )
            return GitKgSyncResult(
                0,
                initialized_cursor=False,
                cursor_sha=last_sha,
                dry_run=dry_run,
                warning=warning,
            )

        chain_chrono = list(reversed(chain))
        processed = 0

        for commit in chain_chrono:
            if dry_run:
                processed += 1
                continue

            files = _list_touched_files(repo, commit)
            short = commit.hexsha[:7]
            msg = commit.message or ""
            first_line = msg.strip().splitlines()[0] if msg.strip() else ""
            cid = f"{COMMIT_NODE_PREFIX}{commit.hexsha}"
            data: dict[str, Any] = {
                "hexsha": commit.hexsha,
                "author": str(commit.author),
                "committed_date": commit.committed_datetime.isoformat(),
                "parents": [p.hexsha for p in commit.parents],
                "files": files,
                "repo_root": str(git_root),
            }
            kg.upsert_node(
                cid,
                "git_commit",
                name=short,
                summary=first_line[:500],
                status="ingested",
                data=data,
            )
            for fi in files:
                fp = fi.get("path") or ""
                if not fp:
                    continue
                fid = f"file:{fp}"
                kg.upsert_node(fid, "file", name=fp, summary="")
                kg.add_edge(cid, fid, "touches", {"status": fi.get("status", "")})

            for did in _directive_ids_in_text(msg):
                dn = kg.get_node(did)
                if dn and dn.get("type") == "directive":
                    kg.add_edge(cid, did, "references")

            processed += 1

        new_cursor = head.hexsha
        if not dry_run and processed:
            cursor_data = {
                "last_processed_sha": new_cursor,
                "last_processed_iso": head.committed_datetime.isoformat(),
                "repo_root": str(git_root),
            }
            kg.upsert_node(
                CURSOR_NODE_ID,
                CURSOR_NODE_TYPE,
                name="git_sync_cursor",
                summary="Git → KG sync cursor",
                status="active",
                data=cursor_data,
            )

        return GitKgSyncResult(
            processed,
            initialized_cursor=False,
            cursor_sha=new_cursor if not dry_run and processed else last_sha or head.hexsha,
            dry_run=dry_run,
            warning=warning,
        )
    finally:
        kg.close()


def should_run_git_kg_sync_from_env() -> bool:
    import os

    v = (os.environ.get("METAHARNESS_GIT_KG_SYNC") or "").strip().lower()
    return v in ("1", "true", "yes", "on")
