from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meta_harness.agent import (
    FileChange,
    PlannedChange,
    Plan,
    _apply_changes,
    _build_execute_prompt_with_scope,
    _extract_files_from_log,
    _format_phase_error,
    _is_protected,
    _is_readable,
    _load_analyze_files,
    _load_planned_files,
    _order_analyze_paths,
    _path_mentioned_in_directive,
    _save_reasoning,
)
from meta_harness.config import HarnessConfig, ScopeConfig, load_config
from meta_harness.cursor_client import CursorResponse
from meta_harness.cycle import run_cycle
from meta_harness.proposer import Directive
import meta_harness.agent as agent_mod


def test_smoke_imports():
    assert callable(run_cycle)
    assert callable(load_config)


def test_format_phase_error_idempotent_when_already_tagged():
    s = "[phase:analyze] already"
    assert _format_phase_error("execute", s) == s


def test_run_surfaces_tagged_cursor_error_with_phase_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = _minimal_cfg(tmp_path)
    d = _directive(tmp_path)
    tagged = "[agent_fail:cli_nonzero] boom"

    def fake_agent_call(*a, **k):
        return CursorResponse(
            success=False,
            failure_kind="CLI_NONZERO",
            error=tagged,
            raw="",
        )

    monkeypatch.setattr(agent_mod.cursor_client, "agent_call", fake_agent_call)
    out = agent_mod.run(cfg, d, cycle_id="cid")
    assert out.success is False
    assert out.error.startswith("[phase:execute]")
    assert "[agent_fail:cli_nonzero]" in out.error


def test_build_execute_prompt_includes_protected_and_body(tmp_path: Path):
    cfg = _minimal_cfg(tmp_path)
    d = _directive(tmp_path)
    p = _build_execute_prompt_with_scope(d, cfg.scope.protected)
    assert "Protected paths" in p
    assert "metaharness.toml" in p
    assert "do thing" in p or "D" in p


def test_extract_files_from_log_finds_paths():
    raw = "Updated `src/foo.py` and also tests/test_x.py for coverage."
    paths = _extract_files_from_log(raw)
    assert "src/foo.py" in paths
    assert "tests/test_x.py" in paths


def test_run_success_saves_reasoning_and_sets_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = _minimal_cfg(tmp_path)
    d = _directive(tmp_path)
    log = "Edited `hello.py` successfully."

    def fake_agent_call(*a, **k):
        return CursorResponse(success=True, raw=log)

    monkeypatch.setattr(agent_mod.cursor_client, "agent_call", fake_agent_call)
    out = agent_mod.run(cfg, d, cycle_id="cid")
    assert out.success is True
    assert out.phases_completed == 1
    assert any(c.path == "hello.py" for c in out.changes)
    rf = cfg.harness_dir / "reasoning" / "cid_execute.md"
    assert rf.is_file()
    assert "yolo" in rf.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "rel_path,protected,expected",
    [
        ("foo.py", ["metaharness.toml"], False),
        ("metaharness.toml", ["metaharness.toml"], True),
        (".git/config", [".git/**"], True),
        ("src/a.py", ["*.py"], True),
        ("src/a.rs", ["*.py"], False),
    ],
)
def test_is_protected(rel_path: str, protected: list[str], expected: bool):
    assert _is_protected(rel_path, protected) is expected


@pytest.mark.parametrize(
    "rel_path,expected",
    [
        ("pkg/foo.py", True),
        (".metaharness/prompts/x.md", False),
        ("x/__pycache__/y.pyc", False),
        (".git/HEAD", False),
        ("node_modules/pkg/a.js", False),
        ("img.png", False),
        ("data.db", False),
        ("readme.md", True),
    ],
)
def test_is_readable(rel_path: str, expected: bool):
    assert _is_readable(rel_path) is expected


def test_load_planned_files_existing_and_new(tmp_path: Path):
    (tmp_path / "exists.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    plan = Plan(
        changes=[
            PlannedChange(action="write", path="exists.py"),
            PlannedChange(action="create", path="missing.py"),
            PlannedChange(action="delete", path="gone.py"),
            PlannedChange(action="write", path="subdir"),
        ]
    )
    out = _load_planned_files(tmp_path, plan)
    assert out["exists.py"] == "x = 1\n"
    assert out["missing.py"] == "(new file — does not exist yet)"
    assert "gone.py" not in out
    assert out["subdir"] == "(new file — does not exist yet)"


def test_load_planned_files_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = tmp_path / "locked.py"
    p.write_text("secret", encoding="utf-8")
    real_read = Path.read_text

    def fake_read_text(self: Path, *a, **kw):
        if self.name == "locked.py":
            raise OSError("denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    plan = Plan(changes=[PlannedChange(action="write", path="locked.py")])
    out = _load_planned_files(tmp_path, plan)
    assert out["locked.py"] == "(unreadable)"


def test_save_reasoning(tmp_path: Path):
    reasoning = tmp_path / ".metaharness" / "reasoning"
    _save_reasoning(reasoning, "c1", "execute", "body")
    f = reasoning / "c1_execute.md"
    assert f.is_file()
    assert f.read_text(encoding="utf-8") == "body"


def _minimal_cfg(root: Path) -> HarnessConfig:
    return HarnessConfig(
        project_root=root,
        scope=ScopeConfig(
            modifiable=["*.py"],
            protected=["metaharness.toml", ".git/**"],
        ),
    )


def _directive(root: Path) -> Directive:
    p = root / "d.md"
    p.write_text("---\nid: D\n---\n# do thing\n", encoding="utf-8")
    return Directive(id="D", path=p, title="t", content=p.read_text(encoding="utf-8"))


def test_apply_changes_write_and_delete(tmp_path: Path):
    cfg = _minimal_cfg(tmp_path)
    (tmp_path / "metaharness.toml").write_text("x", encoding="utf-8")
    target = tmp_path / "out" / "f.py"
    changes = [
        FileChange(action="write", path="out/f.py", content="y = 2\n"),
        FileChange(action="delete", path="gone.txt"),
    ]
    g = tmp_path / "gone.txt"
    g.write_text("z", encoding="utf-8")
    _apply_changes(cfg, changes)
    assert target.read_text(encoding="utf-8") == "y = 2\n"
    assert not g.exists()


def test_apply_changes_rejects_protected(tmp_path: Path):
    cfg = _minimal_cfg(tmp_path)
    with pytest.raises(PermissionError, match="protected"):
        _apply_changes(
            cfg,
            [FileChange(action="write", path="metaharness.toml", content="bad")],
        )


def test_apply_changes_rejects_outside_root(tmp_path: Path):
    cfg = _minimal_cfg(tmp_path)
    with pytest.raises(PermissionError, match="outside project root"):
        _apply_changes(
            cfg,
            [FileChange(action="write", path="../escape.py", content="")],
        )


def test_load_analyze_files_respects_scope_readable_and_noise(tmp_path: Path):
    (tmp_path / "ok.py").write_text("# ok\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("nope\n", encoding="utf-8")
    (tmp_path / ".metaharness").mkdir()
    (tmp_path / ".metaharness" / "x.md").write_text("skip", encoding="utf-8")
    cfg = HarnessConfig(
        project_root=tmp_path,
        scope=ScopeConfig(modifiable=["*.py"], protected=["metaharness.toml"]),
    )
    files = _load_analyze_files(tmp_path, cfg, directive_text="", git_recent_paths=[])
    assert "ok.py" in files
    assert "skip.txt" not in files
    assert ".metaharness/x.md" not in files


def test_load_analyze_files_includes_directive_mentioned_out_of_scope(tmp_path: Path):
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("hi\n", encoding="utf-8")
    cfg = HarnessConfig(
        project_root=tmp_path,
        scope=ScopeConfig(modifiable=["*.py"], protected=[]),
    )
    files = _load_analyze_files(
        tmp_path, cfg, directive_text="Edit note.txt please", git_recent_paths=[]
    )
    assert "a.py" in files
    assert "note.txt" in files


def test_path_mentioned_in_directive():
    assert _path_mentioned_in_directive("src/foo.py", "see src/foo.py ok")
    assert _path_mentioned_in_directive("deep/foo.py", "foo.py")
    assert not _path_mentioned_in_directive("a.py", "no refs here")


def test_order_analyze_paths_cap_prioritizes_mention_then_git_then_alpha():
    paths = ["z.py", "a.py", "b.py", "c.py"]
    git = ["c.py", "a.py"]
    out = _order_analyze_paths(
        paths, directive_text="fix z.py", git_recent_paths=git, max_files=2
    )
    assert out == ["z.py", "c.py"]


def test_order_analyze_paths_unlimited_sorted():
    out = _order_analyze_paths(
        ["b.py", "a.py"], directive_text="", git_recent_paths=[], max_files=0
    )
    assert out == ["a.py", "b.py"]


def test_load_analyze_fallback_when_nothing_matches_scope(tmp_path: Path):
    (tmp_path / "orphan.rs").write_text("x\n", encoding="utf-8")
    cfg = HarnessConfig(
        project_root=tmp_path,
        scope=ScopeConfig(modifiable=["*.py"], protected=[]),
    )
    files = _load_analyze_files(tmp_path, cfg, directive_text="", git_recent_paths=[])
    assert "orphan.rs" in files
