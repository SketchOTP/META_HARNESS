from __future__ import annotations

from pathlib import Path

import pytest

from meta_harness.knowledge_graph import KnowledgeGraph


@pytest.fixture
def kg(tmp_path: Path) -> KnowledgeGraph:
    return KnowledgeGraph(tmp_path / "kg.db")


def test_upsert_and_get_node(kg: KnowledgeGraph):
    kg.upsert_node("n1", "directive", name="T", summary="S", status="pending", data={"k": 1})
    n = kg.get_node("n1")
    assert n is not None
    assert n["type"] == "directive"
    assert n["name"] == "T"
    assert n["summary"] == "S"
    assert n["status"] == "pending"
    assert n["data"]["k"] == 1


def test_upsert_updates_existing(kg: KnowledgeGraph):
    kg.upsert_node("x", "file", name="a", summary="old")
    kg.upsert_node("x", "file", name="a", summary="new")
    assert kg.get_node("x")["summary"] == "new"


def test_add_edge_dedupe(kg: KnowledgeGraph):
    kg.upsert_node("a", "directive")
    kg.upsert_node("b", "file")
    kg.add_edge("a", "b", "modified")
    kg.add_edge("a", "b", "modified")
    assert len(kg.get_edges(src_id="a", relation="modified")) == 1


def test_add_edge_no_dedupe(kg: KnowledgeGraph):
    kg.upsert_node("a", "directive")
    kg.upsert_node("b", "file")
    kg.add_edge("a", "b", "tag", dedupe=False)
    kg.add_edge("a", "b", "tag", dedupe=False)
    assert len(kg.get_edges(src_id="a", relation="tag")) == 2


def test_get_edges_src_filter(kg: KnowledgeGraph):
    kg.upsert_node("s", "directive")
    kg.upsert_node("t1", "file")
    kg.upsert_node("t2", "file")
    kg.add_edge("s", "t1", "modified")
    kg.add_edge("s", "t2", "modified")
    es = kg.get_edges(src_id="s")
    assert len(es) == 2


def test_get_edges_relation_filter(kg: KnowledgeGraph):
    kg.upsert_node("a", "directive")
    kg.upsert_node("b", "file")
    kg.add_edge("a", "b", "modified")
    kg.add_edge("a", "b", "broke", dedupe=False)
    assert len(kg.get_edges(relation="broke")) == 1


def test_neighbors_out(kg: KnowledgeGraph):
    kg.upsert_node("a", "directive")
    kg.upsert_node("b", "file")
    kg.add_edge("a", "b", "modified")
    assert kg.neighbors("a", direction="out") == ["b"]
    assert kg.neighbors("b", direction="out") == []


def test_neighbors_in(kg: KnowledgeGraph):
    kg.upsert_node("a", "directive")
    kg.upsert_node("b", "file")
    kg.add_edge("a", "b", "modified")
    assert kg.neighbors("b", direction="in") == ["a"]


def test_neighbors_both(kg: KnowledgeGraph):
    kg.upsert_node("a", "directive")
    kg.upsert_node("b", "file")
    kg.upsert_node("c", "file")
    kg.add_edge("a", "b", "modified")
    kg.add_edge("c", "a", "imports")
    nba = set(kg.neighbors("a", direction="both"))
    assert nba == {"b", "c"}


def test_subgraph_depth1(kg: KnowledgeGraph):
    kg.upsert_node("root", "directive")
    kg.upsert_node("n1", "file")
    kg.upsert_node("n2", "file")
    kg.add_edge("root", "n1", "modified")
    kg.add_edge("n1", "n2", "imports")
    sg = kg.subgraph("root", depth=1)
    assert sg == {"root", "n1"}


def test_subgraph_depth2(kg: KnowledgeGraph):
    kg.upsert_node("root", "directive")
    kg.upsert_node("n1", "file")
    kg.upsert_node("n2", "file")
    kg.add_edge("root", "n1", "modified")
    kg.add_edge("n1", "n2", "imports")
    sg = kg.subgraph("root", depth=2)
    assert "n2" in sg


def test_causal_chain(kg: KnowledgeGraph):
    kg.upsert_node("A", "directive")
    kg.upsert_node("B", "test")
    kg.upsert_node("C", "test")
    kg.add_edge("A", "B", "broke")
    kg.add_edge("B", "C", "fixed")
    chain = kg.causal_chain("A")
    assert chain[0] == "A"
    assert "B" in chain


def test_file_history(kg: KnowledgeGraph):
    kg.ingest_cycle_outcome(
        directive_id="D001",
        directive_title="t",
        directive_content="x",
        status="COMPLETED",
        files_changed=["src/a.py"],
    )
    kg.ingest_cycle_outcome(
        directive_id="D002",
        directive_title="t2",
        directive_content="y",
        status="COMPLETED",
        files_changed=["src/a.py"],
    )
    h = kg.file_history("src/a.py")
    assert len(h) == 2
    assert {x["src_id"] for x in h} == {"D001", "D002"}


def test_entity_history_changed(kg: KnowledgeGraph):
    kg.ingest_cycle_outcome(
        directive_id="D010",
        directive_title="cfg",
        directive_content='Set batch_size = 32\nand "lr": 0.01',
        status="COMPLETED",
        files_changed=[],
    )
    eid = "entity:config_key:batch_size"
    hist = kg.entity_history(eid)
    assert any(h.get("value") == "32" for h in hist)


def test_full_text_search(kg: KnowledgeGraph):
    kg.upsert_node("u1", "directive", name="alpha uniquewolf", summary="one")
    kg.upsert_node("u2", "directive", name="beta other", summary="two")
    ids = kg.search("uniquewolf")
    assert ids and ids[0] == "u1"


def test_stats(kg: KnowledgeGraph):
    kg.upsert_node("a", "directive")
    kg.upsert_node("b", "file")
    kg.add_edge("a", "b", "modified")
    st = kg.stats()
    assert st["nodes"] >= 2
    assert st["edges"] >= 1
    assert "directive" in st["node_types"]


def test_most_connected(kg: KnowledgeGraph):
    kg.upsert_node("hub", "directive")
    for i in range(3):
        nid = f"f{i}"
        kg.upsert_node(nid, "file")
        kg.add_edge("hub", nid, "modified")
    top = kg.most_connected(3)
    assert top[0] == "hub"


def test_extract_entities():
    text = """
    D042_auto
    class MyModel
    learning_rate = 0.01
    "dropout": 0.1
    """
    ents = KnowledgeGraph.extract_entities(text)
    types = {e["type"] for e in ents}
    assert "directive" in types
    assert "class" in types
    assert "config_key" in types


def test_ingest_cycle_outcome_complete(kg: KnowledgeGraph):
    kg.ingest_cycle_outcome(
        directive_id="D099",
        directive_title="done",
        directive_content="ok",
        status="COMPLETED",
        files_changed=["x.py"],
        metric_name="accuracy",
        metric_before=0.5,
        metric_after=0.9,
    )
    assert kg.get_node("D099") is not None
    assert kg.get_node("file:x.py") is not None
    assert kg.get_node("metric:accuracy") is not None
    assert len(kg.get_edges(src_id="D099", relation="modified")) == 1
    assert len(kg.get_edges(src_id="D099", relation="improved")) == 1


def test_ingest_cycle_outcome_test_failed(kg: KnowledgeGraph):
    kg.ingest_cycle_outcome(
        directive_id="D100",
        directive_title="bad",
        directive_content="x",
        status="TEST_FAILED",
        files_changed=["a.py"],
        failing_tests=["test_foo"],
    )
    assert len(kg.get_edges(src_id="D100", relation="broke")) == 1
    assert len(kg.get_edges(src_id="D100", relation="improved")) == 0


def test_ingest_cycle_outcome_failure_detail(kg: KnowledgeGraph):
    kg.ingest_cycle_outcome(
        directive_id="D101",
        directive_title="agent oops",
        directive_content="body",
        status="AGENT_FAILED",
        files_changed=[],
        failure_detail="No valid JSON in agent output",
    )
    n = kg.get_node("D101")
    assert n is not None
    assert n["data"].get("failure_hint") == "no_json"
    assert "error_excerpt" in n["data"]
    assert "No valid JSON" in n["data"]["error_excerpt"]
    assert "no_json" in (n["summary"] or "")


def test_build_compact_context_empty(kg: KnowledgeGraph):
    assert kg.build_compact_context() == ""


def test_build_compact_context_populated(kg: KnowledgeGraph):
    for i in range(6):
        kg.ingest_cycle_outcome(
            directive_id=f"D{i:03d}",
            directive_title="t",
            directive_content="c",
            status="COMPLETED",
            files_changed=[f"f{i}.py"],
            metric_name="m",
            metric_before=0.1,
            metric_after=0.2,
        )
    ctx = kg.build_compact_context()
    assert "[GRAPH" in ctx
    assert "hot:" in ctx
    assert "wins:" in ctx


def test_ingest_stores_layer_and_get_nodes_by_layer(kg: KnowledgeGraph):
    kg.ingest_cycle_outcome(
        directive_id="M001_auto",
        directive_title="maint",
        directive_content="x",
        status="COMPLETED",
        files_changed=[],
        layer="maintenance",
    )
    kg.ingest_cycle_outcome(
        directive_id="P001_auto",
        directive_title="feat",
        directive_content="y",
        status="COMPLETED",
        files_changed=[],
        layer="product",
    )
    pm = kg.get_nodes_by_layer("product", limit=10)
    assert len(pm) == 1 and pm[0]["id"] == "P001_auto"
    mm = kg.get_nodes_by_layer("maintenance", limit=10)
    assert any(n["id"] == "M001_auto" for n in mm)


def test_build_cross_layer_context(tmp_path: Path):
    from meta_harness.knowledge_graph import KnowledgeGraph, build_cross_layer_context

    p = tmp_path / "kg.db"
    kg = KnowledgeGraph(p)
    kg.ingest_cycle_outcome(
        directive_id="P010_auto",
        directive_title="Roadmap item",
        directive_content="c",
        status="COMPLETED",
        files_changed=[],
        layer="product",
    )
    ctx = build_cross_layer_context(kg, "maintenance")
    assert "P010" in ctx or "Roadmap" in ctx
