from __future__ import annotations

from pathlib import Path

import pytest


def _write_minimal_project(root: Path) -> None:
    (root / "metaharness.toml").write_text(
        "[cycle]\ninterval_seconds = 60\n",
        encoding="utf-8",
    )


def test_load_project_registry_none_when_missing(tmp_path: Path):
    from meta_harness.multi_project import load_project_registry

    assert load_project_registry(tmp_path) is None


def test_load_project_registry_valid_two_projects(tmp_path: Path):
    from meta_harness.multi_project import enabled_projects_in_order, load_project_registry

    cp = tmp_path / "cp"
    cp.mkdir()
    p1 = tmp_path / "proj1"
    p2 = tmp_path / "proj2"
    p1.mkdir()
    p2.mkdir()
    _write_minimal_project(p1)
    _write_minimal_project(p2)

    reg_file = cp / "metaharness-projects.toml"
    reg_file.write_text(
        f"""
[[projects]]
id = "a"
root = "{p1.as_posix()}"
enabled = true
label = "One"

[[projects]]
id = "b"
root = "{p2.as_posix()}"
enabled = false
""",
        encoding="utf-8",
    )

    reg = load_project_registry(cp, registry_file=reg_file)
    assert reg is not None
    assert reg.control_plane_root == cp.resolve()
    assert reg.registry_path == reg_file.resolve()
    en = enabled_projects_in_order(reg)
    assert len(en) == 1 and en[0].id == "a"


def test_load_project_registry_duplicate_id(tmp_path: Path):
    from meta_harness.multi_project import MultiProjectRegistryError, load_project_registry

    cp = tmp_path / "cp"
    cp.mkdir()
    p = tmp_path / "p"
    p.mkdir()
    _write_minimal_project(p)

    reg_file = cp / "metaharness-projects.toml"
    reg_file.write_text(
        f"""
[[projects]]
id = "x"
root = "{p.as_posix()}"

[[projects]]
id = "x"
root = "{p.as_posix()}"
""",
        encoding="utf-8",
    )

    with pytest.raises(MultiProjectRegistryError, match="duplicate"):
        load_project_registry(cp, registry_file=reg_file)


def test_load_project_registry_missing_metaharness_toml(tmp_path: Path):
    from meta_harness.multi_project import MultiProjectRegistryError, load_project_registry

    cp = tmp_path / "cp"
    cp.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()

    reg_file = cp / "metaharness-projects.toml"
    reg_file.write_text(
        f"""
[[projects]]
id = "z"
root = "{empty.as_posix()}"
""",
        encoding="utf-8",
    )

    with pytest.raises(MultiProjectRegistryError, match="metaharness.toml"):
        load_project_registry(cp, registry_file=reg_file)


def test_multi_project_daemon_invokes_run_cycle_per_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from meta_harness import daemon as dm
    from meta_harness.multi_project import load_project_registry

    cp = tmp_path / "cp"
    cp.mkdir()
    p1 = tmp_path / "proj1"
    p2 = tmp_path / "proj2"
    p1.mkdir()
    p2.mkdir()
    _write_minimal_project(p1)
    _write_minimal_project(p2)

    reg_file = cp / "metaharness-projects.toml"
    reg_file.write_text(
        f"""
[[projects]]
id = "first"
root = "{p1.as_posix()}"

[[projects]]
id = "second"
root = "{p2.as_posix()}"
""",
        encoding="utf-8",
    )

    reg = load_project_registry(cp, registry_file=reg_file)
    assert reg is not None

    roots_seen: list[Path] = []

    def fake_run_cycle(c):
        roots_seen.append(c.project_root.resolve())

    monkeypatch.setattr(dm, "run_cycle", fake_run_cycle)
    n = {"i": 0}

    def stop_after_round(*_a, **_k):
        n["i"] += 1
        if n["i"] >= 1:
            dm._running = False

    monkeypatch.setattr(dm, "_interruptible_sleep_between_multi_rounds", stop_after_round)
    monkeypatch.setattr(dm.console, "print", lambda *a, **k: None)
    dm._running = True
    dm.run_multi_project_daemon(reg, git_kg_sync_after_cycle=False)

    assert roots_seen == [p1.resolve(), p2.resolve()]


def test_find_registry_file_walks_up(tmp_path: Path):
    from meta_harness.multi_project import find_registry_file

    cp = tmp_path / "cp"
    cp.mkdir()
    (cp / "metaharness-projects.toml").write_text(
        '[[projects]]\nid="a"\nroot="."\n',
        encoding="utf-8",
    )
    nested = cp / "a" / "b"
    nested.mkdir(parents=True)

    assert find_registry_file(nested) == (cp / "metaharness-projects.toml").resolve()


def test_cli_daemon_explicit_projects_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from click.testing import CliRunner

    from meta_harness.cli import main

    cp = tmp_path / "cp"
    cp.mkdir()
    p1 = tmp_path / "proj1"
    p1.mkdir()
    _write_minimal_project(p1)

    reg_file = cp / "metaharness-projects.toml"
    reg_file.write_text(
        f"""
[[projects]]
id = "only"
root = "{p1.as_posix()}"
""",
        encoding="utf-8",
    )

    called: list[str] = []

    def fake_multi(reg, **kw):
        called.append("multi")

    monkeypatch.setattr("meta_harness.daemon.run_multi_project_daemon", fake_multi)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["daemon", "--projects-file", str(reg_file), "--dir", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert called == ["multi"]


def test_discover_project_registry_from_cli_helper(tmp_path: Path):
    from meta_harness.cli import _discover_project_registry

    cp = tmp_path / "cp"
    cp.mkdir()
    p1 = tmp_path / "proj1"
    p1.mkdir()
    _write_minimal_project(p1)

    (cp / "metaharness-projects.toml").write_text(
        f"""
[[projects]]
id = "only"
root = "{p1.as_posix()}"
""",
        encoding="utf-8",
    )

    reg = _discover_project_registry(cp / "nested")
    assert reg is not None
    assert len(reg.projects) == 1
