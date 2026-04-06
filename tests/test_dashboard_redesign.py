from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from meta_harness import dashboard as dash
from meta_harness.config import load_config
from meta_harness.knowledge_graph import KnowledgeGraph
from meta_harness.vision import VISION_NODE_ID


@pytest.fixture
def dash_project(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        "[project]\nname = \"dash-test\"\n[cycle]\nveto_seconds = 0\n"
        "min_evidence_items = 1\n[memory]\nenabled = true\n"
        "[evidence]\nmetrics_patterns = [\"metrics.json\"]\n"
        "[goals]\nprimary_metric = \"coverage_pct\"\n",
        encoding="utf-8",
    )
    h = tmp_path / ".metaharness"
    (h / "cycles" / "maintenance").mkdir(parents=True)
    (h / "cycles" / "product").mkdir(parents=True)
    (h / "memory").mkdir(parents=True)
    return tmp_path


def test_build_data_json_returns_valid_json(dash_project: Path) -> None:
    cfg = load_config(dash_project)
    (dash_project / "metrics.json").write_text("{}", encoding="utf-8")
    raw = dash.build_data_json(cfg)
    data = json.loads(raw)
    for key in (
        "metrics",
        "maintenance_cycles",
        "product_cycles",
        "kg_stats",
        "vision",
        "memory",
    ):
        assert key in data


def test_data_endpoint_returns_200(dash_project: Path) -> None:
    cfg = load_config(dash_project)
    (dash_project / "metrics.json").write_text("{}", encoding="utf-8")
    rt = dash.DashboardRuntime(cfg)
    rt.refresh_from_disk()
    handler = dash._dashboard_handler_factory(cfg, rt)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/data")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert "application/json" in (resp.headers.get("Content-Type") or "")
    finally:
        rt.close()
        server.shutdown()
        thread.join(timeout=2)


def test_dashboard_html_contains_tailwind(dash_project: Path) -> None:
    cfg = load_config(dash_project)
    html = dash.build_dashboard_html(cfg)
    assert "cdn.tailwindcss.com" in html


def test_dashboard_html_contains_chartjs(dash_project: Path) -> None:
    cfg = load_config(dash_project)
    html = dash.build_dashboard_html(cfg)
    assert "chart.umd.min.js" in html


def test_dashboard_html_contains_all_sections(dash_project: Path) -> None:
    cfg = load_config(dash_project)
    html = dash.build_dashboard_html(cfg)
    for sid in ("overview", "cycles", "metrics", "vision", "graph", "memory"):
        assert f'id="{sid}"' in html


def test_default_host_is_zero_zero() -> None:
    assert dash.DEFAULT_HOST == "0.0.0.0"


def test_data_endpoint_includes_vision(dash_project: Path) -> None:
    cfg = load_config(dash_project)
    (dash_project / "metrics.json").write_text("{}", encoding="utf-8")
    kg = KnowledgeGraph(cfg.kg_path)
    try:
        kg.upsert_node(
            VISION_NODE_ID,
            "vision",
            name="Product Vision",
            summary="S",
            data={"statement": "We ship quality harnesses."},
        )
    finally:
        kg.close()
    data = json.loads(dash.build_data_json(cfg))
    assert "statement" in data["vision"]
    assert data["vision"]["statement"] == "We ship quality harnesses."


def test_data_endpoint_includes_cycles(dash_project: Path) -> None:
    cfg = load_config(dash_project)
    (dash_project / "metrics.json").write_text("{}", encoding="utf-8")
    (cfg.maintenance_cycles_dir / "x.json").write_text(
        json.dumps(
            {
                "cycle_id": "c_x",
                "timestamp": "2026-04-01T12:00:00Z",
                "directive": "D777_test",
                "directive_title": "T",
                "status": "COMPLETED",
            }
        ),
        encoding="utf-8",
    )
    data = json.loads(dash.build_data_json(cfg))
    ids = [c.get("directive") for c in data["maintenance_cycles"]]
    assert "D777_test" in ids
