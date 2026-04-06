from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meta_harness.config import load_config
from meta_harness.git_kg_sync import (
    COMMIT_NODE_PREFIX,
    CURSOR_NODE_ID,
    sync_git_to_kg,
)
from meta_harness.knowledge_graph import KnowledgeGraph


def _git_config(repo: Path) -> None:
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _git_config(repo)


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _write_config(repo: Path) -> None:
    (repo / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _init_repo(root)
    _write_config(root)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(root, "init a")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    _commit(root, "add b")
    return root


def test_first_run_initializes_cursor_no_backfill(git_project: Path):
    cfg = load_config(git_project)
    r = sync_git_to_kg(cfg)
    assert r.initialized_cursor is True
    assert r.commits_processed == 0

    kg = KnowledgeGraph(cfg.kg_path)
    try:
        cur = kg.get_node(CURSOR_NODE_ID)
        assert cur is not None
        assert cur["data"]["last_processed_sha"] == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert kg.stats()["node_types"].get("git_commit", 0) == 0
    finally:
        kg.close()


def test_incremental_ingests_new_commit_idempotent(git_project: Path):
    cfg = load_config(git_project)
    assert sync_git_to_kg(cfg).initialized_cursor is True

    (git_project / "c.txt").write_text("c\n", encoding="utf-8")
    _commit(git_project, "add c")

    r2 = sync_git_to_kg(cfg)
    assert r2.commits_processed == 1

    kg = KnowledgeGraph(cfg.kg_path)
    try:
        heads = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        cid = f"{COMMIT_NODE_PREFIX}{heads}"
        node = kg.get_node(cid)
        assert node is not None
        assert node["type"] == "git_commit"
        paths = [f.get("path") for f in node["data"].get("files", [])]
        assert "c.txt" in paths

        r3 = sync_git_to_kg(cfg)
        assert r3.commits_processed == 0
    finally:
        kg.close()


def test_full_backfill_ingests_history(git_project: Path):
    cfg = load_config(git_project)
    r = sync_git_to_kg(cfg, full=True, max_commits=20)
    assert r.commits_processed >= 2

    kg = KnowledgeGraph(cfg.kg_path)
    try:
        stats = kg.stats()
        assert stats["node_types"].get("git_commit", 0) >= 2
    finally:
        kg.close()


def test_directive_reference_edge_when_directive_in_kg(git_project: Path):
    cfg = load_config(git_project)
    sync_git_to_kg(cfg)

    kg = KnowledgeGraph(cfg.kg_path)
    try:
        kg.upsert_node(
            "P009_auto",
            "directive",
            name="Test",
            summary="s",
            status="pending",
            data={"title": "t"},
        )
    finally:
        kg.close()

    (git_project / "d.txt").write_text("d\n", encoding="utf-8")
    _commit(git_project, "fix for P009_auto")

    sync_git_to_kg(cfg)

    kg = KnowledgeGraph(cfg.kg_path)
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        cid = f"{COMMIT_NODE_PREFIX}{head}"
        es = kg.get_edges(src_id=cid, relation="references")
        assert any(e.dst_id == "P009_auto" for e in es)
    finally:
        kg.close()


def test_dry_run_does_not_write(git_project: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(git_project)
    sync_git_to_kg(cfg)

    (git_project / "e.txt").write_text("e\n", encoding="utf-8")
    _commit(git_project, "e")

    kg_path = cfg.kg_path
    if kg_path.exists():
        mtime_before = kg_path.stat().st_mtime
    else:
        mtime_before = 0.0

    r = sync_git_to_kg(cfg, dry_run=True)
    assert r.commits_processed == 1
    if kg_path.exists():
        assert kg_path.stat().st_mtime == mtime_before


def test_cli_graph_sync_git_invokes(tmp_path: Path):
    from click.testing import CliRunner

    from meta_harness.cli import main

    root = tmp_path / "p"
    root.mkdir()
    _init_repo(root)
    _write_config(root)
    (root / "x.txt").write_text("x", encoding="utf-8")
    _commit(root, "x")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["graph", "sync-git", "--dir", str(root)],
    )
    assert result.exit_code == 0
    out = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "Initialized" in out or "cursor" in out.lower()


def test_should_run_git_kg_sync_from_env(monkeypatch: pytest.MonkeyPatch):
    from meta_harness.git_kg_sync import should_run_git_kg_sync_from_env

    monkeypatch.delenv("METAHARNESS_GIT_KG_SYNC", raising=False)
    assert should_run_git_kg_sync_from_env() is False
    monkeypatch.setenv("METAHARNESS_GIT_KG_SYNC", "1")
    assert should_run_git_kg_sync_from_env() is True
