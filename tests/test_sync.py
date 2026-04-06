from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from meta_harness.cli import main
from meta_harness.config import load_config
from meta_harness.knowledge_graph import KnowledgeGraph
from meta_harness import memory


@pytest.fixture
def sync_project(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    return tmp_path


def _latest_cycle_json(cycle_dir: Path) -> dict:
    files = sorted(cycle_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    assert files, f"no json in {cycle_dir}"
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_sync_writes_cycle_json_maintenance(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(sync_project),
            "--id",
            "D_sync_maint",
            "--title",
            "Maintenance sync",
            "--layer",
            "maintenance",
        ],
    )
    assert r.exit_code == 0, r.output
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.maintenance_cycles_dir)
    assert data["directive"] == "D_sync_maint"
    assert data["status"] == "COMPLETED"
    assert data["layer"] == "maintenance"


def test_sync_writes_cycle_json_product(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(sync_project),
            "--id",
            "P_sync_prod",
            "--title",
            "Product sync",
            "--layer",
            "product",
        ],
    )
    assert r.exit_code == 0, r.output
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.product_cycles_dir)
    assert data["directive"] == "P_sync_prod"
    assert data["layer"] == "product"


def test_sync_status_val_defaults_to_completed(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["sync", "--dir", str(sync_project), "--id", "D_def", "--title", "Default status"],
    )
    assert r.exit_code == 0
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.maintenance_cycles_dir)
    assert data["status"] == "COMPLETED"


def test_sync_delta_computed_when_both_metrics_given(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(sync_project),
            "--id",
            "D_delta",
            "--title",
            "Delta",
            "--pre-metric",
            "0.9",
            "--post-metric",
            "1.0",
        ],
    )
    assert r.exit_code == 0
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.maintenance_cycles_dir)
    assert data["delta"] == pytest.approx(0.1)


def test_sync_delta_none_when_metrics_omitted(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["sync", "--dir", str(sync_project), "--id", "D_nd", "--title", "No delta"],
    )
    assert r.exit_code == 0
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.maintenance_cycles_dir)
    assert data["delta"] is None


def test_sync_files_parsed_from_comma_string(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(sync_project),
            "--id",
            "D_files",
            "--title",
            "Files",
            "--files",
            "foo.py,bar.py, baz.py",
        ],
    )
    assert r.exit_code == 0
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.maintenance_cycles_dir)
    assert data["changes_applied"] == 3


def test_sync_empty_files_produces_zero_changes(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["sync", "--dir", str(sync_project), "--id", "D_zero", "--title", "Zero files"],
    )
    assert r.exit_code == 0
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.maintenance_cycles_dir)
    assert data["changes_applied"] == 0


def test_sync_updates_kg_node(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(sync_project),
            "--id",
            "D_kg",
            "--title",
            "KG node",
            "--status",
            "TEST_FAILED",
        ],
    )
    assert r.exit_code == 0
    cfg = load_config(sync_project)
    kg = KnowledgeGraph(cfg.kg_path)
    try:
        node = kg.get_node("D_kg")
        assert node is not None
        assert node["status"] == "TEST_FAILED"
    finally:
        kg.close()


def test_sync_updates_memory_json(sync_project: Path):
    cfg = load_config(sync_project)
    mem_before = memory.load(cfg.memory_dir)
    tc_before = mem_before.total_cycles

    runner = CliRunner()
    r = runner.invoke(
        main,
        ["sync", "--dir", str(sync_project), "--id", "D_mem", "--title", "Memory"],
    )
    assert r.exit_code == 0

    mem_after = memory.load(cfg.memory_dir)
    assert mem_after.total_cycles == tc_before + 1
    ids = [d["id"] for d in mem_after.directives]
    assert "D_mem" in ids


def test_sync_synced_flag_in_json(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["sync", "--dir", str(sync_project), "--id", "D_flag", "--title", "Flag"],
    )
    assert r.exit_code == 0
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.maintenance_cycles_dir)
    assert data["synced"] is True


def test_sync_note_stored_in_json(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(sync_project),
            "--id",
            "D_note",
            "--title",
            "Note title",
            "--note",
            "human fix",
        ],
    )
    assert r.exit_code == 0
    cfg = load_config(sync_project)
    data = _latest_cycle_json(cfg.maintenance_cycles_dir)
    assert data["note"] == "human fix"


def test_sync_missing_required_options_exits_nonzero(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(main, ["sync", "--dir", str(sync_project), "--title", "No id"])
    assert r.exit_code != 0


def test_sync_missing_title_exits_nonzero(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(main, ["sync", "--dir", str(sync_project), "--id", "D_only"])
    assert r.exit_code != 0


def test_sync_invalid_status_exits_nonzero(sync_project: Path):
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(sync_project),
            "--id",
            "D_bad",
            "--title",
            "Bad",
            "--status",
            "BOGUS",
        ],
    )
    assert r.exit_code != 0


def test_sync_appears_in_status_output(sync_project: Path):
    runner = CliRunner()
    rid = "D_status_show"
    r = runner.invoke(
        main,
        ["sync", "--dir", str(sync_project), "--id", rid, "--title", "Show in status"],
    )
    assert r.exit_code == 0
    r2 = runner.invoke(main, ["status", "--dir", str(sync_project)])
    assert r2.exit_code == 0
    out = (r2.output or "") + (getattr(r2, "stderr", "") or "")
    assert rid in out


def test_sync_product_appears_in_product_status_output(sync_project: Path):
    runner = CliRunner()
    rid = "P_prod_show"
    r = runner.invoke(
        main,
        [
            "sync",
            "--dir",
            str(sync_project),
            "--id",
            rid,
            "--title",
            "Product show",
            "--layer",
            "product",
        ],
    )
    assert r.exit_code == 0
    r2 = runner.invoke(main, ["product", "status", "--dir", str(sync_project)])
    assert r2.exit_code == 0
    out = (r2.output or "") + (getattr(r2, "stderr", "") or "")
    assert rid in out


def test_sync_same_directive_id_upserts_kg_not_duplicated(sync_project: Path):
    runner = CliRunner()
    did = "D_idempotent"
    titles = ("First", "Second")
    for i, title in enumerate(titles):
        args = ["sync", "--dir", str(sync_project), "--id", did, "--title", title]
        if i > 0:
            args.append("--force")
        r = runner.invoke(main, args)
        assert r.exit_code == 0

    cfg = load_config(sync_project)
    kg = KnowledgeGraph(cfg.kg_path)
    try:
        node = kg.get_node(did)
        assert node is not None
        assert node["name"] == "Second"
    finally:
        kg.close()
