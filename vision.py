"""
meta_harness/vision.py

Product vision stored in the knowledge graph (live) with TOML [vision] as seed only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import HarnessConfig
    from .knowledge_graph import KnowledgeGraph

logger = logging.getLogger(__name__)

VISION_NODE_ID = "vision:current"


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def load_vision(kg: "KnowledgeGraph") -> dict[str, Any]:
    n = kg.get_node(VISION_NODE_ID)
    if not n:
        return {}
    data = n.get("data")
    return dict(data) if isinstance(data, dict) else {}


def seed_vision(kg: "KnowledgeGraph", cfg: "HarnessConfig") -> None:
    try:
        existing = kg.get_node(VISION_NODE_ID)
        if existing:
            return
        data = {
            "statement": cfg.vision.statement,
            "target_users": cfg.vision.target_users,
            "core_value": cfg.vision.core_value,
            "north_star_metric": cfg.vision.north_star_metric,
            "features_wanted": list(cfg.vision.features_wanted),
            "features_done": list(cfg.vision.features_done),
            "out_of_scope": list(cfg.vision.out_of_scope),
            "last_evolved_at": _now(),
            "evolution_count": 0,
        }
        stmt = cfg.vision.statement or ""
        kg.upsert_node(
            VISION_NODE_ID,
            "vision",
            name="Product Vision",
            summary=stmt[:200],
            status="active",
            data=data,
        )
    except Exception as e:
        logger.warning("seed_vision failed: %s", e)


def derive_features_done(kg: "KnowledgeGraph") -> list[str]:
    try:
        rows = kg._conn.execute(
            """
            SELECT name FROM nodes
            WHERE node_type = 'directive'
              AND status = 'COMPLETED'
              AND (id LIKE 'P%' OR json_extract(data_json, '$.layer') = 'product')
            ORDER BY updated_at DESC
            LIMIT 50
            """
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception as e:
        logger.warning("derive_features_done failed: %s", e)
        return []


def derive_out_of_scope(kg: "KnowledgeGraph") -> list[str]:
    try:
        rows = kg._conn.execute(
            """
            SELECT name FROM nodes
            WHERE node_type = 'directive'
              AND status = 'VETOED'
            ORDER BY updated_at DESC
            LIMIT 20
            """
        ).fetchall()
        return [f"Previously vetoed: {r[0]}" for r in rows if r[0]]
    except Exception as e:
        logger.warning("derive_out_of_scope failed: %s", e)
        return []


def derive_research_influences(cfg: "HarnessConfig") -> list[str]:
    path = cfg.research_queue_path
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("derive_research_influences read failed: %s", e)
        return []
    items: list[dict[str, Any]]
    if isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict)]
    elif isinstance(raw, dict) and isinstance(raw.get("items"), list):
        items = [x for x in raw["items"] if isinstance(x, dict)]
    else:
        return []
    out: list[str] = []
    for it in items[:10]:
        title = str(it.get("title", "") or "").strip()
        app = str(it.get("applicable_to", "") or "").strip()
        out.append(f"Research: {title} -> {app}")
    return out


def _feature_covered(wanted: str, done_list: list[str]) -> bool:
    w = wanted.lower()
    for d in done_list:
        d_lower = d.lower()
        words: list[str] = []
        for raw in w.split():
            for tok in re.split(r"[-_/]+", raw):
                t = tok.lower()
                if len(t) > 2:
                    words.append(t)
        if not words:
            continue
        matches = sum(1 for word in words if word in d_lower)
        if matches / len(words) >= 0.5:
            return True
    return False


def _research_paper_node_id(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return f"research:{h}"


def evolve_vision(
    kg: "KnowledgeGraph",
    cfg: "HarnessConfig",
    completed_directive_id: str | None = None,
) -> dict[str, Any]:
    try:
        v = load_vision(kg)
        if not v:
            seed_vision(kg, cfg)
            v = load_vision(kg)
        if not v:
            return {}

        done_titles = derive_features_done(kg)
        veto_scope = derive_out_of_scope(kg)
        research_lines = derive_research_influences(cfg)

        wanted = [str(x) for x in v.get("features_wanted", []) if str(x).strip()]
        for line in research_lines:
            if not line.strip():
                continue
            if line in wanted:
                continue
            if _feature_covered(line, done_titles):
                continue
            wanted.append(line)

        filtered: list[str] = []
        for w in wanted:
            if _feature_covered(w, done_titles):
                continue
            filtered.append(w)

        evo = int(v.get("evolution_count", 0) or 0) + 1
        ts = _now()
        data = {
            "statement": v.get("statement", ""),
            "target_users": v.get("target_users", ""),
            "core_value": v.get("core_value", ""),
            "north_star_metric": v.get("north_star_metric", ""),
            "features_wanted": filtered,
            "features_done": done_titles,
            "out_of_scope": veto_scope,
            "last_evolved_at": ts,
            "evolution_count": evo,
        }
        stmt = str(data.get("statement", "") or "")
        kg.upsert_node(
            VISION_NODE_ID,
            "vision",
            name="Product Vision",
            summary=stmt[:200],
            status="active",
            data=data,
        )

        if completed_directive_id:
            try:
                kg.add_edge(VISION_NODE_ID, completed_directive_id, "informed_by")
            except Exception as e:
                logger.warning("informed_by edge failed: %s", e)

        path = cfg.research_queue_path
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    qitems = [x for x in raw if isinstance(x, dict)]
                elif isinstance(raw, dict) and isinstance(raw.get("items"), list):
                    qitems = [x for x in raw["items"] if isinstance(x, dict)]
                else:
                    qitems = []
                for it in qitems[:10]:
                    url = str(it.get("url", "") or "").strip()
                    if not url:
                        continue
                    rid = _research_paper_node_id(url)
                    title = str(it.get("title", "") or "")[:200]
                    try:
                        kg.upsert_node(
                            rid,
                            "research_paper",
                            name=title or url[:80],
                            summary=url[:500],
                            data={"url": url},
                        )
                        kg.add_edge(VISION_NODE_ID, rid, "influenced_by")
                    except Exception as e:
                        logger.warning("influenced_by edge failed: %s", e)
            except Exception as e:
                logger.warning("research queue read for edges failed: %s", e)

        return dict(data)
    except Exception as e:
        logger.warning("evolve_vision failed: %s", e)
        return {}


def vision_prompt_block(kg: "KnowledgeGraph", cfg: "HarnessConfig") -> str:
    try:
        v = load_vision(kg)
        if not v:
            seed_vision(kg, cfg)
            v = load_vision(kg)
        if not v:
            return "## Product vision\n(not yet initialized)\n"

        features_done = derive_features_done(kg)
        out_of_scope = derive_out_of_scope(kg)

        lines = [
            "## Product vision",
            v.get("statement", "").strip() or "(not configured)",
            "",
            f"**Target users:** {v.get('target_users', '') or '--'}",
            f"**Core value:** {v.get('core_value', '') or '--'}",
            f"**North star:** {v.get('north_star_metric', '') or '--'}",
            "",
            "### Features wanted (build these)",
            "\n".join(f"- {x}" for x in v.get("features_wanted", [])) or "- (none listed)",
            "",
            "### Already shipped (do not re-propose)",
            "\n".join(f"- {x}" for x in features_done[:20]) or "- (none)",
            "",
            "### Out of scope (do not propose these)",
            "\n".join(f"- {x}" for x in out_of_scope[:10]) or "- (none)",
            "",
            "### Vision evolution",
            f"Evolution count: {v.get('evolution_count', 0)} | "
            f"Last evolved: {v.get('last_evolved_at', 'never')[:10]}",
        ]
        return "\n".join(lines)
    except Exception as e:
        logger.warning("vision_prompt_block failed: %s", e)
        return _vision_prompt_block_toml(cfg)


def _vision_prompt_block_toml(cfg: "HarnessConfig") -> str:
    v = cfg.vision
    lines = [
        "## Product vision",
        v.statement.strip() or "(not configured — infer from project description)",
        "",
        f"**Target users:** {v.target_users.strip() or '—'}",
        f"**Core value:** {v.core_value.strip() or '—'}",
        f"**North star:** {v.north_star_metric.strip() or '—'}",
        "",
        "### Features wanted",
        "\n".join(f"- {x}" for x in v.features_wanted) or "- (none listed)",
        "",
        "### Already shipped (do not re-propose as net-new)",
        "\n".join(f"- {x}" for x in v.features_done) or "- (none listed)",
        "",
        "### Out of scope",
        "\n".join(f"- {x}" for x in v.out_of_scope) or "- (none listed)",
    ]
    return "\n".join(lines)
