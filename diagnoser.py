"""
meta_harness/diagnoser.py
Feeds collected evidence to Cursor Agent (Composer) and produces a structured Diagnosis.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from rich.console import Console

from . import cursor_client
from .config import HarnessConfig
from .evidence import Evidence
from . import memory as mem_module
from .knowledge_graph import build_cross_layer_context

console = Console()

if TYPE_CHECKING:
    from .knowledge_graph import KnowledgeGraph


@dataclass
class Diagnosis:
    summary: str = ""
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)    # Recurring behaviors
    opportunities: list[str] = field(default_factory=list)  # Specific improvement targets
    risk_areas: list[str] = field(default_factory=list)  # Areas to be careful with
    raw: str = ""  # Full model response


_SYSTEM = '''\
You are the Diagnoser component of a Meta-Harness — an autonomous outer loop that \
analyzes a software project's behavior and proposes targeted improvements.

Your job: analyze the evidence provided and produce a structured diagnosis of the project's \
current state, identifying what's working well, what's failing, recurring patterns, \
and specific improvement opportunities.

Be concrete. Reference actual log lines, metric values, or file names when relevant.
Avoid vague generalities — every point should be actionable.

Output your response as a single JSON code block like this:

```json
{
  "summary": "2-3 sentence overview of current project health",
  "strengths": ["..."],
  "weaknesses": ["..."],
  "patterns": ["recurring behaviors or error patterns observed"],
  "opportunities": ["specific, targeted things that could be improved"],
  "risk_areas": ["areas that should be touched carefully or not at all right now"]
}
```

No other text before or after the code block.
'''


def _build_prompt(cfg: HarnessConfig, ev: Evidence, kg: Optional["KnowledgeGraph"]) -> str:
    parts = [
        f"# Project: {cfg.project.name}",
        f"## Description\n{cfg.project.description.strip()}",
        f"## Goals\n" + "\n".join(f"- {g}" for g in cfg.goals.objectives),
        "",
    ]

    if cfg.memory.enabled:
        mem = mem_module.load(cfg.memory_dir)
        ctx = mem_module.compact_context(mem, kg=kg)
        if ctx:
            parts.append(f"## Harness Memory\n```\n{ctx}\n```\n")

    if kg is not None:
        xctx = build_cross_layer_context(kg, "maintenance")
        if xctx.strip():
            parts.append(f"## Product / parallel track\n{xctx}\n")

    parts += [
        "## Evidence Collected",
        f"Collected at: {ev.collected_at}",
    ]

    if ev.metrics.current:
        parts.append(
            "\n### Metrics\n```json\n" + json.dumps(ev.metrics.current, indent=2) + "\n```"
        )

    if ev.test_results.strip():
        parts.append(f"\n### Last Test Results\n```\n{ev.test_results.strip()[:3000]}\n```")

    if ev.log_tail.strip():
        parts.append(f"\n### Runtime Logs\n```\n{ev.log_tail.strip()}\n```")

    if ev.cursor_cli_failure_excerpt.strip():
        parts.append(
            "\n### Last Cursor / Agent CLI failure (most recent)\n```\n"
            + ev.cursor_cli_failure_excerpt.strip()
            + "\n```"
        )

    if ev.git_diff.strip():
        parts.append(f"\n### Recent Changes\n```\n{ev.git_diff.strip()}\n```")

    if ev.cycle_history.strip():
        parts.append(f"\n### Previous Cycle History\n```\n{ev.cycle_history.strip()}\n```")

    if ev.file_tree.strip():
        parts.append(f"\n### Project File Tree\n```\n{ev.file_tree.strip()}\n```")

    parts.append(
        "\nPrimary metric: "
        + (cfg.goals.primary_metric or "none specified")
        + f" ({cfg.goals.optimization_direction})"
    )

    return "\n".join(parts)


_EXPECTED_KEYS = frozenset(
    {"summary", "strengths", "weaknesses", "patterns", "opportunities", "risk_areas"}
)

# Used by json_call parse-failure retries (see cursor_client.parse_retry_user_suffix).
_DIAGNOSIS_JSON_RETRY_SUFFIX = (
    "\n\n---\n"
    "Your previous reply did not include parseable JSON for this diagnosis. "
    "Reply with exactly one markdown fenced code block labeled `json` containing only a JSON object "
    "with keys `summary`, `strengths`, `weaknesses`, `patterns`, `opportunities`, and `risk_areas` "
    "(as in the system instructions). No other text before or after that block.\n"
)


def _str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, list):
        return [str(x) for x in val]
    return [str(val)]


def _normalize_diagnosis_keys(data: dict) -> dict:
    """Map Title_Case or other variants to snake_case fields Diagnosis expects."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        key = str(k).strip().lower().replace(" ", "_")
        if key == "riskareas":
            key = "risk_areas"
        out[key] = v
    return out


def _coerce_diagnosis_payload(payload: Any) -> Optional[dict]:
    if isinstance(payload, dict):
        return _normalize_diagnosis_keys(payload)
    if isinstance(payload, list) and payload:
        if isinstance(payload[0], dict):
            return _normalize_diagnosis_keys(payload[0])
    if isinstance(payload, str) and payload.strip():
        inner = cursor_client.extract_json(payload)
        return _coerce_diagnosis_payload(inner)
    return None


def _diagnosis_from_dict(data: dict, raw: str) -> Diagnosis:
    data = _normalize_diagnosis_keys(data)
    return Diagnosis(
        summary=str(data.get("summary", "") or ""),
        strengths=_str_list(data.get("strengths")),
        weaknesses=_str_list(data.get("weaknesses")),
        patterns=_str_list(data.get("patterns")),
        opportunities=_str_list(data.get("opportunities")),
        risk_areas=_str_list(data.get("risk_areas")),
        raw=raw,
    )


def _looks_like_diagnosis_dict(d: dict) -> bool:
    d = _normalize_diagnosis_keys(d)
    return bool(set(d.keys()) & _EXPECTED_KEYS)


# Token-bounded excerpt of prior stdout for the diagnose repair pass (see `_repair_suffix`).
_REPAIR_PREV_MAX = 3500
_REPAIR_HEAD = 2000
_REPAIR_TAIL = 1500


def _repair_suffix(previous_raw: str, resp_error: str) -> str:
    prev = previous_raw or ""
    if len(prev) > _REPAIR_PREV_MAX:
        excerpt = (
            prev[:_REPAIR_HEAD]
            + "\n\n... [truncated middle; total length "
            + str(len(prev))
            + " chars] ...\n\n"
            + prev[-_REPAIR_TAIL:]
        )
    else:
        excerpt = prev
    err = (resp_error or "").strip()
    err_block = f"\n\nParse / harness error (if any): {err}\n" if err else ""
    return (
        "\n\n---\n## Repair request\n"
        "The previous reply could not be parsed as the required JSON diagnosis."
        + err_block
        + "\nBelow is a bounded excerpt of the previous stdout:\n```\n"
        + excerpt
        + "\n```\n\n"
        "Reply with only one markdown fenced code block using the json language tag; "
        "inside that block, put only a JSON object with keys "
        "`summary`, `strengths`, `weaknesses`, `patterns`, `opportunities`, `risk_areas` "
        "(same schema as the system instructions). No other text before or after the block.\n"
    )


def _diagnosis_from_response(resp: cursor_client.CursorResponse) -> Optional[Diagnosis]:
    """If ``resp`` yields a structured diagnosis (data or fenced JSON in raw), return it."""
    raw = resp.raw or ""

    if resp.success:
        data_dict = _coerce_diagnosis_payload(resp.data)
        if data_dict is not None and _looks_like_diagnosis_dict(data_dict):
            return _diagnosis_from_dict(data_dict, raw)

    parsed_from_raw = cursor_client.extract_json(raw) if raw else None
    data_dict = _coerce_diagnosis_payload(parsed_from_raw)
    if data_dict is not None and _looks_like_diagnosis_dict(data_dict):
        return _diagnosis_from_dict(data_dict, raw)

    return None


def run(cfg: HarnessConfig, ev: Evidence, kg: Optional["KnowledgeGraph"] = None) -> Diagnosis:
    prompt = _build_prompt(cfg, ev, kg)
    j_timeout = cfg.cursor.json_timeout
    if j_timeout is None:
        j_timeout = cfg.cursor.timeout_seconds
    resp = cursor_client.json_call(
        cfg,
        _SYSTEM,
        prompt,
        label="diagnose",
        timeout_seconds=j_timeout,
        max_retries=cfg.cursor.json_retries,
        parse_retry_user_suffix=_DIAGNOSIS_JSON_RETRY_SUFFIX,
    )
    raw = resp.raw or ""

    if os.environ.get("META_HARNESS_DEBUG"):
        dk = ""
        if isinstance(resp.data, dict):
            dk = f" keys={sorted(resp.data.keys())}"
        elif resp.data is not None:
            dk = f" type={type(resp.data).__name__}"
        console.print(
            f"[dim]Diagnoser resp.success={resp.success} data={resp.data is not None}{dk}[/dim]"
        )

    found = _diagnosis_from_response(resp)
    if found is not None:
        return found

    # One repair attempt: same system prompt, user prompt with bounded prior output + strict JSON-only instruction.
    # max_retries=0 avoids stacking with the initial json_call retry budget.
    repair_user = prompt + _repair_suffix(raw, resp.error or "")
    resp_repair = cursor_client.json_call(
        cfg,
        _SYSTEM,
        repair_user,
        label="diagnose_repair",
        timeout_seconds=j_timeout,
        max_retries=0,
    )
    found_repair = _diagnosis_from_response(resp_repair)
    if found_repair is not None:
        return found_repair

    return Diagnosis(summary=(raw or resp.error or "")[:500], raw=raw or resp.error)
