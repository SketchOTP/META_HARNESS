from __future__ import annotations

import subprocess
from pathlib import Path

from meta_harness.agent import FileChange
from meta_harness.config import load_config
from meta_harness import rollback


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


def test_is_metric_regression_maximize(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        '[goals]\noptimization_direction = "maximize"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert rollback.is_metric_regression(cfg, 1.0, 0.5) is True
    assert rollback.is_metric_regression(cfg, 1.0, 1.0) is False
    assert rollback.is_metric_regression(cfg, 0.5, 1.0) is False


def test_is_metric_regression_minimize(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        '[goals]\noptimization_direction = "minimize"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert rollback.is_metric_regression(cfg, 1.0, 2.0) is True
    assert rollback.is_metric_regression(cfg, 2.0, 1.0) is False


def test_attempt_restore_skips_without_git(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text("[cycle]\nrollback_enabled = true\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    r = rollback.attempt_restore(
        cfg, [FileChange("write", "x.py", "")], kind="test_failure"
    )
    assert r.attempted is True
    assert r.succeeded is False
    assert "git" in r.detail.lower()


def test_attempt_restore_tracked_file_reverted(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nrollback_enabled = true\n", encoding="utf-8"
    )
    (tmp_path / "f.py").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "f.py").write_text("broken\n", encoding="utf-8")

    cfg = load_config(tmp_path)
    r = rollback.attempt_restore(
        cfg, [FileChange("write", "f.py", "")], kind="test_failure"
    )
    assert r.succeeded is True
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "original\n"


def test_attempt_restore_skips_ambiguous_extra_dirty(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nrollback_enabled = true\n", encoding="utf-8"
    )
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "a.py").write_text("dirty-a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("dirty-b\n", encoding="utf-8")

    cfg = load_config(tmp_path)
    r = rollback.attempt_restore(
        cfg, [FileChange("write", "b.py", "")], kind="test_failure"
    )
    assert r.attempted is True
    assert r.succeeded is False
    assert "outside" in r.detail
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dirty-b\n"


def test_require_git_false_allows_restore_with_extra_dirty(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nrollback_enabled = true\nrollback_require_git = false\n",
        encoding="utf-8",
    )
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "a.py").write_text("dirty-a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("dirty-b\n", encoding="utf-8")

    cfg = load_config(tmp_path)
    r = rollback.attempt_restore(
        cfg, [FileChange("write", "b.py", "")], kind="test_failure"
    )
    assert r.succeeded is True
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "b\n"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "dirty-a\n"


def test_attempt_restore_removes_untracked_new_file(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nrollback_enabled = true\n", encoding="utf-8"
    )
    (tmp_path / "base.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "new.py").write_text("ephemeral\n", encoding="utf-8")

    cfg = load_config(tmp_path)
    r = rollback.attempt_restore(
        cfg, [FileChange("write", "new.py", "")], kind="test_failure"
    )
    assert r.succeeded is True
    assert not (tmp_path / "new.py").exists()
