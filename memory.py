"""
meta_harness/memory.py

Persistent memory for the Meta-Harness.

Design principles:
  - Everything stored in .metaharness/memory/project_memory.json
  - Compact context injected into Cursor Agent prompts is <= ~80 tokens
  - Memory map rendered as ASCII for zero-cost review
  - Optional (disabled if [memory] enabled = false in config)

Compact context format (example, ~70 tokens):
  [MEM 15cy 9✓ 3✗ 1⊘ | acc 0.72→0.81 best=0.83]
  hot: classifier.py×8 system.txt×3
  wins: D012+.03 D009+.02 D007+.01
  miss: D014(test) D011-.01
  dead: regex-tokenizer batch>32
  works: prompt-eng→acc cfg-tune→latency
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .config import HarnessConfig
    from .evidence import Evidence
    from .knowledge_graph import KnowledgeGraph


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class DirectiveRecord:
    id: str
    title: str
    status: str          # COMPLETED | TEST_FAILED | METRIC_REGRESSION | AGENT_FAILED | VETOED | ERROR
    delta: Optional[float]
    files_changed: list[str]
    timestamp: str


@dataclass
class ProjectMemory:
    project_name: str = ""
    created_at: str = ""
    last_updated: str = ""

    # Cycle stats
    total_cycles: int = 0
    completed: int = 0
    failed: int = 0
    vetoed: int = 0

    # Metric trajectory — only keep last 20 (id, value) pairs
    metric_name: str = ""
    metric_baseline: Optional[float] = None
    metric_current: Optional[float] = None
    metric_best: Optional[float] = None
    metric_trajectory: list[list] = field(default_factory=list)  # [[id, value], ...]

    # File heat map — how many times each file was changed + success rate numerator/denominator
    file_touches: dict[str, int] = field(default_factory=dict)
    file_successes: dict[str, int] = field(default_factory=dict)

    # Directive records — last 50
    directives: list[dict] = field(default_factory=list)

    # Learned patterns (written by the harness itself via memory_update_patterns)
    success_patterns: list[str] = field(default_factory=list)
    failure_patterns: list[str] = field(default_factory=list)
    dead_ends: list[str] = field(default_factory=list)


# ── Storage ────────────────────────────────────────────────────────────────────

_ERR_STORAGE_CAP = 240
_FAILURE_HINT_MAX = 24


def _normalize_failure_detail(text: Optional[str]) -> str:
    """Collapse whitespace and cap length for stored `err` on directive records."""
    if not text:
        return ""
    line = " ".join(text.strip().split())
    if len(line) > _ERR_STORAGE_CAP:
        return line[: _ERR_STORAGE_CAP - 1] + "…"
    return line


def _failure_hint(err: str) -> str:
    """
    Short classifier for compact context / KG (token-bounded).
    Uses the first line and a few keyword classes.
    """
    if not err:
        return ""
    first = err.strip().split("\n", 1)[0]
    lower = first.lower()
    tag: str
    if "timeoutexpired" in lower or "timed out" in lower:
        tag = "timeout"
    elif "no valid json" in lower:
        tag = "no_json"
    elif "filenotfounderror" in lower or "file not found" in lower:
        tag = "no_file"
    elif "non-zero exit" in lower or "exit code" in lower or re.search(r"\bexit\s*\d", lower):
        tag = "exit"
    else:
        tag = re.sub(r"[^\w\-]+", "_", first.strip()).strip("_")[:20] or "err"
    if len(tag) > _FAILURE_HINT_MAX:
        return tag[: _FAILURE_HINT_MAX - 1] + "…"
    return tag


def _memory_path(memory_dir: Path) -> Path:
    return memory_dir / "project_memory.json"


def load(memory_dir: Path) -> ProjectMemory:
    path = _memory_path(memory_dir)
    if not path.exists():
        return ProjectMemory()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        m = ProjectMemory()
        for k, v in raw.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m
    except Exception:
        return ProjectMemory()


def get_kg(cfg: "HarnessConfig") -> "KnowledgeGraph":
    """Always return the project knowledge graph (independent of [memory] enabled)."""
    from .knowledge_graph import KnowledgeGraph

    return KnowledgeGraph(cfg.kg_path)


def persist_cycle_outcome(
    cfg: "HarnessConfig",
    *,
    kg: Optional["KnowledgeGraph"] = None,
    directive_id: str,
    directive_title: str,
    status: str,
    delta: Optional[float],
    files_changed: list[str],
    pre_metric: Optional[float],
    post_metric: Optional[float],
    directive_content: str = "",
    failing_tests: Optional[list[str]] = None,
    failure_detail: Optional[str] = None,
    layer: str = "maintenance",
) -> Optional[ProjectMemory]:
    """
    Update JSON memory when enabled; always persist cycle outcome to the knowledge graph
    (via ingest inside update, or ingest-only when memory is disabled).
    """
    graph = kg if kg is not None else get_kg(cfg)
    mem_out: Optional[ProjectMemory] = None
    if cfg.memory.enabled:
        mem_out = update(
            cfg.memory_dir,
            cfg.project.name,
            directive_id,
            directive_title,
            status,
            delta,
            files_changed,
            pre_metric,
            post_metric,
            cfg.goals.primary_metric,
            max_directive_history=cfg.cycle.max_directive_history,
            kg=graph,
            directive_content=directive_content,
            failing_tests=failing_tests or [],
            failure_detail=failure_detail,
            layer=layer,
        )
    elif directive_id:
        err_norm = _normalize_failure_detail(failure_detail) if failure_detail else None
        graph.ingest_cycle_outcome(
            directive_id=directive_id,
            directive_title=directive_title,
            directive_content=directive_content,
            status=status,
            files_changed=files_changed,
            metric_name=cfg.goals.primary_metric or "",
            metric_before=pre_metric,
            metric_after=post_metric,
            failing_tests=failing_tests or [],
            failure_detail=err_norm,
            layer=layer,
        )
    if directive_id and status == "COMPLETED" and (directive_content or "").strip():
        try:
            from . import embedding_retrieval

            embedding_retrieval.index_directive_body(cfg, directive_id, directive_content)
        except Exception:
            pass
    return mem_out


def save(memory_dir: Path, mem: ProjectMemory) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    _memory_path(memory_dir).write_text(
        json.dumps(asdict(mem), indent=2), encoding="utf-8"
    )


# ── Update after a cycle ───────────────────────────────────────────────────────

def update(
    memory_dir: Path,
    project_name: str,
    directive_id: str,
    directive_title: str,
    status: str,
    delta: Optional[float],
    files_changed: list[str],
    pre_metric: Optional[float],
    post_metric: Optional[float],
    metric_name: str,
    max_directive_history: int = 50,
    *,
    kg: Optional["KnowledgeGraph"] = None,
    directive_content: str = "",
    failing_tests: Optional[list[str]] = None,
    failure_detail: Optional[str] = None,
    layer: str = "maintenance",
) -> ProjectMemory:
    mem = load(memory_dir)
    now = datetime.utcnow().isoformat() + "Z"

    if not mem.created_at:
        mem.created_at = now
        mem.project_name = project_name
    mem.last_updated = now
    mem.metric_name = metric_name or mem.metric_name

    # Cycle stats
    mem.total_cycles += 1
    if status == "COMPLETED":
        mem.completed += 1
    elif status == "VETOED":
        mem.vetoed += 1
    else:
        mem.failed += 1

    # Metric tracking
    if post_metric is not None:
        if mem.metric_baseline is None and pre_metric is not None:
            mem.metric_baseline = pre_metric
        mem.metric_current = post_metric
        if mem.metric_best is None or post_metric > mem.metric_best:
            mem.metric_best = post_metric
        mem.metric_trajectory.append([directive_id, post_metric])
        if len(mem.metric_trajectory) > 20:
            mem.metric_trajectory = mem.metric_trajectory[-20:]

    # File heat map
    if status == "COMPLETED":
        for f in files_changed:
            short = _shorten_path(f)
            mem.file_touches[short] = mem.file_touches.get(short, 0) + 1
            mem.file_successes[short] = mem.file_successes.get(short, 0) + 1
    else:
        for f in files_changed:
            short = _shorten_path(f)
            mem.file_touches[short] = mem.file_touches.get(short, 0) + 1

    # Directive record
    record = {
        "id": directive_id,
        "title": directive_title[:60],
        "status": status,
        "delta": round(delta, 4) if delta is not None else None,
        "files": [_shorten_path(f) for f in files_changed[:5]],
        "ts": now[:10],
    }
    err_norm: Optional[str] = None
    if failure_detail:
        err_norm = _normalize_failure_detail(failure_detail)
        if err_norm:
            record["err"] = err_norm
    mem.directives.append(record)
    if len(mem.directives) > max_directive_history:
        mem.directives = mem.directives[-max_directive_history:]

    save(memory_dir, mem)

    if kg is not None and directive_id:
        kg.ingest_cycle_outcome(
            directive_id=directive_id,
            directive_title=directive_title,
            directive_content=directive_content,
            status=status,
            files_changed=files_changed,
            metric_name=metric_name or "",
            metric_before=pre_metric,
            metric_after=post_metric,
            failing_tests=failing_tests or [],
            failure_detail=err_norm,
            layer=layer,
        )

    return mem


def _shorten_path(p: str) -> str:
    """Abbreviate long paths for token efficiency: src/foo/bar.py → s/foo/bar.py"""
    parts = p.replace("\\", "/").split("/")
    if len(parts) > 3:
        # Keep last 2 dirs + filename
        return "…/" + "/".join(parts[-2:])
    return p


# ── Compact context (injected into Cursor Agent prompts) ───────────────────────

def compact_context(
    mem: ProjectMemory,
    n_wins: int = 3,
    n_miss: int = 2,
    *,
    cfg: Optional["HarnessConfig"] = None,
    kg: Optional["KnowledgeGraph"] = None,
    evidence: Optional["Evidence"] = None,
) -> str:
    """
    Render memory as a compact string for injection into Cursor Agent prompts.
    When `kg` is set, prefer the knowledge-graph summary (may include [GRAPH ...]).
    """
    if cfg is not None:
        n_wins = cfg.memory.compact_n_wins
        n_miss = cfg.memory.compact_n_miss
    if kg is not None:
        if cfg is not None:
            gctx = kg.build_compact_context(
                max_directives=cfg.memory.kg_max_directives,
                evidence=evidence,
                kg_use_query_context=cfg.memory.kg_use_query_context,
                kg_query_max_chars=cfg.memory.kg_query_max_chars,
                kg_query_max_directives=cfg.memory.kg_query_max_directives,
            )
        else:
            gctx = kg.build_compact_context()
        if gctx:
            extra: list[str] = []
            if cfg is not None and cfg.embedding.enabled and evidence is not None:
                try:
                    from . import embedding_retrieval

                    emb_lines = embedding_retrieval.retrieve_compact_lines(cfg, evidence)
                    if emb_lines:
                        extra.append("emb: " + " | ".join(emb_lines))
                except Exception:
                    pass
            if extra:
                return gctx + "\n" + "\n".join(extra)
            return gctx

    if mem.total_cycles == 0:
        return ""

    lines = []

    # Header line
    metric_part = ""
    if mem.metric_name and mem.metric_current is not None:
        b = f"{mem.metric_baseline:.2f}" if mem.metric_baseline is not None else "?"
        c = f"{mem.metric_current:.2f}"
        best = f" best={mem.metric_best:.2f}" if mem.metric_best is not None else ""
        metric_part = f" | {mem.metric_name} {b}→{c}{best}"
    lines.append(
        f"[MEM {mem.total_cycles}cy "
        f"{mem.completed}✓ {mem.failed}✗ {mem.vetoed}⊘{metric_part}]"
    )

    # File heat (top 4)
    if mem.file_touches:
        top = sorted(mem.file_touches.items(), key=lambda x: x[1], reverse=True)[:4]
        lines.append("hot: " + " ".join(f"{p}×{n}" for p, n in top))

    # Recent wins and misses
    completed = [d for d in mem.directives if d["status"] == "COMPLETED"]
    wins = []
    for d in reversed(completed):
        if d["delta"] is not None and d["delta"] > 0:
            wins.append(f"{d['id']}+{d['delta']:.2f}")
        elif d["delta"] is None:
            wins.append(f"{d['id']}✓")
        if len(wins) >= n_wins:
            break
    if wins:
        lines.append("wins: " + " ".join(wins))

    misses = []
    for d in reversed(mem.directives):
        if d["status"] != "COMPLETED":
            tag = {"TEST_FAILED": "test", "AGENT_FAILED": "agent",
                   "VETOED": "veto", "ERROR": "err",
                   "METRIC_REGRESSION": "metric"}.get(d["status"], "?")
            token = f"{d['id']}({tag})"
            if (
                d["status"] in ("AGENT_FAILED", "ERROR", "TEST_FAILED", "METRIC_REGRESSION")
                and d.get("err")
            ):
                hint = _failure_hint(str(d["err"]))
                if hint:
                    token = f"{d['id']}({tag}):{hint}"
            misses.append(token)
        elif d.get("delta") is not None and d["delta"] < 0:
            misses.append(f"{d['id']}{d['delta']:.2f}")
        if len(misses) >= n_miss:
            break
    if misses:
        lines.append("miss: " + " ".join(misses))

    # Learned patterns
    if mem.dead_ends:
        lines.append("dead: " + " ".join(mem.dead_ends[:4]))
    if mem.success_patterns:
        lines.append("works: " + " ".join(mem.success_patterns[:3]))

    return "\n".join(lines)


# ── Memory map (ASCII) ─────────────────────────────────────────────────────────

def render_map(mem: ProjectMemory, width: int = 72) -> str:
    """
    Render a visual memory map:
      - Directive chain with status glyphs
      - File heat map with bar
      - Metric sparkline (if available)
    """
    if mem.total_cycles == 0:
        return "No memory yet"

    lines = []
    sep = "─" * width

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append(f"MEMORY MAP ─ {mem.project_name} {'─' * max(0, width - 14 - len(mem.project_name))}")
    lines.append(f"Cycles: {mem.total_cycles} total | {mem.completed}✓ completed | "
                 f"{mem.failed}✗ failed | {mem.vetoed}⊘ vetoed")
    if mem.last_updated:
        lines.append(f"Last updated: {mem.last_updated[:19].replace('T', ' ')} UTC")
    lines.append(sep)

    # ── Directive chain ────────────────────────────────────────────────────────
    if mem.directives:
        lines.append("DIRECTIVE CHAIN")
        lines.append("")
        # Lay out in rows of 6
        row_size = 6
        for row_start in range(0, len(mem.directives), row_size):
            row = mem.directives[row_start:row_start + row_size]
            cells = []
            for d in row:
                glyph = {
                    "COMPLETED": "✓",
                    "TEST_FAILED": "✗",
                    "METRIC_REGRESSION": "↓",
                    "AGENT_FAILED": "✗",
                    "VETOED": "⊘",
                    "ERROR": "!",
                    "INSUFFICIENT_EVIDENCE": "…",
                }.get(d["status"], "?")
                delta = ""
                if d.get("delta") is not None:
                    delta = f"{d['delta']:+.2f}"
                cells.append(f"{d['id']} {glyph}{delta}")

            lines.append("  →  ".join(cells))

        lines.append("")

        # Show last 3 directives with title
        lines.append("Recent:")
        for d in mem.directives[-3:]:
            glyph = "✓" if d["status"] == "COMPLETED" else "✗"
            delta_str = f" ({d['delta']:+.3f})" if d.get("delta") is not None else ""
            files_str = ", ".join(d.get("files", [])[:2])
            lines.append(f"  {glyph} {d['id']} — {d['title'][:45]}{delta_str}")
            if files_str:
                lines.append(f"      files: {files_str}")
        lines.append(sep)

    # ── File heat map ──────────────────────────────────────────────────────────
    if mem.file_touches:
        lines.append("FILE HEAT MAP")
        lines.append("")
        max_touches = max(mem.file_touches.values())
        bar_width = 12
        sorted_files = sorted(mem.file_touches.items(), key=lambda x: x[1], reverse=True)[:12]
        for path, count in sorted_files:
            bar_len = max(1, round(count / max_touches * bar_width))
            bar = "█" * bar_len + "░" * (bar_width - bar_len)
            success = mem.file_successes.get(path, 0)
            pct = round(success / count * 100) if count else 0
            lines.append(f"  {bar}  {path:<30} ×{count} ({pct}%✓)")
        lines.append(sep)

    # ── Metric sparkline ───────────────────────────────────────────────────────
    if mem.metric_trajectory and mem.metric_name:
        lines.append(f"METRIC: {mem.metric_name.upper()}")
        lines.append("")
        values = [v for _, v in mem.metric_trajectory]
        _render_sparkline(lines, values, mem.metric_trajectory, width)
        if mem.metric_baseline is not None:
            total_gain = (mem.metric_current or 0) - mem.metric_baseline
            lines.append(
                f"  baseline={mem.metric_baseline:.3f}  "
                f"current={mem.metric_current:.3f}  "
                f"best={mem.metric_best:.3f}  "
                f"total gain={total_gain:+.3f}"
            )
        lines.append(sep)

    # ── Learned patterns ───────────────────────────────────────────────────────
    has_patterns = mem.success_patterns or mem.failure_patterns or mem.dead_ends
    if has_patterns:
        lines.append("LEARNED PATTERNS")
        lines.append("")
        if mem.success_patterns:
            lines.append("  Works:")
            for p in mem.success_patterns:
                lines.append(f"    + {p}")
        if mem.failure_patterns:
            lines.append("  Fails:")
            for p in mem.failure_patterns:
                lines.append(f"    - {p}")
        if mem.dead_ends:
            lines.append("  Dead ends:")
            for p in mem.dead_ends:
                lines.append(f"    ✗ {p}")
        lines.append(sep)

    return "\n".join(lines)


def _render_sparkline(lines: list, values: list[float], trajectory: list, width: int) -> None:
    """Render a simple ASCII sparkline."""
    if len(values) < 2:
        lines.append(f"  (only {len(values)} data point(s))")
        return

    chart_w = min(len(values), width - 10)
    chart_h = 5
    v_min = min(values)
    v_max = max(values)
    v_range = v_max - v_min or 1

    # Sample values to fit chart width
    sampled = values[-chart_w:] if len(values) > chart_w else values
    ids = [t[0] for t in trajectory[-len(sampled):]]

    grid = [[" "] * len(sampled) for _ in range(chart_h)]

    prev_row = None
    for col, v in enumerate(sampled):
        row = chart_h - 1 - round((v - v_min) / v_range * (chart_h - 1))
        row = max(0, min(chart_h - 1, row))
        grid[row][col] = "●"
        if prev_row is not None and prev_row != row:
            lo, hi = min(prev_row, row), max(prev_row, row)
            for r in range(lo + 1, hi):
                grid[r][col - 1] = "│"
        prev_row = row

    for r, row in enumerate(grid):
        v_label = v_min + (chart_h - 1 - r) / (chart_h - 1) * v_range
        lines.append(f"  {v_label:.3f} │{''.join(row)}")

    # X-axis labels (show first and last directive id)
    x_line = " " * 9 + "└" + "─" * len(sampled)
    lines.append(x_line)
    if ids:
        label_line = " " * 10 + ids[0]
        if len(ids) > 1:
            padding = len(sampled) - len(ids[0]) - len(ids[-1])
            label_line += " " * max(0, padding) + ids[-1]
        lines.append(label_line)


# ── Pattern learning (called after N cycles) ───────────────────────────────────

def infer_patterns(mem: ProjectMemory) -> tuple[list[str], list[str], list[str]]:
    """
    Simple heuristic pattern inference from directive history.
    Returns (success_patterns, failure_patterns, dead_ends).
    No LLM call — pure data analysis, zero tokens.
    """
    if len(mem.directives) < 3:
        return [], [], []

    # Which file types correlate with success?
    ext_success: dict[str, list[bool]] = {}
    for d in mem.directives:
        won = d["status"] == "COMPLETED"
        for f in d.get("files", []):
            ext = f.rsplit(".", 1)[-1] if "." in f else "?"
            ext_success.setdefault(ext, []).append(won)

    success_pats = []
    failure_pats = []
    for ext, outcomes in ext_success.items():
        if len(outcomes) >= 3:
            rate = sum(outcomes) / len(outcomes)
            if rate >= 0.75:
                success_pats.append(f".{ext}-changes→gains")
            elif rate <= 0.25:
                failure_pats.append(f".{ext}-changes→fails")

    # Dead ends: directives that failed multiple times in same area
    title_failures: dict[str, int] = {}
    for d in mem.directives:
        if d["status"] in ("TEST_FAILED", "METRIC_REGRESSION", "AGENT_FAILED", "ERROR"):
            # Use first 3 words of title as fingerprint
            key = " ".join(d["title"].split()[:3]).lower()
            title_failures[key] = title_failures.get(key, 0) + 1

    dead_ends = [k for k, v in title_failures.items() if v >= 2][:5]

    return success_pats[:4], failure_pats[:4], dead_ends


def refresh_patterns(memory_dir: Path, kg: Optional[Any] = None) -> None:
    """Recompute and save patterns. Call after every N cycles."""
    mem = load(memory_dir)
    sp, fp, de = infer_patterns(mem)
    if sp or fp or de:
        mem.success_patterns = sp
        mem.failure_patterns = fp
        mem.dead_ends = de
        save(memory_dir, mem)

    if kg is not None:
        mem2 = load(memory_dir)
        for path_key, touches in mem2.file_touches.items():
            if touches <= 0:
                continue
            successes = mem2.file_successes.get(path_key, 0)
            rate = successes / touches
            fid = f"file:{path_key}"
            kg.upsert_node(
                fid,
                "file",
                name=path_key,
                data={"success_rate": rate, "touches": touches},
            )
