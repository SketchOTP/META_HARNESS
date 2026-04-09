"""
meta_harness/knowledge_graph.py

SQLite knowledge graph: nodes, typed edges, FTS5, causal traversal.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .memory import _failure_hint, _normalize_failure_detail


_DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  node_type TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  data_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_id TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  data_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(relation);
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
  node_id UNINDEXED,
  body
);
"""


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


@dataclass
class GraphNode:
    id: str
    type: str
    name: str = ""
    summary: str = ""
    status: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    id: int
    src_id: str
    dst_id: str
    relation: str
    data: dict = field(default_factory=dict)
    created_at: str = ""


class KnowledgeGraph:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _row_to_node(self, row: sqlite3.Row) -> GraphNode:
        return GraphNode(
            id=row["id"],
            type=row["node_type"],
            name=row["name"] or "",
            summary=row["summary"] or "",
            status=row["status"] or "",
            data=json.loads(row["data_json"] or "{}"),
        )

    def upsert_node(
        self,
        node_id: str,
        node_type: str,
        *,
        name: str = "",
        summary: str = "",
        status: str = "",
        data: Optional[dict] = None,
    ) -> None:
        data = data or {}
        ts = _now()
        cur = self._conn.execute("SELECT id FROM nodes WHERE id = ?", (node_id,))
        exists = cur.fetchone() is not None
        if exists:
            self._conn.execute(
                """UPDATE nodes SET node_type=?, name=?, summary=?, status=?, data_json=?, updated_at=?
                   WHERE id=?""",
                (node_type, name, summary, status, json.dumps(data), ts, node_id),
            )
        else:
            self._conn.execute(
                """INSERT INTO nodes (id, node_type, name, summary, status, data_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (node_id, node_type, name, summary, status, json.dumps(data), ts, ts),
            )
        self._sync_fts(node_id, name, summary)
        self._conn.commit()

    def _sync_fts(self, node_id: str, name: str, summary: str) -> None:
        self._conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))
        body = f"{name}\n{summary}".strip()
        if body:
            self._conn.execute(
                "INSERT INTO nodes_fts (node_id, body) VALUES (?, ?)",
                (node_id, body),
            )

    def get_node(self, node_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return None
        n = self._row_to_node(row)
        return {
            "id": n.id,
            "type": n.type,
            "name": n.name,
            "summary": n.summary,
            "status": n.status,
            "data": n.data,
        }

    def add_edge(
        self,
        src_id: str,
        dst_id: str,
        relation: str,
        data: Optional[dict] = None,
        *,
        dedupe: bool = True,
    ) -> None:
        data = data or {}
        if dedupe:
            cur = self._conn.execute(
                "SELECT 1 FROM edges WHERE src_id=? AND dst_id=? AND relation=?",
                (src_id, dst_id, relation),
            )
            if cur.fetchone():
                return
        self._conn.execute(
            """INSERT INTO edges (src_id, dst_id, relation, data_json, created_at)
               VALUES (?,?,?,?,?)""",
            (src_id, dst_id, relation, json.dumps(data), _now()),
        )
        self._conn.commit()

    def get_edges(
        self,
        *,
        src_id: Optional[str] = None,
        dst_id: Optional[str] = None,
        relation: Optional[str] = None,
    ) -> list[GraphEdge]:
        q = "SELECT * FROM edges WHERE 1=1"
        args: list[Any] = []
        if src_id is not None:
            q += " AND src_id = ?"
            args.append(src_id)
        if dst_id is not None:
            q += " AND dst_id = ?"
            args.append(dst_id)
        if relation is not None:
            q += " AND relation = ?"
            args.append(relation)
        q += " ORDER BY id ASC"
        rows = self._conn.execute(q, args).fetchall()
        return [
            GraphEdge(
                id=r["id"],
                src_id=r["src_id"],
                dst_id=r["dst_id"],
                relation=r["relation"],
                data=json.loads(r["data_json"] or "{}"),
                created_at=r["created_at"] or "",
            )
            for r in rows
        ]

    def neighbors(
        self,
        node_id: str,
        *,
        direction: str = "out",
        relation: Optional[str] = None,
    ) -> list[str]:
        ids: list[str] = []
        if direction in ("out", "both"):
            q = "SELECT dst_id FROM edges WHERE src_id = ?"
            a: list[Any] = [node_id]
            if relation:
                q += " AND relation = ?"
                a.append(relation)
            ids.extend(r[0] for r in self._conn.execute(q, a).fetchall())
        if direction in ("in", "both"):
            q = "SELECT src_id FROM edges WHERE dst_id = ?"
            a = [node_id]
            if relation:
                q += " AND relation = ?"
                a.append(relation)
            ids.extend(r[0] for r in self._conn.execute(q, a).fetchall())
        seen: set[str] = set()
        out: list[str] = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    def subgraph(self, start_id: str, depth: int = 1) -> set[str]:
        """BFS up to `depth` hops from start (depth=1 => start + immediate neighbors)."""
        if depth < 1:
            return {start_id}
        seen: set[str] = {start_id}
        frontier = deque([(start_id, 0)])
        while frontier:
            nid, d = frontier.popleft()
            if d >= depth:
                continue
            for nb in self.neighbors(nid, direction="both"):
                if nb not in seen:
                    seen.add(nb)
                    frontier.append((nb, d + 1))
        return seen

    def causal_chain(self, start_id: str) -> list[str]:
        """Follow broke → fixed edges in order (simple walk from start)."""
        order: list[str] = [start_id]
        cur = start_id
        visited = {start_id}
        while True:
            nxt = None
            for e in self.get_edges(src_id=cur, relation="broke"):
                if e.dst_id not in visited:
                    nxt = e.dst_id
                    break
            if nxt is None:
                for e in self.get_edges(src_id=cur, relation="fixed"):
                    if e.dst_id not in visited:
                        nxt = e.dst_id
                        break
            if nxt is None:
                break
            order.append(nxt)
            visited.add(nxt)
            cur = nxt
        return order

    def file_history(self, file_path: str) -> list[dict]:
        fid = f"file:{file_path}"
        edges = self.get_edges(dst_id=fid, relation="modified")
        edges.sort(key=lambda e: e.created_at, reverse=True)
        return [{"src_id": e.src_id, "created_at": e.created_at, "data": e.data} for e in edges]

    def entity_history(self, entity_id: str) -> list[dict]:
        edges = self.get_edges(dst_id=entity_id, relation="changed")
        edges.sort(key=lambda e: e.created_at, reverse=False)
        return [{"src_id": e.src_id, "value": e.data.get("value"), "created_at": e.created_at} for e in edges]

    def search(self, query: str, *, limit: int = 20) -> list[str]:
        if not query.strip():
            return []
        safe = query.strip().replace('"', '""')
        try:
            rows = self._conn.execute(
                """SELECT node_id FROM nodes_fts WHERE nodes_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (safe, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = self._conn.execute(
                "SELECT node_id FROM nodes_fts WHERE body LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [r[0] for r in rows]

    def stats(self) -> dict[str, Any]:
        n = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        e = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        types: dict[str, int] = {}
        for row in self._conn.execute("SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type"):
            types[row[0]] = row[1]
        rels: dict[str, int] = {}
        for row in self._conn.execute("SELECT relation, COUNT(*) FROM edges GROUP BY relation"):
            rels[row[0]] = row[1]
        return {"nodes": n, "edges": e, "node_types": types, "relations": rels}

    def most_connected(self, limit: int = 10) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT n.id, (
              SELECT COUNT(*) FROM edges WHERE src_id = n.id OR dst_id = n.id
            ) AS c
            FROM nodes n
            ORDER BY c DESC, n.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def extract_entities(text: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(etype: str, val: str) -> None:
            val = val.strip()
            if not val:
                return
            key = (etype, val)
            if key in seen:
                return
            seen.add(key)
            out.append({"type": etype, "id": val})

        for m in re.finditer(r"\b[DMP]\d{3}[a-zA-Z_]*\b", text):
            add("directive", m.group(0))
        for m in re.finditer(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)", text):
            add("class", m.group(1))
        for m in re.finditer(
            r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*=\s*",
            text,
        ):
            add("config_key", m.group(1))
        for m in re.finditer(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', text):
            add("config_key", m.group(1))
        return out

    @staticmethod
    def _extract_assignment_value(directive_text: str, key: str) -> Optional[str]:
        pat_eq = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(key)}\s*=\s*([^\n]+)"
        )
        m = pat_eq.search(directive_text)
        if m:
            return m.group(1).strip().strip('"').strip("'").rstrip(",")
        pat_json = re.compile(rf'"{re.escape(key)}"\s*:\s*([^,\n]+)', re.M)
        m2 = pat_json.search(directive_text)
        if m2:
            return m2.group(1).strip().strip('"').strip("'")
        return None

    def ingest_cycle_outcome(
        self,
        *,
        directive_id: str,
        directive_title: str,
        directive_content: str,
        status: str,
        files_changed: list[str],
        metric_name: str = "",
        metric_before: Optional[float] = None,
        metric_after: Optional[float] = None,
        failing_tests: Optional[list[str]] = None,
        failure_detail: Optional[str] = None,
        layer: str = "maintenance",
    ) -> None:
        failing_tests = failing_tests or []

        existing = self.get_node(directive_id)
        merged: dict[str, Any] = {}
        if existing and existing.get("data"):
            merged.update(existing["data"])
        merged["title"] = directive_title
        merged["layer"] = layer

        summary = (directive_content or "")[:500]
        if failure_detail and status in (
            "AGENT_FAILED",
            "ERROR",
            "TEST_FAILED",
            "METRIC_REGRESSION",
        ):
            hint = _failure_hint(failure_detail)
            excerpt = _normalize_failure_detail(failure_detail)
            if excerpt:
                merged["error_excerpt"] = excerpt[:200]
            if hint:
                merged["failure_hint"] = hint
            fts_bits = " ".join(x for x in (hint, excerpt[:120]) if x)
            if fts_bits:
                summary = f"{summary}\n{fts_bits}"[:800]

        merged["timestamp"] = _now()

        self.upsert_node(
            directive_id,
            "directive",
            name=directive_title,
            summary=summary,
            status=status,
            data=merged,
        )

        for fp in files_changed:
            fid = f"file:{fp}"
            self.upsert_node(fid, "file", name=fp, summary="")
            self.add_edge(directive_id, fid, "modified")

        if metric_name and metric_after is not None:
            mid = f"metric:{metric_name}"
            self.upsert_node(mid, "metric", name=metric_name, summary=str(metric_after))
            self.add_edge(directive_id, mid, "referenced", {"metric": metric_name})
            if status == "COMPLETED" and metric_before is not None and metric_after > metric_before:
                self.add_edge(directive_id, mid, "improved", {"before": metric_before, "after": metric_after})

        if status == "TEST_FAILED":
            for t in failing_tests:
                tid = f"test:{t}"
                self.upsert_node(tid, "test", name=t, summary="")
                self.add_edge(directive_id, tid, "broke")

        entities = self.extract_entities(directive_content)
        for ent in entities:
            etype = ent["type"]
            eid = ent["id"]
            entity_id = f"entity:{etype}:{eid}"
            self.upsert_node(entity_id, "entity", name=eid, summary=etype, data={"entity_type": etype})
            if etype == "config_key":
                val = self._extract_assignment_value(directive_content, eid)
                if val is not None:
                    self.add_edge(directive_id, entity_id, "changed", {"value": val})
                else:
                    self.add_edge(directive_id, entity_id, "referenced", {"entity_type": etype})
            else:
                self.add_edge(directive_id, entity_id, "referenced", {"entity_type": etype})

        self._conn.commit()

    def _directive_one_liner(self, directive_id: str) -> str:
        row = self._conn.execute(
            "SELECT id, name, status FROM nodes WHERE node_type='directive' AND id = ?",
            (directive_id,),
        ).fetchone()
        if not row:
            return directive_id
        title = (row["name"] or "").strip()
        if len(title) > 48:
            title = title[:45] + "…"
        st = (row["status"] or "?")[:4]
        return f"{row['id']} {st} {title}".strip()

    def _fts_query_lines(self, query: str, *, limit: int) -> list[str]:
        if not query.strip() or limit <= 0:
            return []
        hits = self.search(query.strip()[:500], limit=limit * 3)
        out: list[str] = []
        seen: set[str] = set()
        for node_id in hits:
            if not str(node_id).startswith("D") and not str(node_id).startswith("M") and not str(node_id).startswith("P"):
                continue
            if node_id in seen:
                continue
            seen.add(node_id)
            line = self._directive_one_liner(str(node_id))
            out.append(line)
            if len(out) >= limit:
                break
        return out

    def build_compact_context(
        self,
        *,
        max_directives: int = 8,
        evidence: Any = None,
        kg_use_query_context: bool = True,
        kg_query_max_chars: int = 900,
        kg_query_max_directives: int = 6,
    ) -> str:
        rows = self._conn.execute(
            """SELECT id, name, status FROM nodes WHERE node_type='directive'
               ORDER BY updated_at DESC LIMIT ?""",
            (max_directives,),
        ).fetchall()
        if not rows:
            return ""
        lines = ["[GRAPH"]
        hot_rows = self._conn.execute(
            """SELECT dst_id, COUNT(*) AS cnt FROM edges
               WHERE relation = 'modified' AND dst_id LIKE 'file:%'
               GROUP BY dst_id ORDER BY cnt DESC LIMIT 4"""
        ).fetchall()
        if hot_rows:
            lines.append(
                "hot: " + " ".join(r[0].replace("file:", "", 1) for r in hot_rows)
            )
        wins: list[str] = []
        for r in rows:
            if r["status"] == "COMPLETED":
                wins.append(r["id"])
        if wins:
            lines.append("wins: " + " ".join(wins[:5]))

        rel_lines: list[str] = []
        win_ids = set(wins)
        if kg_use_query_context and evidence is not None:
            qparts: list[str] = []
            try:
                fn = getattr(evidence, "tests", None)
                if fn is not None and getattr(fn, "failed_names", None):
                    qparts.extend(str(x) for x in (fn.failed_names or [])[:8])
            except Exception:
                pass
            for p in getattr(evidence, "error_patterns", None) or []:
                if isinstance(p, dict) and p.get("pattern"):
                    qparts.append(str(p["pattern"])[:120])
            tr = getattr(evidence, "test_results", "") or ""
            if tr.strip():
                qparts.append(tr.strip()[-500:])
            lt = getattr(evidence, "log_tail", "") or ""
            if lt.strip():
                qparts.append(lt.strip()[-400:])
            q = " ".join(qparts).strip()[:900]
            if q:
                rel_lines = self._fts_query_lines(q, limit=kg_query_max_directives)
            seen_rel = {x.split()[0] for x in rel_lines if x.split()}
            for fp in (getattr(evidence, "git_recent_paths", None) or [])[:6]:
                if not fp:
                    continue
                for edge in self.get_edges(dst_id=f"file:{fp}", relation="modified")[:2]:
                    lid = edge.src_id
                    if lid in win_ids or lid in seen_rel:
                        continue
                    line = self._directive_one_liner(lid)
                    rel_lines.append(line)
                    seen_rel.add(lid)
                    if len(rel_lines) >= kg_query_max_directives:
                        break
                if len(rel_lines) >= kg_query_max_directives:
                    break

        if rel_lines:
            filtered: list[str] = []
            seen_ids = set(win_ids)
            for ln in rel_lines:
                pid = ln.split()[0] if ln.split() else ""
                if pid and pid not in seen_ids:
                    filtered.append(ln)
                    seen_ids.add(pid)
                if len(filtered) >= kg_query_max_directives:
                    break
            if filtered:
                lines.append("rel: " + " | ".join(filtered))

        lines.append("]")
        block = "\n".join(lines)
        if len(block) > kg_query_max_chars and kg_query_max_chars > 80:
            return block[: kg_query_max_chars - 1] + "…"
        return block

    def get_nodes_by_layer(self, layer: str, *, limit: int = 12) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT id, name, status, data_json, updated_at FROM nodes
               WHERE node_type = 'directive' ORDER BY updated_at DESC"""
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            data = json.loads(r["data_json"] or "{}")
            if infer_directive_layer(str(r["id"]), data) != layer:
                continue
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"] or "",
                    "status": r["status"] or "",
                    "data": data,
                    "updated_at": r["updated_at"] or "",
                }
            )
            if len(out) >= limit:
                break
        return out


def infer_directive_layer(directive_id: str, data: Optional[dict[str, Any]] = None) -> str:
    ly = (data or {}).get("layer") if data else None
    if ly in ("product", "maintenance"):
        return ly
    u = directive_id.upper()
    if re.match(r"^P\d{3}", u):
        return "product"
    return "maintenance"


def build_cross_layer_context(kg: KnowledgeGraph, requesting_layer: str) -> str:
    """
    Maintenance agent sees recent product directives; product agent sees maintenance health.
    """
    other = "product" if requesting_layer == "maintenance" else "maintenance"
    nodes = kg.get_nodes_by_layer(other, limit=8)
    if not nodes:
        return ""
    label = "product initiatives" if other == "product" else "maintenance / stability work"
    lines = [f"## Other layer ({label})"]
    for n in nodes:
        title = (n["data"].get("title") or n["name"] or n["id"]).strip()
        if len(title) > 100:
            title = title[:97] + "…"
        st = n["status"] or "?"
        lines.append(f"- `{n['id']}` — {title} — {st}")
    return "\n".join(lines)
