"""
meta_harness/directive_confidence.py

Deterministic pre-run success estimates for proposed directives (no Cursor CLI calls).
Combines harness memory, KG similarity signals, directive text heuristics, and optional
diagnosis text — aligned with research.py naming (confidence + human-readable factors).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .config import HarnessConfig
from . import memory as mem_module
from .proposer import Directive

if TYPE_CHECKING:
    from .knowledge_graph import KnowledgeGraph

# ── Tier bands (inclusive lower bound for `high` / `medium`; else `low`) ───────
# Documented thresholds — tune via these constants until a config story exists.
TIER_HIGH_MIN = 0.67
TIER_MEDIUM_MIN = 0.34

# Weight budget (see score_directive): memory ~40%, KG ~35%, text ~15%, diagnosis ~10%.
# Each component contributes a delta in [-1, 1] before blending into [0, 1].


@dataclass(frozen=True)
class DirectiveConfidenceResult:
    score: float  # [0.0, 1.0]
    tier: str  # high | medium | low
    factors: list[str]


def tier_for_score(score: float) -> str:
    s = max(0.0, min(1.0, score))
    if s >= TIER_HIGH_MIN:
        return "high"
    if s >= TIER_MEDIUM_MIN:
        return "medium"
    return "low"


_PATH_LIKE = re.compile(
    r"(?:^|[\s`'\"])([A-Za-z0-9_.][A-Za-z0-9_./\\-]*\.(?:py|md|toml|tsx?|jsx?|rs|go|json))\b"
)
_BROAD_HINTS = re.compile(
    r"\b(refactor|rearchitecture|rewrite|new module|large.?scale|broad|major rewrite)\b",
    re.IGNORECASE,
)
_NARROW_HINTS = re.compile(
    r"\b(fix|bug|typo|small|narrow|guard|test only|add test|patch)\b",
    re.IGNORECASE,
)
_DIAG_RISK = re.compile(
    r"\b(risk|fragile|uncertain|blocker|failed|broken)\b",
    re.IGNORECASE,
)
_DIAG_CLEAR = re.compile(
    r"\b(clear|straightforward|small|localized|simple fix)\b",
    re.IGNORECASE,
)


def _significant_tokens(text: str, *, max_tokens: int = 8) -> list[str]:
    stop = {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "directive",
        "implementation",
        "into",
        "your",
        "are",
        "not",
    }
    out: list[str] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_-]{2,}", text):
        t = m.group(0).lower()
        if t in stop:
            continue
        if t not in out:
            out.append(t)
        if len(out) >= max_tokens:
            break
    return out


def _memory_signals(
    cfg: HarnessConfig,
    directive_body: str,
) -> tuple[float, list[str]]:
    """
    Returns a partial score in [0, 1] (neutral 0.5 when no data) and factor strings.
    """
    mem = mem_module.load(cfg.memory_dir)
    factors: list[str] = []

    if mem.total_cycles <= 0:
        factors.append("No prior cycles in memory yet (neutral baseline)")
        return 0.5, factors

    base_rate = mem.completed / mem.total_cycles if mem.total_cycles else 0.0
    factors.append(
        f"Memory: {mem.completed}/{mem.total_cycles} cycles completed ({base_rate:.0%} win rate)"
    )

    recent = mem.directives[-5:] if mem.directives else []
    recent_rate = 0.5
    if recent:
        rc = sum(1 for d in recent if d.get("status") == "COMPLETED")
        recent_rate = rc / len(recent)
        factors.append(f"Recent directives ({len(recent)}): {rc} completed")

    path_hits: list[float] = []
    for m in _PATH_LIKE.finditer(directive_body):
        raw = m.group(1).strip()
        short = mem_module._shorten_path(raw.replace("\\", "/"))
        touches = mem.file_touches.get(short, 0)
        if touches <= 0:
            continue
        succ = mem.file_successes.get(short, 0)
        path_hits.append(succ / touches)
    if path_hits:
        avg = sum(path_hits) / len(path_hits)
        factors.append(
            f"Paths mentioned: ~{avg:.0%} historical success on {len(path_hits)} file(s)"
        )
    else:
        factors.append("No overlapping file-heat paths extracted from directive text")

    # Blend: base rate primary, recent secondary, path tertiary.
    partial = 0.55 * base_rate + 0.30 * recent_rate + 0.15 * (
        sum(path_hits) / len(path_hits) if path_hits else 0.5
    )
    partial = max(0.0, min(1.0, partial))
    return partial, factors


def _kg_signals(
    kg: Optional["KnowledgeGraph"],
    title: str,
    body: str,
) -> tuple[float, list[str]]:
    """
    Returns partial score in [0, 1] (0.5 neutral) from FTS overlap with past directive nodes.
    """
    factors: list[str] = []
    if kg is None:
        factors.append("Knowledge graph unavailable (neutral)")
        return 0.5, factors

    blob = f"{title}\n{body[:1200]}"
    tokens = _significant_tokens(blob, max_tokens=6)
    if not tokens:
        factors.append("KG: no tokens to match past directives (neutral)")
        return 0.5, factors

    seen: set[str] = set()
    for tok in tokens[:4]:
        try:
            for nid in kg.search(tok, limit=12):
                seen.add(nid)
        except Exception:
            continue

    completed = 0
    failed = 0
    for nid in seen:
        node = kg.get_node(nid)
        if not node or node.get("type") != "directive":
            continue
        st = (node.get("status") or "").upper()
        if st == "COMPLETED":
            completed += 1
        elif st in ("TEST_FAILED", "AGENT_FAILED", "ERROR", "METRIC_REGRESSION"):
            failed += 1

    total = completed + failed
    if total == 0:
        factors.append(
            f"KG: {len(seen)} search hit(s), no comparable directive outcomes yet"
        )
        return 0.5, factors

    ratio = completed / total
    factors.append(
        f"KG: similar directives — {completed} completed vs {failed} failed (n={total})"
    )
    return max(0.0, min(1.0, ratio)), factors


def _text_signals(body: str) -> tuple[float, list[str]]:
    """
    Partial score [0,1]: narrow bugfix language lifts; broad refactor language lowers.
    Capped so keywords cannot dominate KG/memory.
    """
    b = len(_BROAD_HINTS.findall(body))
    n = len(_NARROW_HINTS.findall(body))
    # Map to [0,1] with neutral 0.5 when no cues.
    raw = 0.5 + min(0.12, 0.04 * n) - min(0.12, 0.04 * b)
    raw = max(0.0, min(1.0, raw))
    factors: list[str] = []
    if b:
        factors.append(f"Directive text: broad-scope cue(s) ({b}) — slight caution")
    if n:
        factors.append(f"Directive text: narrow-fix cue(s) ({n}) — slight positive")
    if not factors:
        factors.append("Directive text: no strong breadth/narrow cues")
    return raw, factors


def _diagnosis_signals(summary: str | None) -> tuple[float, list[str]]:
    if not (summary and summary.strip()):
        return 0.5, []
    s = summary
    nr = len(_DIAG_RISK.findall(s))
    nc = len(_DIAG_CLEAR.findall(s))
    raw = 0.5 - min(0.1, 0.025 * nr) + min(0.1, 0.025 * nc)
    raw = max(0.0, min(1.0, raw))
    factors: list[str] = []
    if nr:
        factors.append(f"Diagnosis: risk/uncertainty cues ({nr})")
    if nc:
        factors.append(f"Diagnosis: clarity/straightforward cues ({nc})")
    return raw, factors


def _merge_factors(
    mem_f: list[str],
    kg_f: list[str],
    text_f: list[str],
    dx_f: list[str],
) -> list[str]:
    combined = [*mem_f, *kg_f, *text_f, *dx_f]
    out: list[str] = []
    for line in combined:
        line = line.strip()
        if line and line not in out:
            out.append(line)
        if len(out) >= 6:
            break
    return out[:6] if out else ["No scoring factors (neutral)"]


def score_directive(
    cfg: HarnessConfig,
    directive: Directive,
    kg: Optional["KnowledgeGraph"] = None,
    diagnosis_summary: str | None = None,
) -> DirectiveConfidenceResult:
    """
    Deterministic confidence in [0,1] with tier + short factor bullets.
    Does not invoke the Cursor CLI.
    """
    body = directive.content or ""
    mem_part, mem_f = _memory_signals(cfg, body)
    kg_part, kg_f = _kg_signals(kg, directive.title, body)
    text_part, text_f = _text_signals(body)
    dx_part, dx_f = _diagnosis_signals(diagnosis_summary)

    # Weighted blend into [0, 1].
    score = (
        0.40 * mem_part
        + 0.35 * kg_part
        + 0.15 * text_part
        + 0.10 * dx_part
    )
    score = max(0.0, min(1.0, score))

    tier = tier_for_score(score)
    factors = _merge_factors(mem_f, kg_f, text_f, dx_f)
    return DirectiveConfidenceResult(score=score, tier=tier, factors=factors)


def format_confidence_rich_line(result: DirectiveConfidenceResult) -> str:
    """Single-line summary for Rich console (percentage + tier + truncated factors)."""
    pct = int(round(result.score * 100))
    fac = "; ".join(result.factors[:3])
    return f"Predicted success: {pct}% ({result.tier}) — {fac}"


def outcome_detail_string(result: DirectiveConfidenceResult) -> str:
    """Persisted alongside cycle JSON (human-readable)."""
    pct = int(round(result.score * 100))
    return f"{pct}% ({result.tier}) — " + "; ".join(result.factors[:5])
