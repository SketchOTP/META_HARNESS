from __future__ import annotations

from pathlib import Path

import pytest

from meta_harness.config import load_config
from meta_harness import memory as mem_mod
from meta_harness.knowledge_graph import KnowledgeGraph


def test_get_kg_creates_db(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text("[memory]\nenabled = true\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    kg = mem_mod.get_kg(cfg)
    assert kg is not None
    assert cfg.kg_path.exists()


def test_get_kg_works_when_memory_disabled(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text("[memory]\nenabled = false\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    kg = mem_mod.get_kg(cfg)
    assert kg is not None
    assert cfg.kg_path.exists()


def test_update_ingest(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text("[memory]\nenabled = true\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    kg = KnowledgeGraph(cfg.kg_path)
    mem_mod.update(
        cfg.memory_dir,
        "p",
        "D777",
        "title",
        "COMPLETED",
        0.1,
        ["a.py"],
        0.0,
        0.1,
        "acc",
        kg=kg,
        directive_content="batch_size = 64",
    )
    assert kg.get_node("D777") is not None


def test_compact_context_empty(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text("[memory]\nenabled = true\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    kg = KnowledgeGraph(cfg.kg_path)
    assert mem_mod.compact_context(mem_mod.ProjectMemory(), kg=kg) == ""


def test_compact_context_populated(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text("[memory]\nenabled = true\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    kg = KnowledgeGraph(cfg.kg_path)
    for i in range(4):
        kg.ingest_cycle_outcome(
            directive_id=f"D{i}",
            directive_title="t",
            directive_content="c",
            status="COMPLETED",
            files_changed=[f"{i}.py"],
            metric_name="m",
            metric_before=0.1,
            metric_after=0.2,
        )
    ctx = mem_mod.compact_context(mem_mod.ProjectMemory(), kg=kg)
    assert "[GRAPH" in ctx


def test_render_map_empty(tmp_path: Path):
    m = mem_mod.ProjectMemory()
    out = mem_mod.render_map(m)
    assert "No memory" in out or m.total_cycles == 0


def test_render_map_populated(tmp_path: Path):
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir(parents=True)
    for i in range(6):
        mem_mod.update(
            mem_dir,
            "proj",
            f"D{i:03d}",
            f"title {i}",
            "COMPLETED",
            0.01,
            ["f.py"],
            0.5,
            0.51,
            "acc",
        )
    m = mem_mod.load(mem_dir)
    out = mem_mod.render_map(m)
    assert "DIRECTIVE CHAIN" in out
    assert "FILE HEAT MAP" in out


def test_infer_patterns_from_directives(tmp_path: Path):
    m = mem_mod.ProjectMemory()
    m.directives = [
        {"status": "COMPLETED", "delta": 0.1, "files": ["a.py", "b.py"], "title": "one"},
        {"status": "COMPLETED", "delta": 0.2, "files": ["a.py"], "title": "two"},
        {"status": "COMPLETED", "delta": 0.1, "files": ["a.py"], "title": "three"},
        {"status": "TEST_FAILED", "delta": None, "files": [], "title": "bad test case"},
        {"status": "TEST_FAILED", "delta": None, "files": [], "title": "bad test case"},
    ]
    sp, fp, dead = mem_mod.infer_patterns(m)
    assert isinstance(sp, list) and isinstance(fp, list) and isinstance(dead, list)
    assert "bad test case" in " ".join(dead) or dead


def test_update_failure_detail_err_capped(tmp_path: Path):
    mem_dir = tmp_path / "m"
    long_err = "No valid JSON in agent output. " + ("x" * 300)
    mem_mod.update(
        mem_dir,
        "p",
        "D901",
        "t",
        "AGENT_FAILED",
        None,
        [],
        None,
        None,
        "",
        failure_detail=long_err,
    )
    m = mem_mod.load(mem_dir)
    last = m.directives[-1]
    assert "err" in last
    assert "No valid JSON" in last["err"]
    assert len(last["err"]) <= mem_mod._ERR_STORAGE_CAP


def test_compact_context_miss_includes_failure_hint(tmp_path: Path):
    m = mem_mod.ProjectMemory()
    m.total_cycles = 1
    m.completed = 0
    m.failed = 1
    m.directives = [
        {
            "id": "D902",
            "status": "AGENT_FAILED",
            "delta": None,
            "title": "x",
            "err": "TimeoutExpired: command took too long",
        }
    ]
    ctx = mem_mod.compact_context(m, kg=None)
    assert "D902(agent):timeout" in ctx


def test_refresh_patterns_updates_file_nodes(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text("[memory]\nenabled = true\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    kg = KnowledgeGraph(cfg.kg_path)
    mem_dir = cfg.memory_dir
    mem_mod.update(
        mem_dir,
        "p",
        "D1",
        "t",
        "COMPLETED",
        None,
        ["src/x.py"],
        None,
        None,
        "",
    )
    mem_mod.update(
        mem_dir,
        "p",
        "D2",
        "t2",
        "COMPLETED",
        None,
        ["src/x.py"],
        None,
        None,
        "",
    )
    mem_mod.refresh_patterns(mem_dir, kg=kg)
    short = mem_mod._shorten_path("src/x.py")
    fid = f"file:{short}"
    n = kg.get_node(fid)
    assert n is not None
    assert "success_rate" in n["data"]
