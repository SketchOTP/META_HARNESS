"""
meta_harness/proposer.py
Takes a Diagnosis and generates a Directive — a markdown spec for the agent to implement.
Uses Cursor Agent (Composer) in ask mode.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from . import cursor_client
from .config import HarnessConfig
from .diagnoser import Diagnosis
from . import memory as mem_module

if TYPE_CHECKING:
    from .knowledge_graph import KnowledgeGraph


@dataclass
class Directive:
    id: str          # e.g. D016_auto
    path: Path       # Where the .md file was saved
    title: str
    content: str


_SYSTEM = """\
You are the Proposer component of a Meta-Harness — an autonomous outer loop that \
improves a software project by writing precise directives for a coding agent.

Given a diagnosis of the project's current state, write ONE focused, actionable directive.

A directive is a markdown document that:
1. Names a specific, bounded improvement (not a broad refactor)
2. Explains WHY this improvement matters (grounded in the diagnosis evidence)
3. Specifies WHAT files to change and exactly HOW
4. Lists acceptance criteria (what the tests/behavior should look like after)
5. Lists constraints (what NOT to touch, what must stay stable)

Rules:
- One directive = one coherent change. Do not bundle unrelated changes.
- Be specific about file paths, function names, config keys.
- If modifying a prompt or config, show the before/after explicitly.
- The agent implementing this directive will have access to the full file contents.
- Prefer the smallest change that meaningfully improves the target metric or goal.
- Directives that modify tests must also modify the code they test.

Start your response with a title line: `# DIRECTIVE: <short title>`
Then write the full directive in markdown.
"""


def _build_prompt(cfg: HarnessConfig, diagnosis: Diagnosis, kg: Optional["KnowledgeGraph"]) -> str:
    weaknesses = "\n".join(f"- {w}" for w in diagnosis.weaknesses)
    opportunities = "\n".join(f"- {o}" for o in diagnosis.opportunities)
    risks = "\n".join(f"- {r}" for r in diagnosis.risk_areas)
    patterns = "\n".join(f"- {p}" for p in diagnosis.patterns)

    in_scope = "\n".join(f"  {pat}" for pat in cfg.scope.modifiable)
    protected = "\n".join(f"  {pat}" for pat in cfg.scope.protected)
    objectives = "\n".join(f"- {g}" for g in cfg.goals.objectives)

    return f"""\
# Project: {cfg.project.name}
## Description
{cfg.project.description.strip()}

## Goals
{objectives}

## Primary Metric
{cfg.goals.primary_metric or 'none'} ({cfg.goals.optimization_direction})

## Harness Memory
```
{mem_module.compact_context(mem_module.load(cfg.memory_dir), kg=kg) if cfg.memory.enabled else '(disabled)'}
```

---

## Diagnosis Summary
{diagnosis.summary}

### Strengths (don't break these)
{chr(10).join(f"- {s}" for s in diagnosis.strengths) or "none identified"}

### Weaknesses
{weaknesses or "none identified"}

### Patterns Observed
{patterns or "none identified"}

### Improvement Opportunities
{opportunities or "none identified"}

### Risk Areas (be conservative here)
{risks or "none identified"}

---

## Scope Constraints
### Agent may modify files matching:
{in_scope}

### Protected (never touch):
{protected}

---

Write the directive now. Pick the HIGHEST IMPACT opportunity from the list above.
"""


def _normalize_agent_markdown_body(text: str) -> str:
    """Strip a single leading ```lang ... ``` wrapper if the model fenced the whole directive."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl == -1:
        return t
    inner = t[first_nl + 1 :]
    end = inner.rfind("```")
    if end == -1:
        return t
    return inner[:end].strip()


def _extract_directive_title(content: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        u = s.upper()
        if u.startswith("# DIRECTIVE:"):
            return s.split(":", 1)[1].strip()
    return "Untitled Directive"


def _directive_titles_similar(a: str, b: str, threshold: float = 0.55) -> bool:
    a_n = " ".join(a.lower().split())
    b_n = " ".join(b.lower().split())
    if not a_n or not b_n:
        return False
    if a_n in b_n or b_n in a_n:
        return True
    return difflib.SequenceMatcher(None, a_n, b_n).ratio() >= threshold


def _kg_notes_for_similar_attempts(
    kg: Optional["KnowledgeGraph"], proposed_title: str
) -> str:
    """Build user-prompt notes when KG shows a similar directive already COMPLETED or AGENT_FAILED."""
    if kg is None or not (proposed_title or "").strip():
        return ""
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    try:
        hits = kg.search(proposed_title.strip(), limit=24)
    except Exception:
        return ""
    for nid in hits:
        node = kg.get_node(nid)
        if not node or node.get("type") != "directive":
            continue
        st = (node.get("status") or "").strip()
        if st not in ("COMPLETED", "AGENT_FAILED"):
            continue
        name = (node.get("name") or "").strip()
        if not _directive_titles_similar(proposed_title, name):
            continue
        key = (nid, st)
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"NOTE: Similar directive was already attempted: {nid} — {name} — {st}. "
            "Propose something different."
        )
    return "\n".join(lines)


def _next_directive_id(directives_dir: Path) -> str:
    nums: list[int] = []
    for p in directives_dir.glob("*.md"):
        m = re.match(r"^[DM](\d+)", p.stem, re.IGNORECASE)
        if m:
            nums.append(int(m.group(1)))
    next_num = (max(nums) + 1) if nums else 1
    return f"M{next_num:03d}_auto"


def run(cfg: HarnessConfig, diagnosis: Diagnosis, kg: Optional["KnowledgeGraph"] = None) -> Directive:
    prompt = _build_prompt(cfg, diagnosis, kg)
    resp = cursor_client.agent_call(cfg, _SYSTEM, prompt, label="propose")
    if not resp.success:
        raise RuntimeError(resp.error or "Proposer agent_call failed")
    content = _normalize_agent_markdown_body(resp.raw.strip() if resp.raw else "")
    if not content or len(content) < 50:
        raise ValueError(
            f"Proposer returned empty content. Raw: {repr((resp.raw or '')[:200])}"
        )

    title = _extract_directive_title(content)
    kg_notes = _kg_notes_for_similar_attempts(kg, title)
    if kg_notes:
        retry_prompt = (
            prompt
            + "\n\n---\n# Knowledge graph — prior attempts\n"
            + kg_notes
            + "\n"
        )
        r2 = cursor_client.agent_call(cfg, _SYSTEM, retry_prompt, label="propose_retry")
        if r2.success and r2.raw:
            c2 = _normalize_agent_markdown_body(r2.raw.strip())
            if len(c2) >= 50:
                content = c2
                title = _extract_directive_title(content)

    directive_id = _next_directive_id(cfg.directives_dir)
    directive_path = cfg.directives_dir / f"{directive_id}.md"

    header = f"""\
---
id: {directive_id}
generated: {datetime.utcnow().isoformat()}Z
project: {cfg.project.name}
status: pending
---

"""
    full_content = header + content
    directive_path.write_text(full_content, encoding="utf-8")

    return Directive(
        id=directive_id,
        path=directive_path,
        title=title,
        content=full_content,
    )
