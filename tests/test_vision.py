from __future__ import annotations

import json
from pathlib import Path
import pytest
from click.testing import CliRunner

import meta_harness.slack_integration as si
import meta_harness.vision as vision_mod
from meta_harness.cli import main
from meta_harness.config import HarnessConfig, load_config
from meta_harness.knowledge_graph import KnowledgeGraph


def _minimal_toml(extra: str = "") -> str:
    return (
        "[cycle]\nmin_evidence_items = 1\n"
        "[vision]\n"
        'statement = "We build the best harness"\n'
        "target_users = \"devs\"\n"
        "features_wanted = [\"Feature A\"]\n"
        + extra
    )


@pytest.fixture
def cfg_with_vision(tmp_path: Path) -> HarnessConfig:
    (tmp_path / "metaharness.toml").write_text(_minimal_toml(), encoding="utf-8")
    return load_config(tmp_path)


@pytest.fixture
def kg(tmp_path: Path) -> KnowledgeGraph:
    return KnowledgeGraph(tmp_path / "kg.db")


def test_seed_vision_writes_kg_node(cfg_with_vision: HarnessConfig, kg: KnowledgeGraph):
    vision_mod.seed_vision(kg, cfg_with_vision)
    n = kg.get_node(vision_mod.VISION_NODE_ID)
    assert n is not None
    assert n["data"]["statement"] == "We build the best harness"


def test_seed_vision_is_idempotent(cfg_with_vision: HarnessConfig, kg: KnowledgeGraph):
    vision_mod.seed_vision(kg, cfg_with_vision)
    vision_mod.seed_vision(kg, cfg_with_vision)
    assert kg.get_node(vision_mod.VISION_NODE_ID)["data"]["evolution_count"] == 0


def test_derive_features_done_returns_completed_product_directives(kg: KnowledgeGraph):
    kg.upsert_node(
        "P001_auto",
        "directive",
        name="Prod one",
        status="COMPLETED",
        data={"layer": "product"},
    )
    kg.upsert_node(
        "P002_auto",
        "directive",
        name="Prod two",
        status="COMPLETED",
        data={"layer": "product"},
    )
    kg.upsert_node(
        "M010_auto",
        "directive",
        name="Maint",
        status="COMPLETED",
        data={"layer": "maintenance"},
    )
    got = vision_mod.derive_features_done(kg)
    assert "Prod one" in got and "Prod two" in got
    assert "Maint" not in got


def test_derive_features_done_excludes_failed(kg: KnowledgeGraph):
    kg.upsert_node(
        "P099_auto",
        "directive",
        name="Failed product",
        status="TEST_FAILED",
        data={"layer": "product"},
    )
    assert vision_mod.derive_features_done(kg) == []


def test_derive_out_of_scope_returns_vetoed_titles(kg: KnowledgeGraph):
    kg.upsert_node("P001_auto", "directive", name="No thanks", status="VETOED", data={})
    kg.upsert_node("P002_auto", "directive", name="Also no", status="VETOED", data={})
    got = vision_mod.derive_out_of_scope(kg)
    assert "Previously vetoed: No thanks" in got
    assert "Previously vetoed: Also no" in got


def test_derive_research_influences_reads_queue(cfg_with_vision: HarnessConfig):
    cfg_with_vision.research_dir.mkdir(parents=True, exist_ok=True)
    cfg_with_vision.research_queue_path.write_text(
        json.dumps(
            [
                {"title": "Paper A", "applicable_to": "metrics"},
                {"title": "Paper B", "applicable_to": "UX"},
            ]
        ),
        encoding="utf-8",
    )
    got = vision_mod.derive_research_influences(cfg_with_vision)
    assert "Research: Paper A -> metrics" in got
    assert "Research: Paper B -> UX" in got


def test_evolve_vision_removes_covered_features(cfg_with_vision: HarnessConfig, kg: KnowledgeGraph):
    vision_mod.seed_vision(kg, cfg_with_vision)
    v = vision_mod.load_vision(kg)
    data = dict(v)
    data["features_wanted"] = ["Dashboard live updates"]
    stmt = str(data.get("statement", "") or "")
    kg.upsert_node(
        vision_mod.VISION_NODE_ID,
        "vision",
        name="Product Vision",
        summary=stmt[:200],
        status="active",
        data=data,
    )
    kg.upsert_node(
        "P050_auto",
        "directive",
        name="Dashboard live updates via SSE",
        status="COMPLETED",
        data={"layer": "product"},
    )
    vision_mod.evolve_vision(kg, cfg_with_vision)
    assert "Dashboard live updates" not in vision_mod.load_vision(kg)["features_wanted"]


def test_evolve_vision_adds_research_to_wanted(cfg_with_vision: HarnessConfig, kg: KnowledgeGraph):
    vision_mod.seed_vision(kg, cfg_with_vision)
    cfg_with_vision.research_queue_path.write_text(
        json.dumps(
            [
                {
                    "title": "Agent confidence scoring",
                    "applicable_to": "diagnostics",
                    "url": "https://example.com/p1",
                }
            ]
        ),
        encoding="utf-8",
    )
    vision_mod.evolve_vision(kg, cfg_with_vision)
    wanted = vision_mod.load_vision(kg)["features_wanted"]
    assert any("Agent confidence scoring" in w for w in wanted)


def test_evolve_vision_increments_evolution_count(cfg_with_vision: HarnessConfig, kg: KnowledgeGraph):
    vision_mod.seed_vision(kg, cfg_with_vision)
    for _ in range(3):
        vision_mod.evolve_vision(kg, cfg_with_vision)
    assert vision_mod.load_vision(kg)["evolution_count"] == 3


def test_evolve_vision_adds_informed_by_edge(cfg_with_vision: HarnessConfig, kg: KnowledgeGraph):
    vision_mod.seed_vision(kg, cfg_with_vision)
    kg.upsert_node("P007_auto", "directive", name="Done", status="COMPLETED", data={"layer": "product"})
    vision_mod.evolve_vision(kg, cfg_with_vision, completed_directive_id="P007_auto")
    es = kg.get_edges(src_id=vision_mod.VISION_NODE_ID, relation="informed_by")
    assert any(e.dst_id == "P007_auto" for e in es)


def test_vision_prompt_block_reads_from_kg(cfg_with_vision: HarnessConfig, kg: KnowledgeGraph):
    vision_mod.seed_vision(kg, cfg_with_vision)
    out = vision_mod.vision_prompt_block(kg, cfg_with_vision)
    assert "## Product vision" in out
    assert "We build the best harness" in out


def test_vision_prompt_block_falls_back_to_toml_when_kg_empty(cfg_with_vision: HarnessConfig, kg: KnowledgeGraph):
    out = vision_mod.vision_prompt_block(kg, cfg_with_vision)
    assert "## Product vision" in out
    assert "We build the best harness" in out


def test_cli_vision_show(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(_minimal_toml(), encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(main, ["vision", "evolve", "--dir", str(tmp_path)]).exit_code == 0
    r2 = runner.invoke(main, ["vision", "show", "--dir", str(tmp_path)])
    assert r2.exit_code == 0
    assert "Product vision" in (r2.output or "")


def test_cli_vision_evolve(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(_minimal_toml(), encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(main, ["vision", "evolve", "--dir", str(tmp_path)])
    assert r.exit_code == 0
    out = (r.output or "") + (getattr(r, "stderr", "") or "")
    assert "evolved" in out.lower()


def test_slack_vision_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "metaharness.toml").write_text(
        '[project]\nname = "proj"\n[cycle]\nveto_seconds = 120\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)

    def fake_load(_kg):
        return {
            "statement": "S" * 400,
            "features_wanted": ["a", "b"],
            "evolution_count": 2,
        }

    monkeypatch.setattr("meta_harness.vision.load_vision", fake_load)
    monkeypatch.setattr("meta_harness.vision.derive_features_done", lambda _kg: ["x"])

    out = si.handle_slash_command(cfg, "vision", "")
    assert "Product Vision" in out
