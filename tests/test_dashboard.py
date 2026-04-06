from __future__ import annotations

import json
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from meta_harness import memory as mem_mod
from meta_harness.cli import main
from meta_harness.config import load_config
from meta_harness import dashboard as dash


@pytest.fixture
def mini_project(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        "[project]\nname = \"dash-test\"\n[cycle]\nveto_seconds = 0\n"
        "min_evidence_items = 1\n[memory]\nenabled = true\n"
        "[evidence]\nmetrics_patterns = [\"metrics.json\", \"alt.json\"]\n"
        "[goals]\nprimary_metric = \"coverage_pct\"\n",
        encoding="utf-8",
    )
    h = tmp_path / ".metaharness"
    (h / "cycles" / "maintenance").mkdir(parents=True)
    (h / "cycles" / "product").mkdir(parents=True)
    (h / "memory").mkdir(parents=True)
    return tmp_path


def test_load_cycles_sorted_newest_first(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    mdir = cfg.maintenance_cycles_dir
    older = {
        "cycle_id": "cycle_a",
        "timestamp": "2026-01-01T10:00:00Z",
        "directive": "D001",
        "directive_title": "First",
        "status": "COMPLETED",
        "delta": 0.1,
    }
    newer = {
        "cycle_id": "cycle_b",
        "timestamp": "2026-06-01T10:00:00Z",
        "directive": "D002",
        "status": "COMPLETED",
    }
    (mdir / "cycle_a.json").write_text(json.dumps(older), encoding="utf-8")
    (mdir / "cycle_b.json").write_text(json.dumps(newer), encoding="utf-8")
    rows = dash.load_cycles_for_dir(mdir, "maintenance")
    assert [r.cycle_id for r in rows] == ["cycle_b", "cycle_a"]


def test_resolve_latest_metrics_prefers_newer_mtime(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    old = mini_project / "metrics.json"
    alt = mini_project / "alt.json"
    old.write_text(json.dumps({"x": 1}), encoding="utf-8")
    alt.write_text(json.dumps({"y": 2}), encoding="utf-8")
    # Make alt newer
    import os

    stat = old.stat()
    os.utime(alt, (stat.st_mtime + 100, stat.st_mtime + 100))
    p = dash.resolve_latest_metrics_path(cfg)
    assert p is not None and p.name == "alt.json"


def test_kg_table_row_counts_readonly(tmp_path: Path) -> None:
    db = tmp_path / "kg.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE nodes (id TEXT);")
    conn.execute("INSERT INTO nodes VALUES ('a');")
    conn.commit()
    conn.close()
    counts = dash.kg_table_row_counts(db)
    assert counts == [("nodes", 1)]


def test_kg_table_row_counts_missing_returns_none(tmp_path: Path) -> None:
    assert dash.kg_table_row_counts(tmp_path / "nope.db") is None


def test_build_dashboard_html_includes_paths_and_metrics(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    mdir = cfg.maintenance_cycles_dir
    pdir = cfg.product_cycles_dir
    (mdir / "c1.json").write_text(
        json.dumps(
            {
                "cycle_id": "c1",
                "timestamp": "2026-03-01T12:00:00Z",
                "directive": "M001",
                "directive_title": "Fix",
                "status": "COMPLETED",
                "pre_metric": 0.5,
                "post_metric": 0.6,
                "delta": 0.1,
                "error": "",
            }
        ),
        encoding="utf-8",
    )
    (pdir / "p1.json").write_text(
        json.dumps(
            {
                "cycle_id": "p1",
                "timestamp": "2026-03-02T12:00:00Z",
                "directive": "P001",
                "directive_title": "Feature",
                "status": "COMPLETED",
                "layer": "product",
            }
        ),
        encoding="utf-8",
    )
    (mini_project / "metrics.json").write_text(
        json.dumps({"coverage_pct": 88.5, "test_pass_rate": 1.0}),
        encoding="utf-8",
    )
    db_path = mini_project / ".metaharness" / "knowledge_graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE nodes (id TEXT);")
    conn.commit()
    conn.close()

    html = dash.build_dashboard_html(cfg)
    assert "dash-test" in html
    assert "cdn.tailwindcss.com" in html
    assert "chart.umd.min.js" in html
    assert 'id="overview"' in html
    assert 'id="cycles"' in html
    assert 'id="metrics"' in html
    assert 'id="vision"' in html
    assert 'id="graph"' in html
    assert 'id="memory"' in html
    assert "fetch('/data')" in html or 'fetch("/data")' in html


def test_http_handler_serves_get(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    rt = dash.DashboardRuntime(cfg)
    rt.refresh_from_disk()
    handler = dash._dashboard_handler_factory(cfg, rt)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
        assert "Meta-Harness" in body and "dash-test" in body
        assert 'id="overview"' in body
        assert "EventSource" in body
        assert "/data" in body
        h = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        try:
            assert h.status == 200
            assert h.read().decode("utf-8") == "OK"
            assert h.getheader("Content-Type", "").startswith("text/plain")
        finally:
            h.close()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/missing", timeout=5)
    finally:
        rt.close()
        server.shutdown()
        thread.join(timeout=2)


def test_api_dashboard_json_keys(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    (cfg.maintenance_cycles_dir / "c1.json").write_text(
        json.dumps(
            {
                "cycle_id": "c1",
                "timestamp": "2026-03-01T12:00:00Z",
                "directive": "M001",
                "directive_title": "Fix",
                "status": "COMPLETED",
            }
        ),
        encoding="utf-8",
    )
    (mini_project / "metrics.json").write_text(
        json.dumps({"coverage_pct": 88.5}),
        encoding="utf-8",
    )
    rt = dash.DashboardRuntime(cfg)
    rt.refresh_from_disk()
    handler = dash._dashboard_handler_factory(cfg, rt)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/dashboard.json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
        assert data["project"] == "dash-test"
        assert "seq" in data and "generated_at" in data
        assert "maintenance_cycles" in data and "product_cycles" in data
        assert isinstance(data["maintenance_cycles"], list)
        assert data["metrics"]["coverage_pct"] == 88.5
        assert "kg" in data and "sqlite_path" in data["kg"]
        assert "markup" in data
    finally:
        rt.close()
        server.shutdown()
        thread.join(timeout=2)


def test_events_sse_first_line_json(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    rt = dash.DashboardRuntime(cfg)
    rt.refresh_from_disk()
    handler = dash._dashboard_handler_factory(cfg, rt)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            conn.sendall(
                b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            raw = b""
            while b"\r\n\r\n" not in raw and len(raw) < 65536:
                raw += conn.recv(4096)
            assert b"200" in raw.split(b"\r\n", 1)[0]
            assert b"text/event-stream" in raw.lower()
            body_start = raw.index(b"\r\n\r\n") + 4
            body = raw[body_start:]
            while b"\n\n" not in body and len(body) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                body += chunk
            text = body.decode("utf-8", errors="replace")
            first_event = text.split("\n\n")[0]
            data_line = [ln for ln in first_event.split("\n") if ln.startswith("data: ")][0]
            payload = json.loads(data_line[len("data: ") :])
            assert "seq" in payload
        finally:
            conn.close()
    finally:
        rt.close()
        server.shutdown()
        thread.join(timeout=2)


def test_build_dashboard_snapshot_dict_structure(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    snap = dash.build_dashboard_snapshot_dict(cfg)
    assert snap["project"] == "dash-test"
    assert "maintenance_cycles" in snap
    assert "markup" in snap and "maintenance_tbody" in snap["markup"]


def test_build_dashboard_html_memmap_section(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    mem_dir = cfg.memory_dir
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_mod.update(
        mem_dir,
        "dash-test",
        "D001",
        "First",
        "COMPLETED",
        0.05,
        ["src/foo.py"],
        0.5,
        0.55,
        "coverage_pct",
    )
    html = dash.build_dashboard_html(cfg)
    assert 'id="mem-mmap"' in html
    data = json.loads(dash.build_data_json(cfg))
    assert "ascii_map" in data["memory"]
    assert "MEMORY MAP" in data["memory"]["ascii_map"] or "DIRECTIVE CHAIN" in data["memory"]["ascii_map"]


def test_build_dashboard_html_kg_snapshot(mini_project: Path) -> None:
    cfg = load_config(mini_project)
    db_path = cfg.kg_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE nodes (
          id TEXT PRIMARY KEY,
          node_type TEXT NOT NULL,
          name TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT '',
          data_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE edges (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          src_id TEXT NOT NULL,
          dst_id TEXT NOT NULL,
          relation TEXT NOT NULL,
          data_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        INSERT INTO nodes (id, node_type, name, summary, status, data_json, created_at, updated_at)
        VALUES ('n1', 'directive', 'D001', 'summary text', '', '{}', '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z');
        INSERT INTO edges (src_id, dst_id, relation, data_json, created_at)
        VALUES ('n1', 'n1', 'relates_to', '{}', '2026-03-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    raw = json.loads(dash.build_data_json(cfg))
    assert raw["kg_stats"]["total_nodes"] >= 1
    edges = raw["kg_recent_edges"]
    assert any("relates_to" in str(e.get("relation", "")) for e in edges)
    nodes = raw["kg_recent_nodes"]
    assert any("directive" in str(n.get("node_type", "")) for n in nodes)


def test_cli_dashboard_help_shows_defaults() -> None:
    runner = CliRunner()
    r = runner.invoke(main, ["dashboard", "--help"])
    assert r.exit_code == 0
    out = r.output or ""
    assert "0.0.0.0" in out and "8765" in out


def test_default_constants_loopback() -> None:
    assert dash.DEFAULT_HOST == "0.0.0.0"
    assert dash.DEFAULT_PORT == 8765
