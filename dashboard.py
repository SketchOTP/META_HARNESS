"""
meta_harness/dashboard.py

Read-only local web dashboard: cycle history, metrics summary, KG table counts,
read-only KG snapshot (node types + recent nodes/edges), and ASCII memory map.

Uses ThreadingHTTPServer so long-lived GET /events (Server-Sent Events) does not
block other routes. Bind ``0.0.0.0`` for LAN access; no authentication (operator tool).
"""
from __future__ import annotations

import json
import re
import socket
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from .config import HarnessConfig
from .directive_confidence import tier_for_score
from . import memory as memory_module
from . import vision as vision_module


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

# Local-only dashboard: SSE keepalive and mtime polling (no extra deps).
SSE_HEARTBEAT_SEC = 20.0
WATCH_POLL_SEC = 2.5


@dataclass(frozen=True)
class ParsedCycle:
    """One cycle JSON row for display (maintenance or product)."""

    kind: str
    source_path: str
    cycle_id: str
    timestamp: str
    directive: str
    directive_title: str
    status: str
    pre_metric: Optional[float]
    post_metric: Optional[float]
    delta: Optional[float]
    error: str
    raw: dict[str, Any]
    directive_confidence: Optional[float] = None
    directive_confidence_detail: str = ""


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cycle_sort_ts(timestamp: str | None, mtime: float) -> float:
    if timestamp:
        try:
            ts = timestamp.strip()
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return datetime.fromisoformat(ts).timestamp()
        except ValueError:
            pass
    return mtime


def _parse_cycle_file(path: Path, kind: str) -> ParsedCycle | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    err = raw.get("error") or ""
    if isinstance(err, str) and len(err) > 500:
        err = err[:500] + "…"
    elif not isinstance(err, str):
        err = str(err)
    dc = _safe_float(raw.get("directive_confidence"))
    return ParsedCycle(
        kind=kind,
        source_path=str(path.resolve()),
        cycle_id=str(raw.get("cycle_id") or path.stem),
        timestamp=str(raw.get("timestamp") or ""),
        directive=str(raw.get("directive") or "-"),
        directive_title=str(raw.get("directive_title") or ""),
        status=str(raw.get("status") or "?"),
        pre_metric=_safe_float(raw.get("pre_metric")),
        post_metric=_safe_float(raw.get("post_metric")),
        delta=_safe_float(raw.get("delta")),
        error=err,
        raw=raw,
        directive_confidence=dc,
        directive_confidence_detail=str(raw.get("directive_confidence_detail") or ""),
    )


def load_cycles_for_dir(cycle_dir: Path, kind: str) -> list[ParsedCycle]:
    if not cycle_dir.is_dir():
        return []
    out: list[ParsedCycle] = []
    for p in sorted(cycle_dir.glob("*.json")):
        rec = _parse_cycle_file(p, kind)
        if rec is not None:
            out.append(rec)
    out.sort(
        key=lambda r: _cycle_sort_ts(r.timestamp or None, Path(r.source_path).stat().st_mtime),
        reverse=True,
    )
    return out


def collect_all_cycles(cfg: HarnessConfig) -> tuple[list[ParsedCycle], list[ParsedCycle]]:
    maintenance = load_cycles_for_dir(cfg.maintenance_cycles_dir, "maintenance")
    product = load_cycles_for_dir(cfg.product_cycles_dir, "product")
    return maintenance, product


def resolve_latest_metrics_path(cfg: HarnessConfig) -> Path | None:
    """First existing metrics file under project_root matching evidence patterns (newest mtime wins tie)."""
    root = cfg.project_root
    candidates: list[Path] = []
    for pattern in cfg.evidence.metrics_patterns:
        for p in root.glob(pattern):
            if p.is_file():
                candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def load_metrics_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _safe_sqlite_table_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def kg_table_row_counts(kg_path: Path) -> list[tuple[str, int]] | None:
    """Non-destructive snapshot: user tables and row counts. Returns None if DB missing."""
    if not kg_path.is_file():
        return None
    uri = f"file:{kg_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        rows: list[tuple[str, int]] = []
        for (name,) in cur.fetchall():
            if not isinstance(name, str) or not _safe_sqlite_table_name(name):
                continue
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
                cnt = int(n[0]) if n else 0
            except sqlite3.Error:
                cnt = -1
            rows.append((name, cnt))
        return rows
    finally:
        conn.close()


_KG_SNAPSHOT_NODE_LIMIT = 50
_KG_SNAPSHOT_EDGE_LIMIT = 50
_TEXT_PREVIEW = 120


def _truncate_text(s: str, max_len: int = _TEXT_PREVIEW) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


@dataclass(frozen=True)
class KgSnapshot:
    """Read-only knowledge graph snapshot for dashboard display."""

    counts_by_type: list[tuple[str, int]]
    recent_nodes: list[tuple[str, str, str, str]]
    recent_edges: list[tuple[str, str, str, str]]


def kg_snapshot(kg_path: Path) -> KgSnapshot | None:
    """
    Load node-type counts, newest nodes by updated_at, and recent edges (read-only).
    Returns None if DB missing, unreadable, or required tables/columns absent.
    """
    if not kg_path.is_file():
        return None
    uri = f"file:{kg_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('nodes','edges')"
        )
        found = {row[0] for row in cur.fetchall()}
        if "nodes" not in found:
            return None

        counts_by_type: list[tuple[str, int]] = []
        try:
            cur = conn.execute(
                "SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type ORDER BY node_type"
            )
            for row in cur.fetchall():
                if row[0] is not None:
                    counts_by_type.append((str(row[0]), int(row[1])))
        except sqlite3.Error:
            return None

        recent_nodes: list[tuple[str, str, str, str]] = []
        try:
            cur = conn.execute(
                """
                SELECT id, node_type, name, summary, updated_at
                FROM nodes
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (_KG_SNAPSHOT_NODE_LIMIT,),
            )
            for nid, ntype, name, summary, _ua in cur.fetchall():
                recent_nodes.append(
                    (
                        str(nid),
                        str(ntype or ""),
                        _truncate_text(str(name or "")),
                        _truncate_text(str(summary or "")),
                    )
                )
        except sqlite3.Error:
            recent_nodes = []

        recent_edges: list[tuple[str, str, str, str]] = []
        if "edges" in found:
            try:
                cur = conn.execute(
                    """
                    SELECT relation, src_id, dst_id, COALESCE(created_at, '')
                    FROM edges
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (_KG_SNAPSHOT_EDGE_LIMIT,),
                )
                for rel, src, dst, ca in cur.fetchall():
                    recent_edges.append(
                        (str(rel or ""), str(src or ""), str(dst or ""), str(ca or ""))
                    )
            except sqlite3.Error:
                pass

        return KgSnapshot(
            counts_by_type=counts_by_type,
            recent_nodes=recent_nodes,
            recent_edges=recent_edges,
        )
    finally:
        conn.close()


def _format_kg_snapshot_html(snap: KgSnapshot) -> str:
    if snap.counts_by_type:
        rows_ct = "".join(
            f"<tr><td><code>{escape(t)}</code></td><td>{n}</td></tr>"
            for t, n in snap.counts_by_type
        )
        block_ct = f"""
        <h3>Nodes by type</h3>
        <table class="kg-snap"><thead><tr><th>node_type</th><th>Count</th></tr></thead><tbody>{rows_ct}</tbody></table>
        """
    else:
        block_ct = "<p class='muted'>No typed nodes in this database.</p>"

    if snap.recent_nodes:
        rows_n = []
        for nid, ntype, name, summ in snap.recent_nodes:
            rows_n.append(
                "<tr>"
                f"<td><code>{escape(nid)}</code></td>"
                f"<td><code>{escape(ntype)}</code></td>"
                f"<td>{escape(name)}</td>"
                f"<td>{escape(summ)}</td>"
                "</tr>"
            )
        block_n = f"""
        <h3>Recent nodes <span class="muted">(newest by updated_at, up to {_KG_SNAPSHOT_NODE_LIMIT})</span></h3>
        <table class="kg-snap wide"><thead><tr><th>id</th><th>node_type</th><th>name</th><th>summary</th></tr></thead><tbody>
        {"".join(rows_n)}
        </tbody></table>
        """
    else:
        block_n = ""

    if snap.recent_edges:
        rows_e = []
        for rel, src, dst, _ca in snap.recent_edges:
            rows_e.append(
                "<tr>"
                f"<td><code>{escape(rel)}</code></td>"
                f"<td><code>{escape(src)}</code></td>"
                f"<td><code>{escape(dst)}</code></td>"
                "</tr>"
            )
        block_e = f"""
        <h3>Recent edges <span class="muted">(up to {_KG_SNAPSHOT_EDGE_LIMIT})</span></h3>
        <table class="kg-snap wide"><thead><tr><th>relation</th><th>src_id</th><th>dst_id</th></tr></thead><tbody>
        {"".join(rows_e)}
        </tbody></table>
        """
    else:
        block_e = ""

    return block_ct + block_n + block_e


def _memory_map_section_html(cfg: HarnessConfig) -> str:
    """Aligned with CLI `memmap`: disabled → message; no cycles yet → dim note; else ASCII map in &lt;pre&gt;."""
    if not cfg.memory.enabled:
        return (
            "<p class='muted'>Memory is disabled in configuration "
            "(<code>[memory] enabled = false</code>).</p>"
        )
    mem = memory_module.load(cfg.memory_dir)
    if mem.total_cycles == 0:
        return (
            "<p class='muted'>No memory yet — run at least one cycle first.</p>"
        )
    try:
        text = memory_module.render_map(mem)
    except Exception:
        return "<p class='warn'>Could not render memory map.</p>"
    return f'<pre class="memmap">{escape(text)}</pre>'


def _fmt_metric(v: Any) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) and v != int(v) else str(int(v) if v == int(v) else v)
    if isinstance(v, str):
        return v[:200] + ("…" if len(v) > 200 else "")
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)[:300]
    return str(v)[:200]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cycle_public_dict(c: ParsedCycle) -> dict[str, Any]:
    raw = c.raw if isinstance(c.raw, dict) else {}
    chg = raw.get("changes_applied")
    return {
        "kind": c.kind,
        "source_path": c.source_path,
        "cycle_id": c.cycle_id,
        "timestamp": c.timestamp,
        "directive": c.directive,
        "directive_title": c.directive_title,
        "status": c.status,
        "pre_metric": c.pre_metric,
        "post_metric": c.post_metric,
        "delta": c.delta,
        "error": c.error,
        "changes_applied": chg,
        "directive_confidence": c.directive_confidence,
        "directive_confidence_detail": c.directive_confidence_detail,
        "raw": raw,
    }


def _kg_snapshot_to_json(snap: KgSnapshot) -> dict[str, Any]:
    return {
        "counts_by_type": [[t, n] for t, n in snap.counts_by_type],
        "recent_nodes": [list(row) for row in snap.recent_nodes],
        "recent_edges": [list(row) for row in snap.recent_edges],
    }


def _kg_edge_count(kg_path: Path) -> int:
    if not kg_path.is_file():
        return 0
    uri = f"file:{kg_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return 0
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='edges'"
        )
        if not cur.fetchone():
            return 0
        row = conn.execute("SELECT COUNT(*) FROM edges").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _kg_top_files_by_edges(kg_path: Path, limit: int = 5) -> list[tuple[str, int]]:
    if not kg_path.is_file() or limit <= 0:
        return []
    uri = f"file:{kg_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='edges'"
        )
        if not cur.fetchone():
            return []
        cur = conn.execute(
            """
            WITH ids AS (
              SELECT src_id AS id FROM edges
              UNION ALL
              SELECT dst_id FROM edges
            )
            SELECT id, COUNT(*) AS c
            FROM ids
            WHERE id LIKE 'file:%'
            GROUP BY id
            ORDER BY c DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [(str(i), int(c)) for i, c in cur.fetchall() if i]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _truncate_80(s: str) -> str:
    t = (s or "").strip()
    if len(t) <= 80:
        return t
    return t[:79] + "…"


def _merged_cycle_activity(
    maintenance: list[ParsedCycle], product: list[ParsedCycle], limit: int = 10
) -> list[ParsedCycle]:
    merged = list(maintenance) + list(product)
    merged.sort(
        key=lambda r: _cycle_sort_ts(
            r.timestamp or None, Path(r.source_path).stat().st_mtime
        ),
        reverse=True,
    )
    return merged[:limit]


def _test_pass_trajectory_points(
    cfg: HarnessConfig,
    metrics_data: dict[str, Any],
    mem: memory_module.ProjectMemory,
    maintenance: list[ParsedCycle],
    product: list[ParsedCycle],
) -> list[dict[str, Any]]:
    primary = (cfg.goals.primary_metric or "").strip()
    if primary == "test_pass_rate" and mem.metric_trajectory:
        out: list[dict[str, Any]] = []
        for pair in mem.metric_trajectory[-20:]:
            if len(pair) >= 2:
                out.append({"directive": str(pair[0]), "value": float(pair[1])})
        return out
    combined: list[ParsedCycle] = []
    for c in maintenance + product:
        combined.append(c)
    combined.sort(
        key=lambda r: _cycle_sort_ts(
            r.timestamp or None, Path(r.source_path).stat().st_mtime
        ),
        reverse=True,
    )
    combined = combined[:20]
    combined.reverse()
    fallback = metrics_data.get("test_pass_rate")
    pts: list[dict[str, Any]] = []
    for c in combined:
        raw = c.raw if isinstance(c.raw, dict) else {}
        v = raw.get("test_pass_rate")
        if v is None and primary == "test_pass_rate" and c.post_metric is not None:
            v = c.post_metric
        if v is None and fallback is not None:
            v = fallback
        pts.append(
            {
                "directive": c.directive,
                "value": float(v) if v is not None else None,
            }
        )
    return pts


def _product_coverage_trend(
    cfg: HarnessConfig,
    metrics_data: dict[str, Any],
    product: list[ParsedCycle],
) -> list[dict[str, Any]]:
    primary = (cfg.goals.primary_metric or "").strip().lower()
    rows = list(reversed(product[:20]))  # oldest first, up to 20 newest
    cur_cov = metrics_data.get("coverage_pct")
    cur_br = metrics_data.get("branch_coverage_pct")
    out: list[dict[str, Any]] = []
    for c in rows:
        raw = c.raw if isinstance(c.raw, dict) else {}
        cov = raw.get("coverage_pct")
        br = raw.get("branch_coverage_pct")
        if cov is None and primary == "coverage_pct" and c.post_metric is not None:
            cov = c.post_metric
        if cov is None:
            cov = cur_cov
        if br is None:
            br = cur_br
        out.append(
            {
                "timestamp": c.timestamp,
                "directive": c.directive,
                "coverage_pct": float(cov) if cov is not None else None,
                "branch_coverage_pct": float(br) if br is not None else None,
            }
        )
    return out


def build_data_dict(cfg: HarnessConfig) -> dict[str, Any]:
    maintenance, product = collect_all_cycles(cfg)
    m_main = maintenance[:20]
    m_prod = product[:20]
    metrics_path = resolve_latest_metrics_path(cfg)
    metrics_raw = load_metrics_json(metrics_path) if metrics_path else None
    metrics_data: dict[str, Any] = dict(metrics_raw) if metrics_raw else {}

    mem = memory_module.load(cfg.memory_dir)
    mem_dict = asdict(mem)
    if cfg.memory.enabled and mem.total_cycles > 0:
        try:
            mem_dict["ascii_map"] = memory_module.render_map(mem)
        except Exception:
            mem_dict["ascii_map"] = ""
    else:
        mem_dict["ascii_map"] = ""

    kg_snap = kg_snapshot(cfg.kg_path)
    kg_stats: dict[str, Any] = {
        "counts_by_type": [],
        "total_nodes": 0,
        "total_edges": 0,
        "top_files_by_edges": [],
    }
    if kg_snap:
        kg_stats["counts_by_type"] = [[t, n] for t, n in kg_snap.counts_by_type]
        kg_stats["total_nodes"] = sum(n for _, n in kg_snap.counts_by_type)
        kg_stats["total_edges"] = _kg_edge_count(cfg.kg_path)
        kg_stats["top_files_by_edges"] = [
            {"id": fid, "edges": n} for fid, n in _kg_top_files_by_edges(cfg.kg_path, 5)
        ]

    kg_recent_nodes: list[dict[str, Any]] = []
    if kg_snap:
        for nid, ntype, name, summ in kg_snap.recent_nodes[:20]:
            kg_recent_nodes.append(
                {
                    "id": nid,
                    "node_type": ntype,
                    "name": name,
                    "summary": _truncate_80(summ),
                }
            )

    kg_recent_edges: list[dict[str, Any]] = []
    if kg_snap:
        for rel, src, dst, ca in kg_snap.recent_edges[:20]:
            kg_recent_edges.append(
                {
                    "relation": rel,
                    "src_id": src,
                    "dst_id": dst,
                    "created_at": ca,
                }
            )

    vision_block: dict[str, Any] = {}
    kg = memory_module.get_kg(cfg)
    try:
        vision_block = dict(vision_module.load_vision(kg))
        vision_block["features_done"] = vision_module.derive_features_done(kg)
        vision_block["out_of_scope"] = vision_module.derive_out_of_scope(kg)
    finally:
        kg.close()

    activity = _merged_cycle_activity(maintenance, product, 10)
    test_pass_traj = _test_pass_trajectory_points(
        cfg, metrics_data, mem, maintenance, product
    )
    product_cov_trend = _product_coverage_trend(cfg, metrics_data, product)

    return {
        "project": cfg.project.name,
        "metrics": metrics_data,
        "metrics_path": str(metrics_path.resolve()) if metrics_path else None,
        "maintenance_cycles": [_cycle_public_dict(c) for c in m_main],
        "product_cycles": [_cycle_public_dict(c) for c in m_prod],
        "activity_cycles": [_cycle_public_dict(c) for c in activity],
        "kg_stats": kg_stats,
        "kg_recent_nodes": kg_recent_nodes,
        "kg_recent_edges": kg_recent_edges,
        "vision": vision_block,
        "memory": mem_dict,
        "file_touches": dict(mem.file_touches),
        "file_successes": dict(mem.file_successes),
        "metric_trajectory": list(mem.metric_trajectory),
        "test_pass_trajectory": test_pass_traj,
        "product_coverage_trend": product_cov_trend,
        "memory_enabled": cfg.memory.enabled,
    }


def build_data_json(cfg: HarnessConfig) -> str:
    return json.dumps(build_data_dict(cfg), ensure_ascii=False)


def _table_rows_html(cycles: list[ParsedCycle]) -> str:
    if not cycles:
        return "<tr><td colspan='10' class='muted'>No cycle logs yet.</td></tr>"
    lines: list[str] = []
    for c in cycles:
        pre = "-" if c.pre_metric is None else f"{c.pre_metric:g}"
        post = "-" if c.post_metric is None else f"{c.post_metric:g}"
        delta = "-" if c.delta is None else f"{c.delta:+.4f}"
        err = escape(c.error) if c.error else "—"
        if c.directive_confidence is not None:
            tr = tier_for_score(c.directive_confidence)
            pred = f"{int(round(c.directive_confidence * 100))}% ({tr})"
        else:
            pred = "—"
        det = escape(c.directive_confidence_detail[:120]) if c.directive_confidence_detail else ""
        lines.append(
            "<tr>"
            f"<td><span class='tag'>{escape(c.kind)}</span></td>"
            f"<td class='ts'>{escape(c.timestamp[:19] if len(c.timestamp) > 10 else c.timestamp)}</td>"
            f"<td>{escape(c.directive)}</td>"
            f"<td class='mono' title='{det}'>{escape(pred)}</td>"
            f"<td class='title'>{escape(c.directive_title[:80])}</td>"
            f"<td><span class='st'>{escape(c.status)}</span></td>"
            f"<td>{pre}</td><td>{post}</td><td>{delta}</td>"
            f"<td class='err' title='{err}'>{err}</td>"
            "</tr>"
        )
    return "\n".join(lines)


def _compute_dashboard_payload(cfg: HarnessConfig) -> dict[str, Any]:
    """Structured snapshot + HTML fragments for the dashboard (no seq / generated_at)."""
    maintenance, product = collect_all_cycles(cfg)
    metrics_path = resolve_latest_metrics_path(cfg)
    metrics_data = load_metrics_json(metrics_path) if metrics_path else None
    kg_stats = kg_table_row_counts(cfg.kg_path)
    kg_snap = kg_snapshot(cfg.kg_path) if kg_stats is not None else None
    primary = (cfg.goals.primary_metric or "").strip()
    kg_abs = str(cfg.kg_path.resolve())
    mem_abs = str(cfg.memory_dir.resolve())

    maintenance_tbody = _table_rows_html(maintenance)
    product_tbody = _table_rows_html(product)

    metrics_block: str
    if metrics_path and metrics_data:
        mp = escape(str(metrics_path.resolve()))
        pri_line = ""
        if primary and primary in metrics_data:
            pri_line = (
                f"<p><strong>Primary metric ({escape(primary)}):</strong> "
                f"{escape(_fmt_metric(metrics_data[primary]))}</p>"
            )
        elif primary:
            pri_line = (
                f"<p class='muted'><strong>Configured primary metric:</strong> {escape(primary)} "
                "(not present in this metrics.json)</p>"
            )
        cov = metrics_data.get("coverage_pct")
        cov_line = ""
        if cov is not None:
            cov_line = f"<p><strong>coverage_pct:</strong> {escape(_fmt_metric(cov))}</p>"
        keys_html = []
        for k in sorted(metrics_data.keys()):
            if k == "low_coverage_files":
                continue
            keys_html.append(f"<li><code>{escape(k)}</code>: {escape(_fmt_metric(metrics_data[k]))}</li>")
        metrics_block = f"""
        <p class="path">File: <code>{mp}</code></p>
        {pri_line}
        {cov_line}
        <ul class="metrics-list">{"".join(keys_html)}</ul>
        """
    elif metrics_path:
        metrics_block = f"<p class='warn'>Could not read <code>{escape(str(metrics_path))}</code></p>"
    else:
        metrics_block = "<p class='muted'>No metrics file found (check <code>evidence.metrics_patterns</code>).</p>"

    kg_block: str
    if kg_stats is None:
        kg_block = f"<p class='muted'>Knowledge graph database not found at <code>{escape(kg_abs)}</code></p>"
    else:
        rows_kg = "".join(
            f"<tr><td><code>{escape(n)}</code></td><td>{cnt if cnt >= 0 else '—'}</td></tr>"
            for n, cnt in kg_stats
        )
        snap_html = ""
        if kg_snap is not None:
            snap_html = f'<div class="kg-snapshot">{_format_kg_snapshot_html(kg_snap)}</div>'
        kg_block = f"""
        <p class="path">SQLite: <code>{escape(kg_abs)}</code></p>
        <table class="kg"><thead><tr><th>Table</th><th>Rows</th></tr></thead><tbody>{rows_kg or "<tr><td colspan='2' class='muted'>No tables</td></tr>"}</tbody></table>
        {snap_html}
        """

    mem_paths = f"""
    <p class="path">Memory directory: <code>{escape(mem_abs)}</code></p>
    <p class="muted">Snapshots and <code>project_memory.json</code> live here when memory is enabled.</p>
    """
    mem_map_body = _memory_map_section_html(cfg)

    metrics_json: dict[str, Any] = {
        "path": str(metrics_path.resolve()) if metrics_path else None,
        "data": metrics_data,
        "primary_metric": primary,
        "coverage_pct": metrics_data.get("coverage_pct") if metrics_data else None,
    }

    kg_json: dict[str, Any] = {
        "sqlite_path": kg_abs,
        "tables": [[n, cnt] for n, cnt in (kg_stats or [])],
        "snapshot": _kg_snapshot_to_json(kg_snap) if kg_snap is not None else None,
    }

    return {
        "project": cfg.project.name,
        "maintenance_cycles": [_cycle_public_dict(c) for c in maintenance],
        "product_cycles": [_cycle_public_dict(c) for c in product],
        "metrics": metrics_json,
        "kg": kg_json,
        "memory": {
            "enabled": cfg.memory.enabled,
            "directory": mem_abs,
        },
        "markup": {
            "maintenance_tbody": maintenance_tbody,
            "product_tbody": product_tbody,
            "metrics_block": metrics_block,
            "kg_block": kg_block,
            "mem_paths": mem_paths,
            "mem_map_body": mem_map_body,
        },
    }


def build_dashboard_html(cfg: HarnessConfig) -> str:
    proj = escape(cfg.project.name)
    html = '<!DOCTYPE html>\n<html lang="en" class="dark h-full">\n<head>\n<meta charset="utf-8"/>\n<meta name="viewport" content="width=device-width, initial-scale=1"/>\n<title>Meta-Harness — __PRJ__</title>\n<link rel="preconnect" href="https://fonts.googleapis.com"/>\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>\n<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>\n<script src="https://cdn.tailwindcss.com"></script>\n<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>\n<style>\n:root {\n  --bg-primary: #0a0a0f;\n  --bg-secondary: #0f0f1a;\n  --bg-card: #13131f;\n  --bg-card-hover: #1a1a2e;\n  --border: #1e1e3a;\n  --border-accent: #2a2a4a;\n  --text-primary: #e2e8f0;\n  --text-secondary: #94a3b8;\n  --text-muted: #475569;\n  --accent-cyan: #22d3ee;\n  --accent-green: #4ade80;\n  --accent-amber: #fbbf24;\n  --accent-red: #f87171;\n  --accent-purple: #a78bfa;\n  --chart-grid: #1e1e3a;\n}\nbody { font-family: \'Inter\', system-ui, sans-serif; background: var(--bg-primary); color: var(--text-primary); }\n.mono { font-family: \'JetBrains Mono\', ui-monospace, monospace; }\n#sidebar button { color: var(--text-secondary); }\n#sidebar button:hover, #sidebar button.active { color: var(--accent-cyan); background: var(--bg-card-hover); }\n.card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; }\n.scanline { background: linear-gradient(rgba(18, 24, 18, 0) 50%, rgba(0, 255, 65, 0.02) 50%); background-size: 100% 4px; }\n\nhtml, body { height: 100%; max-height: 100%; margin: 0; }\n#app { min-height: 0; }\n#content { min-height: 0; }\n.chart-canvas-host { position: relative; width: 100%; overflow: hidden; }\n.chart-h-220 { height: 220px; }\n.chart-h-200 { height: 200px; }\n.chart-h-140 { height: 140px; }\n.chart-h-120 { height: 120px; }\n.chart-h-100 { height: 100px; }\n</style>\n</head>\n<body class="h-full overflow-hidden">\n<div id="dash-live-banner" class="hidden fixed top-0 left-0 right-0 z-50 bg-amber-900/80 text-amber-100 text-center text-sm py-1" role="status">Live updates unavailable — refresh manually.</div>\n<div id="app" class="flex h-full min-h-0 overflow-hidden pt-6">\n<nav id="sidebar" class="w-16 flex-shrink-0 flex flex-col items-center py-3 gap-2 border-r border-[var(--border)] bg-[var(--bg-secondary)] z-40" aria-label="Sections">\n<button type="button" class="nav-btn active w-12 h-12 rounded border border-transparent" data-target="overview" title="Overview">⌂</button>\n<button type="button" class="nav-btn w-12 h-12 rounded border border-transparent" data-target="cycles" title="Cycles">↻</button>\n<button type="button" class="nav-btn w-12 h-12 rounded border border-transparent" data-target="metrics" title="Metrics">▦</button>\n<button type="button" class="nav-btn w-12 h-12 rounded border border-transparent" data-target="vision" title="Vision">◎</button>\n<button type="button" class="nav-btn w-12 h-12 rounded border border-transparent" data-target="graph" title="Graph">⬡</button>\n<button type="button" class="nav-btn w-12 h-12 rounded border border-transparent" data-target="memory" title="Memory">▤</button>\n</nav>\n<main id="content" class="flex-1 min-h-0 overflow-y-auto overflow-x-hidden p-4 md:p-6">\n<header class="mb-6 border-b border-[var(--border)] pb-3">\n<h1 class="text-xl font-semibold tracking-tight">Meta-Harness <span class="text-[var(--accent-cyan)] mono">__PRJ__</span></h1>\n<p class="text-sm text-[var(--text-muted)] mt-1">Operations console — read-only; data via <code class="mono">/data</code> + SSE</p>\n</header>\n\n<section id="overview" class="dash-section space-y-6">\n<div class="grid grid-cols-1 md:grid-cols-5 gap-4">\n<div class="card p-3 flex flex-col items-center"><div class="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mb-1">Test pass rate</div><div class="chart-canvas-host chart-h-120"><canvas id="g-tpr"></canvas></div></div>\n<div class="card p-3 flex flex-col items-center"><div class="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mb-1">Coverage</div><div class="chart-canvas-host chart-h-120"><canvas id="g-cov"></canvas></div></div>\n<div class="card p-4"><div class="text-[10px] uppercase text-[var(--text-muted)]">Total cycles</div><div id="stat-total" class="text-2xl font-bold mono">—</div><div id="stat-brk" class="text-xs text-[var(--text-secondary)] mt-1 mono">—</div></div>\n<div class="card p-4"><div class="text-[10px] uppercase text-[var(--text-muted)]">Active tests</div><div id="stat-tests" class="text-2xl font-bold mono">—</div><div id="stat-tests-sub" class="text-xs text-[var(--text-secondary)] mt-1">—</div></div>\n<div class="card p-4"><div class="text-[10px] uppercase text-[var(--text-muted)]">Vision evolution</div><div id="stat-evo" class="text-2xl font-bold mono">—</div></div>\n</div>\n<div class="card p-4"><h2 class="text-sm font-semibold text-[var(--accent-cyan)] mb-3">Activity</h2><div id="activity-feed" class="space-y-2 text-sm"></div></div>\n<div class="grid grid-cols-1 lg:grid-cols-2 gap-4">\n<div class="card p-4"><h2 class="text-sm font-semibold mb-2">File heat map</h2><div class="chart-canvas-host chart-h-220"><canvas id="chart-files"></canvas></div></div>\n<div class="card p-4"><h2 class="text-sm font-semibold mb-2">Metric trajectory (test pass)</h2><div class="chart-canvas-host chart-h-220"><canvas id="chart-traj"></canvas></div></div>\n</div>\n</section>\n\n<section id="cycles" class="dash-section hidden space-y-4">\n<div class="flex gap-2 mb-2"><button type="button" id="tab-maint" class="tab-c px-3 py-1 rounded border border-[var(--border)] bg-[var(--bg-card)] text-sm">Maintenance</button><button type="button" id="tab-prod" class="tab-c px-3 py-1 rounded border border-[var(--border)] text-sm text-[var(--text-muted)]">Product</button></div>\n<div id="panel-maint"><div class="overflow-x-auto card"><table class="w-full text-xs mono" id="tbl-maint"><thead class="bg-[var(--bg-secondary)] text-[var(--text-secondary)]"><tr><th class="p-2 text-left">Status</th><th class="p-2">When</th><th class="p-2">Directive</th><th class="p-2">Pred</th><th class="p-2">Title</th><th class="p-2">Chg</th><th class="p-2">Δ</th><th class="p-2">Error</th></tr></thead><tbody></tbody></table></div></div>\n<div id="panel-prod" class="hidden"><div class="overflow-x-auto card"><table class="w-full text-xs mono" id="tbl-prod"><thead class="bg-[var(--bg-secondary)] text-[var(--text-secondary)]"><tr><th class="p-2 text-left">Status</th><th class="p-2">When</th><th class="p-2">Directive</th><th class="p-2">Pred</th><th class="p-2">Title</th><th class="p-2">Chg</th><th class="p-2">Δ</th><th class="p-2">Error</th></tr></thead><tbody></tbody></table></div></div>\n</section>\n\n<section id="metrics" class="dash-section hidden space-y-4">\n<div class="grid grid-cols-2 md:grid-cols-4 gap-4">\n<div class="card p-3 flex flex-col items-center"><div class="text-[10px] text-[var(--text-muted)]">Pass %</div><div class="chart-canvas-host chart-h-100"><canvas id="mg-tpr"></canvas></div></div>\n<div class="card p-3 flex flex-col items-center"><div class="text-[10px] text-[var(--text-muted)]">Line cov</div><div class="chart-canvas-host chart-h-100"><canvas id="mg-lcov"></canvas></div></div>\n<div class="card p-3 flex flex-col items-center"><div class="text-[10px] text-[var(--text-muted)]">Branch cov</div><div class="chart-canvas-host chart-h-100"><canvas id="mg-bc"></canvas></div></div>\n<div class="card p-3 flex flex-col items-center"><div class="text-[10px] text-[var(--text-muted)]">Duration (s)</div><div class="chart-canvas-host chart-h-100"><canvas id="mg-dur"></canvas></div></div>\n</div>\n<div class="card p-4"><h2 class="text-sm font-semibold mb-2">Coverage trend (product cycles)</h2><div class="chart-canvas-host chart-h-200"><canvas id="chart-covtrend"></canvas></div></div>\n<div class="card p-4 overflow-x-auto"><h2 class="text-sm font-semibold mb-2">All metrics</h2><table class="w-full text-xs mono" id="tbl-metrics"><tbody></tbody></table></div>\n</section>\n\n<section id="vision" class="dash-section hidden space-y-4">\n<div class="card p-4 border-l-4 border-[var(--accent-cyan)]"><p id="vision-stmt" class="text-sm leading-relaxed whitespace-pre-wrap"></p><div id="vision-pills" class="flex flex-wrap gap-2 mt-3"></div><div class="text-xs text-[var(--text-muted)] mt-2 mono" id="vision-meta"></div></div>\n<div class="grid grid-cols-1 md:grid-cols-2 gap-4">\n<div class="card p-4"><div class="flex justify-between mb-2"><h2 class="text-sm font-semibold text-[var(--accent-amber)]">Features wanted</h2><span id="badge-want" class="text-xs mono bg-[var(--bg-secondary)] px-2 rounded">0</span></div><div id="list-want" class="space-y-2 max-h-80 overflow-y-auto"></div></div>\n<div class="card p-4"><div class="flex justify-between mb-2"><h2 class="text-sm font-semibold text-[var(--accent-green)]">Already shipped</h2><span id="badge-done" class="text-xs mono bg-[var(--bg-secondary)] px-2 rounded">0</span></div><div id="list-done" class="space-y-2 max-h-80 overflow-y-auto"></div></div>\n</div>\n<div class="card p-4"><h2 class="text-xs text-[var(--text-muted)] mb-2">Out of scope</h2><ul id="list-oos" class="text-sm text-[var(--text-secondary)] space-y-1"></ul></div>\n</section>\n\n<section id="graph" class="dash-section hidden space-y-4">\n<div class="grid grid-cols-2 md:grid-cols-4 gap-4">\n<div class="card p-4"><div class="text-[10px] text-[var(--text-muted)]">Total nodes</div><div id="kg-nodes" class="text-2xl font-bold mono">—</div></div>\n<div class="card p-4"><div class="text-[10px] text-[var(--text-muted)]">Total edges</div><div id="kg-edges" class="text-2xl font-bold mono">—</div></div>\n<div class="card p-4 col-span-2"><div class="chart-canvas-host chart-h-140"><canvas id="chart-kg-donut"></canvas></div></div>\n</div>\n<div class="card p-4"><h2 class="text-sm font-semibold mb-2">Most active files</h2><ul id="kg-top-files" class="text-xs mono space-y-1"></ul></div>\n<div class="card p-4"><h2 class="text-sm font-semibold mb-2">Recent nodes</h2><div id="kg-feed-n" class="space-y-2 text-xs"></div></div>\n<div class="card p-4 overflow-x-auto"><h2 class="text-sm font-semibold mb-2">Recent edges</h2><table class="w-full text-xs mono"><thead><tr><th class="p-2">Rel</th><th class="p-2">Src</th><th class="p-2">Dst</th></tr></thead><tbody id="kg-feed-e"></tbody></table></div>\n</section>\n\n<section id="memory" class="dash-section hidden space-y-4">\n<div class="grid grid-cols-1 md:grid-cols-4 gap-4">\n<div class="card p-4 md:col-span-3"><div class="text-[10px] text-[var(--text-muted)]">Cycle breakdown</div><div id="mem-bar" class="h-6 rounded overflow-hidden flex mt-2 bg-[var(--bg-secondary)]"></div><div id="mem-stat" class="text-xs mono mt-2"></div></div>\n<div class="card p-4"><div class="text-[10px] text-[var(--text-muted)]">Baseline → current → best</div><div id="mem-bcb" class="text-xs mono mt-2 space-y-1"></div></div>\n</div>\n<div class="card p-4"><h2 class="text-sm font-semibold mb-3">Directive chain</h2><div id="mem-chain" class="flex flex-wrap gap-2"></div></div>\n<div class="card p-0 overflow-hidden border border-[var(--border)]"><pre id="mem-mmap" class="mono text-xs p-4 text-[var(--accent-green)] bg-black scanline max-h-[480px] overflow-auto" style="text-shadow: 0 0 1px rgba(74,222,128,0.3);"></pre></div>\n</section>\n</main>\n</div>\n<script>\n(function(){\nvar chartRegistry = {};\nfunction destroyChart(id) {\n  var el = document.getElementById(id);\n  if (!el || typeof Chart === \'undefined\') return;\n  var c = Chart.getChart(el);\n  if (c) c.destroy();\n}\nfunction gauge(canvasId, pct0to100, colors) {\n  destroyChart(canvasId);\n  var el = document.getElementById(canvasId);\n  if (!el) return;\n  var v = Math.max(0, Math.min(100, pct0to100));\n  var rest = 100 - v;\n  var bg = colors && colors.bg ? colors.bg : \'#1e1e3a\';\n  var fg = colors && colors.fg ? colors.fg : \'#22d3ee\';\n  chartRegistry[canvasId] = new Chart(el, {\n    type: \'doughnut\',\n    data: { datasets: [{ data: [v, rest], backgroundColor: [fg, bg], borderWidth: 0 }] },\n    options: {\n      cutout: \'72%\',\n      rotation: -90,\n      circumference: 180,\n      plugins: { legend: { display: false }, tooltip: { callbacks: { label: function(x){ return (x.parsed !== undefined ? x.parsed : x.raw) + \'%\'; } } } },\n      responsive: true,\n      maintainAspectRatio: false\n    }\n  });\n}\nfunction relTime(iso) {\n  if (!iso) return \'—\';\n  try {\n    var t = new Date(iso.indexOf(\'Z\')===-1 && iso.indexOf(\'+\')===-1 ? iso+\'Z\' : iso).getTime();\n    var s = Math.floor((Date.now()-t)/1000);\n    if (s < 60) return s+\'s ago\';\n    if (s < 3600) return Math.floor(s/60)+\'m ago\';\n    if (s < 86400) return Math.floor(s/3600)+\'h ago\';\n    return Math.floor(s/86400)+\'d ago\';\n  } catch(e) { return iso.slice(0,19); }\n}\nfunction normPct(x) {\n  if (x == null || isNaN(x)) return 0;\n  var n = Number(x);\n  return n <= 1 ? n * 100 : n;\n}\nfunction statusColor(st) {\n  if (st === \'COMPLETED\') return \'bg-emerald-900/50 text-emerald-300 border border-emerald-700\';\n  if (st === \'VETOED\') return \'bg-amber-900/40 text-amber-300 border border-amber-700\';\n  if (st === \'TEST_FAILED\' || st === \'AGENT_FAILED\' || st === \'ERROR\' || st === \'METRIC_REGRESSION\') return \'bg-red-900/40 text-red-300 border border-red-700\';\n  return \'bg-slate-800 text-slate-300 border border-slate-600\';\n}\nfunction rowTint(st) {\n  if (st === \'COMPLETED\') return \'bg-emerald-950/20\';\n  if (st === \'VETOED\') return \'bg-amber-950/20\';\n  if (st === \'TEST_FAILED\' || st === \'AGENT_FAILED\' || st === \'ERROR\' || st === \'METRIC_REGRESSION\') return \'bg-red-950/20\';\n  return \'\';\n}\nfunction renderCycles(tbl, rows) {\n  var tb = document.querySelector(\'#\'+tbl+\' tbody\');\n  if (!tb) return;\n  tb.innerHTML = \'\';\n  (rows || []).forEach(function(c) {\n    var raw = c.raw || {};\n    var ch = c.changes_applied != null ? c.changes_applied : raw.changes_applied;\n    if (ch == null || ch === \'\') ch = \'—\';\n    var tr = document.createElement(\'tr\');\n    tr.className = rowTint(c.status);\n    var err = (c.error || \'\').slice(0, 120);\n    var dt = c.delta;\n    var dStr = dt == null ? \'—\' : (dt>0 ? \'+\'+dt : String(dt));\n    var dClass = dt == null ? \'text-[var(--text-muted)]\' : (dt>0 ? \'text-[var(--accent-green)]\' : (dt<0 ? \'text-[var(--accent-red)]\' : \'text-[var(--text-muted)]\'));\n    tr.innerHTML = \'<td class="p-2"><span class="px-2 py-0.5 rounded text-[10px] \'+statusColor(c.status)+\'">\'+(c.status||\'?\')+\'</span></td>\'+\n      \'<td class="p-2 text-[var(--text-secondary)]">\'+relTime(c.timestamp)+\'</td>\'+\n      \'<td class="p-2"><button type="button" class="mono text-[var(--accent-cyan)] underline decoration-dotted" data-copy="\'+(c.directive||\'\').replace(/"/g,\'&quot;\')+\'">\'+(c.directive||\'—\')+\'</button></td>\'+\n      \'<td class="p-2 mono text-[10px] text-[var(--text-secondary)]" title="\'+(c.directive_confidence_detail||\'\').replace(/"/g,\'&quot;\')+\'">\'+(c.directive_confidence!=null&&!isNaN(c.directive_confidence)?String(Math.round(Number(c.directive_confidence)*100))+\'%\' : \'-\')+\'</td>\'+\n      \'<td class="p-2 max-w-xs truncate" title="\'+(c.directive_title||\'\').replace(/"/g,\'&quot;\')+\'">\'+(c.directive_title||\'\')+\'</td>\'+\n      \'<td class="p-2">\'+ch+\'</td>\'+\n      \'<td class="p-2 \'+dClass+\'">\'+dStr+\'</td>\'+\n      \'<td class="p-2 max-w-[200px] truncate text-[var(--text-muted)]" title="\'+err.replace(/"/g,\'&quot;\')+\'">\'+err+\'</td>\';\n    tb.appendChild(tr);\n  });\n  tb.querySelectorAll(\'[data-copy]\').forEach(function(btn){\n    btn.addEventListener(\'click\', function(){ try { navigator.clipboard.writeText(btn.getAttribute(\'data-copy\')||\'\'); } catch(e){} });\n  });\n}\nfunction renderAll(d) {\n  var m = d.metrics || {};\n  var tpr = normPct(m.test_pass_rate);\n  var cov = normPct(m.coverage_pct);\n  gauge(\'g-tpr\', tpr, tpr>=95?{fg:\'#4ade80\',bg:\'#1e1e3a\'}:(tpr>=90?{fg:\'#fbbf24\',bg:\'#1e1e3a\'}:{fg:\'#f87171\',bg:\'#1e1e3a\'}));\n  gauge(\'g-cov\', cov, {fg:\'#22d3ee\',bg:\'#1e1e3a\'});\n  gauge(\'mg-tpr\', tpr, {fg:\'#4ade80\',bg:\'#1e1e3a\'});\n  gauge(\'mg-lcov\', cov, {fg:\'#22d3ee\',bg:\'#1e1e3a\'});\n  gauge(\'mg-bc\', normPct(m.branch_coverage_pct), {fg:\'#a78bfa\',bg:\'#1e1e3a\'});\n  var dur = Number(m.test_duration_s) || 0;\n  var durCol = dur < 30 ? \'#4ade80\' : (dur < 60 ? \'#fbbf24\' : \'#f87171\');\n  var durPct = Math.min(100, dur / 180 * 100);\n  gauge(\'mg-dur\', durPct, {fg:durCol,bg:\'#1e1e3a\'});\n\n  var mem = d.memory || {};\n  document.getElementById(\'stat-total\').textContent = mem.total_cycles != null ? mem.total_cycles : \'—\';\n  document.getElementById(\'stat-brk\').textContent = (mem.completed||0)+\'✓ \'+(mem.failed||0)+\'✗ \'+(mem.vetoed||0)+\'⊘\';\n  document.getElementById(\'stat-tests\').textContent = m.test_count != null ? m.test_count : \'—\';\n  document.getElementById(\'stat-tests-sub\').textContent = (m.test_passed!=null&&m.test_skipped!=null) ? (\'passed \'+m.test_passed+\' · skipped \'+m.test_skipped) : \'\';\n  var v = d.vision || {};\n  document.getElementById(\'stat-evo\').textContent = v.evolution_count != null ? v.evolution_count : \'0\';\n\n  var act = document.getElementById(\'activity-feed\');\n  if (act) {\n    act.innerHTML = \'\';\n    (d.activity_cycles || []).forEach(function(c) {\n      var div = document.createElement(\'div\');\n      div.className = \'flex flex-wrap gap-2 items-baseline border-b border-[var(--border)]/50 py-2\';\n      var title = (c.directive_title||\'\').slice(0,60);\n      var dt = c.delta;\n      var dStr = (dt == null || dt === 0) ? \'\' : (\' · Δ \'+(dt>0?\'+\':\'\')+dt);\n      div.innerHTML = \'<span class="px-2 py-0.5 rounded text-[10px] \'+statusColor(c.status)+\'">\'+(c.status||\'\')+\'</span>\'+\n        \'<span class="mono text-[var(--accent-cyan)]">\'+(c.directive||\'\')+\'</span>\'+\n        \'<span class="text-[var(--text-secondary)] flex-1 truncate">\'+title+\'</span>\'+\n        \'<span class="text-[var(--text-muted)] text-xs">\'+relTime(c.timestamp)+dStr+\'</span>\';\n      act.appendChild(div);\n    });\n  }\n\n  destroyChart(\'chart-files\');\n  var ft = d.file_touches || {};\n  var fs = d.file_successes || {};\n  var keys = Object.keys(ft).slice(0, 16);\n  var fctx = document.getElementById(\'chart-files\');\n  if (fctx && keys.length) {\n    var rates = keys.map(function(k) {\n      var t = ft[k]||1, s = fs[k]||0;\n      return s / t;\n    });\n    chartRegistry.files = new Chart(fctx, {\n      type: \'bar\',\n      data: {\n        labels: keys.map(function(k){ return k.length>24?k.slice(0,22)+\'…\':k; }),\n        datasets: [{ label: \'Touches\', data: keys.map(function(k){ return ft[k]; }),\n          backgroundColor: rates.map(function(r){ return r>0.8?\'#4ade80\':(r>=0.5?\'#fbbf24\':\'#f87171\'); }) }]\n      },\n      options: {\n        indexAxis: \'y\',\n        plugins: { legend: { display: false } },\n        scales: { x: { grid: { color: \'#1e1e3a\' }, ticks: { color: \'#94a3b8\' } }, y: { grid: { display: false }, ticks: { color: \'#94a3b8\', font: { size: 9 } } } }\n      }\n    });\n  }\n\n  destroyChart(\'chart-traj\');\n  var tctx = document.getElementById(\'chart-traj\');\n  var traj = d.test_pass_trajectory || [];\n  if (tctx && traj.length) {\n    chartRegistry.traj = new Chart(tctx, {\n      type: \'line\',\n      data: {\n        labels: traj.map(function(p){ return p.directive || \'\'; }),\n        datasets: [\n          { label: \'test_pass_rate\', data: traj.map(function(p){ return p.value != null ? (p.value<=1?p.value:p.value/100) : null; }), borderColor: \'#22d3ee\', backgroundColor: \'rgba(34,211,238,0.1)\', tension: 0.2, fill: true },\n          { label: \'target\', data: traj.map(function(){ return 1; }), borderColor: \'#475569\', borderDash: [4,4], pointRadius: 0 }\n        ]\n      },\n      options: {\n        scales: {\n          y: { min: 0, max: 1.05, grid: { color: \'#1e1e3a\' } },\n          x: { ticks: { maxRotation: 45, font: { size: 8 } }, grid: { color: \'#1e1e3a\' } }\n        },\n        plugins: { legend: { labels: { color: \'#94a3b8\' } } }\n      }\n    });\n  }\n\n  destroyChart(\'chart-covtrend\');\n  var covt = document.getElementById(\'chart-covtrend\');\n  var trend = d.product_coverage_trend || [];\n  if (covt && trend.length) {\n    chartRegistry.covt = new Chart(covt, {\n      type: \'line\',\n      data: {\n        labels: trend.map(function(r){ return r.directive || (r.timestamp||\'\').slice(0,10); }),\n        datasets: [\n          { label: \'coverage_pct\', data: trend.map(function(r){ return r.coverage_pct; }), borderColor: \'#22d3ee\', tension: 0.2 },\n          { label: \'branch_coverage_pct\', data: trend.map(function(r){ return r.branch_coverage_pct; }), borderColor: \'#a78bfa\', tension: 0.2 }\n        ]\n      },\n      options: { scales: { y: { grid: { color: \'#1e1e3a\' } }, x: { grid: { color: \'#1e1e3a\' } } }, plugins: { legend: { labels: { color: \'#94a3b8\' } } } }\n    });\n  }\n\n  var mt = document.getElementById(\'tbl-metrics\');\n  if (mt) {\n    mt.innerHTML = \'\';\n    var i = 0;\n    Object.keys(m).sort().forEach(function(k) {\n      if (k === \'low_coverage_files\') return;\n      var tr = document.createElement(\'tr\');\n      tr.className = i++ % 2 ? \'bg-[var(--bg-secondary)]/50\' : \'\';\n      var val = typeof m[k] === \'object\' ? JSON.stringify(m[k]) : String(m[k]);\n      tr.innerHTML = \'<td class="p-2 text-[var(--accent-cyan)]">\'+k+\'</td><td class="p-2">\'+val.replace(/</g,\'&lt;\')+\'</td>\';\n      mt.appendChild(tr);\n    });\n  }\n\n  var vx = d.vision || {};\n  document.getElementById(\'vision-stmt\').textContent = vx.statement || \'(no vision in KG)\';\n  var pills = document.getElementById(\'vision-pills\');\n  pills.innerHTML = \'\';\n  [[\'target_users\',\'Users\'], [\'core_value\',\'Value\'], [\'north_star_metric\',\'North star\']].forEach(function(x) {\n    if (vx[x[0]]) {\n      var s = document.createElement(\'span\');\n      s.className = \'text-xs px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border)]\';\n      s.textContent = x[1]+\': \'+vx[x[0]];\n      pills.appendChild(s);\n    }\n  });\n  document.getElementById(\'vision-meta\').textContent = \'Evolution: \'+(vx.evolution_count||0)+\' · Last: \'+(vx.last_evolved_at || \'—\');\n  var wanted = vx.features_wanted || [];\n  document.getElementById(\'badge-want\').textContent = wanted.length;\n  var lw = document.getElementById(\'list-want\');\n  lw.innerHTML = \'\';\n  wanted.forEach(function(w) {\n    var el = document.createElement(\'div\');\n    el.className = \'card p-2 text-sm border border-[var(--border)]\';\n    el.innerHTML = \'<span class="text-[10px] text-[var(--accent-amber)] float-right">→ BUILD</span><div>\'+String(w).replace(/</g,\'&lt;\')+\'</div>\';\n    lw.appendChild(el);\n  });\n  var done = vx.features_done || [];\n  document.getElementById(\'badge-done\').textContent = done.length;\n  var ld = document.getElementById(\'list-done\');\n  ld.innerHTML = \'\';\n  done.forEach(function(w) {\n    var el = document.createElement(\'div\');\n    el.className = \'card p-2 text-sm border border-[var(--border)]\';\n    el.innerHTML = \'<span class="text-[10px] text-[var(--accent-green)] float-right">✓ DONE</span><div class="mono">\'+String(w).replace(/</g,\'&lt;\')+\'</div>\';\n    ld.appendChild(el);\n  });\n  var oos = document.getElementById(\'list-oos\');\n  oos.innerHTML = \'\';\n  (vx.out_of_scope || []).forEach(function(w) {\n    var li = document.createElement(\'li\');\n    li.innerHTML = \'<span class="text-[var(--accent-red)]">✗</span> \'+String(w).replace(/</g,\'&lt;\');\n    oos.appendChild(li);\n  });\n\n  var ks = d.kg_stats || {};\n  document.getElementById(\'kg-nodes\').textContent = ks.total_nodes != null ? ks.total_nodes : \'—\';\n  document.getElementById(\'kg-edges\').textContent = ks.total_edges != null ? ks.total_edges : \'—\';\n  var ul = document.getElementById(\'kg-top-files\');\n  ul.innerHTML = \'\';\n  (ks.top_files_by_edges || []).forEach(function(x) {\n    var li = document.createElement(\'li\');\n    li.textContent = (x.id||\'\') + \' — \' + (x.edges||0) + \' edges\';\n    ul.appendChild(li);\n  });\n  destroyChart(\'chart-kg-donut\');\n  var donut = document.getElementById(\'chart-kg-donut\');\n  var cbt = ks.counts_by_type || [];\n  if (donut && cbt.length) {\n    chartRegistry.kgd = new Chart(donut, {\n      type: \'doughnut\',\n      data: {\n        labels: cbt.map(function(x){ return x[0]; }),\n        datasets: [{ data: cbt.map(function(x){ return x[1]; }), backgroundColor: [\'#22d3ee\',\'#64748b\',\'#a78bfa\',\'#4ade80\',\'#fbbf24\',\'#f87171\'] }]\n      },\n      options: { plugins: { legend: { position: \'right\', labels: { color: \'#94a3b8\' } } } }\n    });\n  }\n  var kfn = document.getElementById(\'kg-feed-n\');\n  kfn.innerHTML = \'\';\n  (d.kg_recent_nodes || []).forEach(function(n) {\n    var colors = { directive:\'border-cyan-500\', file:\'border-slate-500\', entity:\'border-purple-500\', metric:\'border-green-500\', vision:\'border-amber-500\' };\n    var bc = colors[n.node_type] || \'border-slate-600\';\n    var div = document.createElement(\'div\');\n    div.className = \'flex gap-2 items-start border-l-2 pl-2 \'+bc;\n    div.innerHTML = \'<span class="text-[10px] px-1 rounded bg-[var(--bg-secondary)]">\'+(n.node_type||\'\')+\'</span>\'+\n      \'<span class="mono text-[var(--accent-cyan)]">\'+(n.id||\'\')+\'</span>\'+\n      \'<span class="text-[var(--text-secondary)]">\'+(n.name||\'\')+\'</span>\'+\n      \'<span class="text-[var(--text-muted)] text-xs truncate">\'+(n.summary||\'\')+\'</span>\';\n    kfn.appendChild(div);\n  });\n  var kfe = document.getElementById(\'kg-feed-e\');\n  kfe.innerHTML = \'\';\n  (d.kg_recent_edges || []).forEach(function(e) {\n    var tr = document.createElement(\'tr\');\n    tr.className = \'border-b border-[var(--border)]/40\';\n    tr.innerHTML = \'<td class="p-2"><span class="px-1 rounded bg-[var(--bg-secondary)] text-[10px]">\'+(e.relation||\'\')+\'</span></td>\'+\n      \'<td class="p-2 mono text-xs">\'+(e.src_id||\'\')+\'</td><td class="p-2 mono text-xs">→ \'+(e.dst_id||\'\')+\'</td>\';\n    kfe.appendChild(tr);\n  });\n\n  var mb = document.getElementById(\'mem-bar\');\n  var mems = document.getElementById(\'mem-stat\');\n  if (mb && mem) {\n    var tot = mem.total_cycles || 0, comp = mem.completed||0, fail = mem.failed||0, vet = mem.vetoed||0;\n    mb.innerHTML = \'<div title="completed" style="width:\'+(tot?comp/tot*100:0)+\'%;background:#4ade80"></div>\'+\n      \'<div title="failed" style="width:\'+(tot?fail/tot*100:0)+\'%;background:#f87171"></div>\'+\n      \'<div title="vetoed" style="width:\'+(tot?vet/tot*100:0)+\'%;background:#fbbf24"></div>\';\n    mems.textContent = tot+\' total · \'+comp+\'✓ \'+fail+\'✗ \'+vet+\'⊘\';\n  }\n  var bcb = document.getElementById(\'mem-bcb\');\n  if (bcb) {\n    bcb.innerHTML = \'<div>baseline: <span class="text-[var(--accent-cyan)]">\'+(mem.metric_baseline!=null?mem.metric_baseline:\'—\')+\'</span></div>\'+\n      \'<div>current: <span class="text-[var(--accent-green)]">\'+(mem.metric_current!=null?mem.metric_current:\'—\')+\'</span></div>\'+\n      \'<div>best: <span class="text-[var(--accent-amber)]">\'+(mem.metric_best!=null?mem.metric_best:\'—\')+\'</span></div>\';\n  }\n  var chain = document.getElementById(\'mem-chain\');\n  chain.innerHTML = \'\';\n  var dirs = mem.directives || [];\n  for (var i = 0; i < dirs.length; i += 6) {\n    var row = document.createElement(\'div\');\n    row.className = \'flex flex-wrap items-center gap-1 w-full mb-2\';\n    var chunk = dirs.slice(i, i+6);\n    chunk.forEach(function(d, j) {\n      var st = d.status;\n      var col = st===\'COMPLETED\'?\'bg-emerald-900/60 text-emerald-300\':(st===\'VETOED\'?\'bg-amber-900/50 text-amber-300\':\'bg-red-900/50 text-red-300\');\n      var g = st===\'COMPLETED\'?\'✓\':(st===\'VETOED\'?\'⊘\':\'✗\');\n      var span = document.createElement(\'span\');\n      span.className = \'mono text-xs px-2 py-1 rounded \'+col;\n      span.textContent = (d.id||\'\')+\' \'+g;\n      row.appendChild(span);\n      if (j < chunk.length-1) {\n        var ar = document.createElement(\'span\');\n        ar.className = \'text-[var(--text-muted)]\';\n        ar.textContent = \'→\';\n        row.appendChild(ar);\n      }\n    });\n    chain.appendChild(row);\n  }\n  document.getElementById(\'mem-mmap\').textContent = mem.ascii_map || (d.memory_enabled ? \'(no memory yet)\' : \'(memory disabled)\');\n\n  renderCycles(\'tbl-maint\', d.maintenance_cycles || []);\n  renderCycles(\'tbl-prod\', d.product_cycles || []);\n}\n\nvar __loadTimer = null;\nfunction loadData() {\n  if (__loadTimer) clearTimeout(__loadTimer);\n  __loadTimer = setTimeout(function() {\n  fetch(\'/data\').then(function(r){ return r.json(); }).then(function(d){ renderAll(d); }).catch(function(){\n    document.getElementById(\'dash-live-banner\').classList.remove(\'hidden\');\n  });\n  }, 150);\n}\n\ndocument.querySelectorAll(\'.nav-btn\').forEach(function(b) {\n  b.addEventListener(\'click\', function() {\n    document.querySelectorAll(\'.nav-btn\').forEach(function(x){ x.classList.remove(\'active\',\'border-[var(--accent-cyan)]\'); });\n    b.classList.add(\'active\',\'border-[var(--accent-cyan)]\');\n    var t = b.getAttribute(\'data-target\');\n    document.querySelectorAll(\'.dash-section\').forEach(function(s){ s.classList.add(\'hidden\'); });\n    var el = document.getElementById(t);\n    if (el) el.classList.remove(\'hidden\');\n  });\n});\ndocument.getElementById(\'tab-maint\').addEventListener(\'click\', function() {\n  document.getElementById(\'panel-maint\').classList.remove(\'hidden\');\n  document.getElementById(\'panel-prod\').classList.add(\'hidden\');\n  document.getElementById(\'tab-maint\').classList.add(\'border-[var(--accent-cyan)]\');\n  document.getElementById(\'tab-prod\').classList.remove(\'border-[var(--accent-cyan)]\');\n});\ndocument.getElementById(\'tab-prod\').addEventListener(\'click\', function() {\n  document.getElementById(\'panel-prod\').classList.remove(\'hidden\');\n  document.getElementById(\'panel-maint\').classList.add(\'hidden\');\n  document.getElementById(\'tab-prod\').classList.add(\'border-[var(--accent-cyan)]\');\n  document.getElementById(\'tab-maint\').classList.remove(\'border-[var(--accent-cyan)]\');\n});\n\nloadData();\ntry {\n  var es = new EventSource(\'/events\');\n  es.onmessage = function() { loadData(); };\n  es.onerror = function() { document.getElementById(\'dash-live-banner\').classList.remove(\'hidden\'); };\n} catch(e) { document.getElementById(\'dash-live-banner\').classList.remove(\'hidden\'); }\n})();\n</script>\n</body>\n</html>\n'
    return html.replace('__PRJ__', proj)
def build_dashboard_snapshot_dict(cfg: HarnessConfig) -> dict[str, Any]:
    """Single JSON snapshot for /api/dashboard.json and SSE payloads (no seq / generated_at)."""
    return _compute_dashboard_payload(cfg)


def _watch_signature(cfg: HarnessConfig) -> tuple[tuple[str, float], ...]:
    parts: list[tuple[str, float]] = []
    for p in sorted(cfg.maintenance_cycles_dir.glob("*.json")):
        if p.is_file():
            parts.append((str(p.resolve()), p.stat().st_mtime))
    for p in sorted(cfg.product_cycles_dir.glob("*.json")):
        if p.is_file():
            parts.append((str(p.resolve()), p.stat().st_mtime))
    mp = resolve_latest_metrics_path(cfg)
    if mp is not None and mp.is_file():
        parts.append((str(mp.resolve()), mp.stat().st_mtime))
    kg = cfg.kg_path
    if kg.is_file():
        parts.append((str(kg.resolve()), kg.stat().st_mtime))
    return tuple(parts)


class DashboardRuntime:
    """Shared dashboard state: monotonic seq, latest snapshot JSON, file-watch refreshes."""

    def __init__(self, cfg: HarnessConfig) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.seq = 0
        self.snapshot: dict[str, Any] = {}
        self._stop = threading.Event()

    def refresh_from_disk(self) -> None:
        base = _compute_dashboard_payload(self.cfg)
        with self.lock:
            self.seq += 1
            out = dict(base)
            out["seq"] = self.seq
            out["generated_at"] = _utc_now_iso()
            self.snapshot = out

    def close(self) -> None:
        self._stop.set()


def _watch_loop(rt: DashboardRuntime) -> None:
    last_sig = _watch_signature(rt.cfg)
    while not rt._stop.wait(WATCH_POLL_SEC):
        sig = _watch_signature(rt.cfg)
        if sig == last_sig:
            continue
        last_sig = sig
        rt.refresh_from_disk()


def _write_sse_chunk(handler: BaseHTTPRequestHandler, text: str) -> bool:
    try:
        handler.wfile.write(text.encode("utf-8"))
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False
    return True


def _dashboard_handler_factory(cfg: HarnessConfig, runtime: DashboardRuntime):
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/health":
                body = b"OK"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/data":
                body = build_data_json(cfg).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/dashboard.json":
                with runtime.lock:
                    snap = dict(runtime.snapshot)
                body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path in ("/events", "/api/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last_sent = 0
                last_beat = time.monotonic()
                while not runtime._stop.is_set():
                    with runtime.lock:
                        snap = dict(runtime.snapshot) if runtime.snapshot else {}
                        cur_seq = int(snap.get("seq", 0))
                    if cur_seq > last_sent:
                        line = "data: " + json.dumps(snap, ensure_ascii=False) + "\n\n"
                        if not _write_sse_chunk(self, line):
                            break
                        last_sent = cur_seq
                        last_beat = time.monotonic()
                    elif time.monotonic() - last_beat >= SSE_HEARTBEAT_SEC:
                        if not _write_sse_chunk(self, ": ping\n\n"):
                            break
                        last_beat = time.monotonic()
                    time.sleep(0.4)
                return
            if path not in ("/", "/index.html"):
                self.send_error(404, "Not Found")
                return
            body = build_dashboard_html(cfg).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


def run_dashboard_server(cfg: HarnessConfig, host: str, port: int) -> None:
    console = Console()
    runtime = DashboardRuntime(cfg)
    runtime.refresh_from_disk()
    watcher = threading.Thread(target=_watch_loop, args=(runtime,), daemon=True)
    watcher.start()
    handler = _dashboard_handler_factory(cfg, runtime)
    server = ThreadingHTTPServer((host, port), handler)
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except OSError:
        local_ip = "127.0.0.1"
    console.print(f"[dim]Local network: http://{local_ip}:{port}/[/dim]")
    try:
        server.serve_forever()
    finally:
        runtime.close()
        server.server_close()
