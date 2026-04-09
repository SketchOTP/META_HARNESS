"""
meta_harness/product_agent.py

Product agent: vision-grounded diagnosis and feature directives (P_ prefix).
Runs independently from the maintenance cycle; shares the knowledge graph only.
Uses the same Cursor subprocess and test/restart paths as the maintenance cycle
(:mod:`cursor_client`, :mod:`cycle`, :mod:`platform_runtime`).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from rich.console import Console
from rich.panel import Panel

from . import agent, cursor_client, evidence, rollback
from . import memory as mem_module
from .config import HarnessConfig
from .directive_confidence import format_confidence_rich_line, outcome_detail_string, score_directive
from .cycle import (
    CycleOutcome,
    CycleStatus,
    _acquire_agent_lock,
    _read_primary_metric,
    _release_agent_lock,
    _restart_project,
    _run_tests,
)
from . import vision as vision_mod
from .knowledge_graph import build_cross_layer_context
from .proposer import Directive, _extract_directive_title, _normalize_agent_markdown_body

# Model often emits "# DIRECTIVE: D1" or YAML id: — treat as invalid title, not the harness id.
_BARE_DIRECTIVE_ID_TITLE = re.compile(r"^[DMP]\d{1,4}(_auto)?$", re.IGNORECASE)

if TYPE_CHECKING:
    from .knowledge_graph import KnowledgeGraph

console = Console()

_PRODUCT_SYSTEM = """\
You are the Product Agent for a software project.
Your job: analyze what exists and what's missing relative to the product vision.

You have access to:
1. The product vision (what this project is trying to become)
2. The current codebase (what exists today)
3. Recent maintenance work (what the maintenance agent has been fixing)
4. User-facing gaps (what users would want that doesn't exist yet)

Think like a product manager, not an engineer.
Ask: "What would make this more useful? What's missing? What should exist next?"

Respond ONLY with a JSON code block:
```json
{
  "summary": "...",
  "existing_features": ["what already works"],
  "missing_features": ["things users would want that don't exist"],
  "user_value_gaps": ["functionality gaps that reduce user value"],
  "next_build_targets": ["highest priority things to build next, most impactful first"],
  "maintenance_blockers": ["maintenance issues blocking product progress"]
}
```
"""

_PROPOSE_SYSTEM = """\
You are the Proposer for the Product Agent of a Meta-Harness.

You are building toward a vision, not fixing bugs. The maintenance agent handles bugs and stability — you handle growth.

Given the product diagnosis, write ONE implementation directive for the coding agent:
- Pick ONE feature from `next_build_targets` (highest impact first).
- Write a complete implementation directive: new files, new modules, new APIs are allowed when they serve the vision.
- Be creative and concrete: file paths, acceptance criteria, constraints.
- Do not duplicate items marked as already shipped or out of scope.

Start your response with a title line: `# DIRECTIVE: <short human-readable title>`
(use a real feature name — never put a directive id like D1, M003, or P001 in this line; the harness assigns `P_NNN_auto` when saving).

Then write the full directive in markdown. Do not include a YAML `id:` block; the file header is added by the harness.
"""

_PRODUCT_JSON_RETRY = (
    "\n\n---\n"
    "Your previous reply did not include parseable JSON for this product diagnosis. "
    "Reply with exactly one markdown fenced code block labeled `json` containing only a JSON object "
    "with keys `summary`, `existing_features`, `missing_features`, `user_value_gaps`, "
    "`next_build_targets`, and `maintenance_blockers` (arrays of strings where applicable). "
    "No other text before or after that block.\n"
)


@dataclass
class ProductDiagnosis:
    summary: str = ""
    existing_features: list[str] = field(default_factory=list)
    missing_features: list[str] = field(default_factory=list)
    user_value_gaps: list[str] = field(default_factory=list)
    next_build_targets: list[str] = field(default_factory=list)
    maintenance_blockers: list[str] = field(default_factory=list)
    raw: str = ""


def _str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, list):
        return [str(x) for x in val]
    return [str(val)]


def _product_from_dict(data: dict[str, Any], raw: str) -> ProductDiagnosis:
    return ProductDiagnosis(
        summary=str(data.get("summary", "") or ""),
        existing_features=_str_list(data.get("existing_features")),
        missing_features=_str_list(data.get("missing_features")),
        user_value_gaps=_str_list(data.get("user_value_gaps")),
        next_build_targets=_str_list(data.get("next_build_targets")),
        maintenance_blockers=_str_list(data.get("maintenance_blockers")),
        raw=raw,
    )


def _product_from_response(
    resp: cursor_client.CursorResponse,
) -> Optional[ProductDiagnosis]:
    raw = resp.raw or ""
    payload: Any = resp.data
    if not resp.success and payload is None and raw:
        payload = cursor_client.extract_json(raw)
    if isinstance(payload, dict):
        return _product_from_dict(payload, raw)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return _product_from_dict(payload[0], raw)
    if raw:
        inner = cursor_client.extract_json(raw)
        if isinstance(inner, dict):
            return _product_from_dict(inner, raw)
    return None


def _research_queue_block(cfg: HarnessConfig) -> str:
    from . import research as research_mod

    queue = research_mod.get_queue(cfg)
    if not queue:
        return ""
    items = "\n".join(
        f"  - {(q.get('title') or '')[:60]} → {q.get('applicable_to', '')} ({q.get('difficulty', '')}): {q.get('expected_impact', '')}"
        for q in queue[:5]
    )
    return f"\n## Approved Research Papers (implement these)\n{items}\n"


def _vision_prompt_block(cfg: HarnessConfig) -> str:
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


def _build_diagnose_user_prompt(cfg: HarnessConfig, ev: evidence.Evidence, kg: Optional["KnowledgeGraph"]) -> str:
    vision_block = (
        vision_mod.vision_prompt_block(kg, cfg)
        if kg is not None
        else _vision_prompt_block(cfg)
    )
    parts = [
        f"# Project: {cfg.project.name}",
        vision_block,
    ]
    rb = _research_queue_block(cfg)
    if rb.strip():
        parts.append(rb.rstrip())
    parts += [
        "",
        "## Goals (harness)",
        "\n".join(f"- {g}" for g in cfg.goals.objectives) or "(none)",
        "",
    ]
    if kg is not None:
        xctx = build_cross_layer_context(kg, "product")
        if xctx.strip():
            parts.append("## Maintenance / stability context\n")
            parts.append(xctx)
            parts.append("")
    if cfg.memory.enabled:
        mem = mem_module.load(cfg.memory_dir)
        ctx = mem_module.compact_context(mem, cfg=cfg, kg=kg, evidence=ev)
        if ctx:
            parts.append(f"## Harness memory\n```\n{ctx}\n```\n")
    parts += [
        "## Evidence",
        f"Collected at: {ev.collected_at}",
    ]
    sections = evidence.format_evidence_for_diagnosis(cfg, ev)
    priority = [
        "Metrics",
        "Metric anomalies",
        "Tests",
        "Error patterns",
        "Runtime logs",
        "Last Cursor / Agent CLI failure",
        "Recent changes",
        "Previous cycle history",
        "Project file tree",
        "AST syntax errors",
        "Long functions",
        "Functions (sample)",
        "Classes (sample)",
        "Dependencies",
    ]
    seen = set()
    for title in priority:
        body = sections.get(title)
        if body and body.strip():
            parts.append(f"\n### {title}\n```\n{body.strip()}\n```")
            seen.add(title)
    for title, body in sections.items():
        if title in seen or not (body and body.strip()):
            continue
        parts.append(f"\n### {title}\n```\n{body.strip()}\n```")
    return "\n".join(parts)


def diagnose(
    cfg: HarnessConfig,
    ev: evidence.Evidence,
    kg: Optional["KnowledgeGraph"] = None,
) -> ProductDiagnosis:
    if kg is not None:
        vision_mod.seed_vision(kg, cfg)
    prompt = _build_diagnose_user_prompt(cfg, ev, kg)
    j_timeout = cfg.cursor.json_timeout
    if j_timeout is None:
        j_timeout = cfg.cursor.timeout_seconds
    resp = cursor_client.json_call(
        cfg,
        _PRODUCT_SYSTEM,
        prompt,
        label="product_diagnose",
        timeout_seconds=j_timeout,
        max_retries=cfg.cursor.json_retries,
        parse_retry_user_suffix=_PRODUCT_JSON_RETRY,
    )
    found = _product_from_response(resp)
    if found is not None:
        return found
    return ProductDiagnosis(summary=(resp.raw or resp.error or "")[:500], raw=resp.raw or resp.error or "")


def _strip_leading_yaml_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (`---` … `---`) if present (model sometimes emits id: D1, etc.)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, min(len(lines), 120)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :]).lstrip("\n")
    return text


def _extract_product_directive_title(content: str) -> str:
    body = _strip_leading_yaml_frontmatter(content)
    title = _extract_directive_title(body).strip()
    if not title or _BARE_DIRECTIVE_ID_TITLE.match(title):
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("# ") and not s.upper().startswith("# DIRECTIVE:"):
                cand = s[2:].strip()
                if cand and not _BARE_DIRECTIVE_ID_TITLE.match(cand):
                    return cand[:240]
        return "Untitled product directive"
    return title[:240]


def _next_product_directive_id(directives_dir: Path) -> str:
    """
    Next id is P{n:03d}_auto based only on existing `.metaharness/directives/P*.md`
    (never D* / M* — those are maintenance).
    """
    d = Path(directives_dir)
    nums: list[int] = []
    seen: set[Path] = set()
    for pattern in ("P*.md", "p*.md"):
        for p in d.glob(pattern):
            try:
                r = p.resolve()
            except OSError:
                r = p
            if r in seen:
                continue
            seen.add(r)
            stem = p.stem
            m = re.fullmatch(r"P(\d+)_auto", stem, flags=re.IGNORECASE)
            if m:
                nums.append(int(m.group(1)))
    next_num = (max(nums) + 1) if nums else 1
    return f"P{next_num:03d}_auto"


def _build_propose_prompt(cfg: HarnessConfig, diagnosis: ProductDiagnosis, kg: Optional["KnowledgeGraph"]) -> str:
    mod, prot = product_effective_scope(cfg)
    in_scope = "\n".join(f"  {pat}" for pat in mod)
    protected = "\n".join(f"  {pat}" for pat in prot)
    rq = _research_queue_block(cfg).strip()
    vision = (
        vision_mod.vision_prompt_block(kg, cfg)
        if kg is not None
        else _vision_prompt_block(cfg)
    )
    if rq:
        vision = f"{vision}\n\n{rq}"
    return f"""\
{vision}

---

## Product diagnosis

{diagnosis.summary}

### Existing features
{chr(10).join(f"- {x}" for x in diagnosis.existing_features) or "- (none)"}

### Missing features
{chr(10).join(f"- {x}" for x in diagnosis.missing_features) or "- (none)"}

### User value gaps
{chr(10).join(f"- {x}" for x in diagnosis.user_value_gaps) or "- (none)"}

### Next build targets
{chr(10).join(f"- {x}" for x in diagnosis.next_build_targets) or "- (none)"}

### Maintenance blockers
{chr(10).join(f"- {x}" for x in diagnosis.maintenance_blockers) or "- (none)"}

---

## Scope (product agent)

### May modify (globs)
{in_scope}

### Protected (never touch)
{protected}

Write the directive now. Use directive id prefix **P_** in the body if you mention an id; the harness will assign `P_NNN_auto` when saving.
"""


def propose(
    cfg: HarnessConfig,
    diagnosis: ProductDiagnosis,
    kg: Optional["KnowledgeGraph"] = None,
) -> Directive:
    prompt = _build_propose_prompt(cfg, diagnosis, kg)
    resp = cursor_client.agent_call(cfg, _PROPOSE_SYSTEM, prompt, label="product_propose")
    if not resp.success:
        raise RuntimeError(resp.error or "Product proposer agent_call failed")
    content = _normalize_agent_markdown_body((resp.raw or "").strip())
    if not content or len(content) < 50:
        raise ValueError(
            f"Product proposer returned empty content. Raw: {repr((resp.raw or '')[:200])}"
        )
    body = _strip_leading_yaml_frontmatter(content).strip()
    if len(body) < 50:
        raise ValueError(
            f"Product proposer returned empty content. Raw: {repr((resp.raw or '')[:200])}"
        )
    title = _extract_product_directive_title(body)
    directive_id = _next_product_directive_id(cfg.directives_dir)
    if not re.fullmatch(r"P\d+_auto", directive_id, flags=re.IGNORECASE):
        raise RuntimeError(f"Invalid product directive id (internal bug): {directive_id!r}")
    directive_path = cfg.directives_dir / f"{directive_id}.md"
    header = f"""\
---
id: {directive_id}
generated: {datetime.utcnow().isoformat()}Z
project: {cfg.project.name}
status: pending
layer: product
---

"""
    full_content = header + body
    directive_path.write_text(full_content, encoding="utf-8")
    return Directive(id=directive_id, path=directive_path, title=title, content=full_content)


def product_effective_scope(cfg: HarnessConfig) -> tuple[list[str], list[str]]:
    protected = list(dict.fromkeys(list(cfg.scope.protected) + list(cfg.product.protected)))
    modifiable = cfg.product.modifiable if cfg.product.modifiable else cfg.scope.modifiable
    return modifiable, protected


def _log_product_cycle(cfg: HarnessConfig, outcome: CycleOutcome) -> None:
    cfg.product_cycles_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.product_cycles_dir / f"{outcome.cycle_id}.json"
    data = {
        "cycle_id": outcome.cycle_id,
        "timestamp": outcome.timestamp,
        "directive": outcome.directive_id,
        "directive_title": outcome.directive_title,
        "status": outcome.status.value,
        "delta": outcome.delta,
        "pre_metric": outcome.pre_metric,
        "post_metric": outcome.post_metric,
        "changes_applied": outcome.changes_applied,
        "error": outcome.error,
        "rollback_attempted": outcome.rollback_attempted,
        "rollback_succeeded": outcome.rollback_succeeded,
        "rollback_detail": outcome.rollback_detail,
        "layer": "product",
    }
    if outcome.directive_confidence is not None:
        data["directive_confidence"] = outcome.directive_confidence
    if outcome.directive_confidence_detail:
        data["directive_confidence_detail"] = outcome.directive_confidence_detail
    path.write_text(json.dumps(data, indent=2))


def _product_veto_window(
    cfg: HarnessConfig,
    directive: Directive,
    diagnosis: ProductDiagnosis,
) -> bool:
    seconds = cfg.product.veto_seconds
    if seconds <= 0:
        return True

    if cfg.slack_early_product_approve_path.exists():
        cfg.slack_early_product_approve_path.unlink(missing_ok=True)

    cfg.pending_product_veto_path.write_text(
        f"Directive: {directive.id}\nTitle: {directive.title}\n"
        f"Delete this file within {seconds}s to ABORT the product cycle.\n",
        encoding="utf-8",
    )
    console.print(
        f"[yellow]Product veto window ({seconds}s). "
        f"Delete [bold].metaharness/PENDING_PRODUCT_VETO[/bold] to abort.[/yellow]"
    )
    summary_bits = (diagnosis.next_build_targets or diagnosis.missing_features or [])[:3]
    summary_line = "\n".join(summary_bits)

    if cfg.slack.enabled:
        try:
            from . import slack_integration as slack

            slack.post_product_veto_window(
                cfg,
                directive_id=directive.id,
                directive_title=directive.title,
                directive_summary=summary_line,
                seconds=seconds,
            )
        except Exception as e:
            console.print(f"[dim yellow]Slack product veto post failed: {e}[/dim yellow]")

    socket_handler = None
    socket_started_for_veto = False
    if cfg.slack.enabled:
        try:
            from . import slack_integration as slack

            if slack.socket_tokens_ready(cfg) and not slack.slack_socket_listener_active():
                socket_handler = slack.start_socket_mode(cfg)
                if socket_handler:
                    slack.run_socket_handler_background(
                        socket_handler, thread_name="metaharness-slack-socket-product-veto"
                    )
                    slack.register_slack_socket_listener(socket_handler)
                    socket_started_for_veto = True
        except Exception as e:
            console.print(f"[dim yellow]Slack Socket Mode (product veto) failed: {e}[/dim yellow]")

    try:
        start = time.time()
        while time.time() - start < seconds:
            if cfg.slack_early_product_approve_path.exists():
                cfg.slack_early_product_approve_path.unlink(missing_ok=True)
                console.print("[green]Product early approval (Slack).[/green]")
                if cfg.slack.enabled:
                    try:
                        from . import slack_integration as slack

                        slack.update_product_veto_result(cfg, approved=True)
                    except Exception:
                        pass
                cfg.pending_product_veto_path.unlink(missing_ok=True)
                return True
            if not cfg.pending_product_veto_path.exists():
                console.print("[red bold]Product cycle vetoed.[/red bold]")
                if cfg.slack.enabled:
                    try:
                        from . import slack_integration as slack

                        slack.update_product_veto_result(cfg, approved=False)
                    except Exception:
                        pass
                return False
            time.sleep(2)
        cfg.pending_product_veto_path.unlink(missing_ok=True)
        if cfg.slack.enabled:
            try:
                from . import slack_integration as slack

                slack.update_product_veto_result(cfg, approved=True)
            except Exception:
                pass
        return True
    finally:
        if socket_started_for_veto and socket_handler is not None:
            from . import slack_integration as slack

            try:
                socket_handler.close()
            except Exception:
                pass
            try:
                slack.unregister_slack_socket_listener(socket_handler)
            except Exception:
                pass


def run_product_cycle(cfg: HarnessConfig) -> CycleOutcome:
    outcome = _run_product_cycle_inner(cfg)
    if cfg.slack.enabled:
        try:
            from . import slack_integration as slack

            slack.post_cycle_outcome(cfg, outcome)
        except Exception as e:
            console.print(f"[dim yellow]Slack product outcome post failed: {e}[/dim yellow]")
    return outcome


def _run_product_cycle_inner(cfg: HarnessConfig) -> CycleOutcome:
    cycle_id = f"product_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    now = datetime.utcnow().isoformat() + "Z"
    outcome = CycleOutcome(cycle_id=cycle_id, timestamp=now)
    console.rule(f"[bold magenta]Product Agent Cycle: {cycle_id}[/bold magenta]")
    kg = mem_module.get_kg(cfg)

    console.print("[magenta]▶ Collecting evidence…[/magenta]")
    ev = evidence.collect(cfg)
    if not evidence.has_sufficient_evidence(ev, cfg.cycle.min_evidence_items):
        console.print("[yellow]Insufficient evidence — skipping product cycle.[/yellow]")
        outcome.status = CycleStatus.INSUFFICIENT_EVIDENCE
        _log_product_cycle(cfg, outcome)
        return outcome

    outcome.pre_metric = _read_primary_metric(cfg)

    console.print("[magenta]▶ Product diagnosis…[/magenta]")
    try:
        dx = diagnose(cfg, ev, kg)
        console.print(Panel(dx.summary, title="Product diagnosis", border_style="magenta"))
    except Exception as e:
        outcome.status = CycleStatus.ERROR
        outcome.error = f"Product diagnosis failed: {e}"
        _log_product_cycle(cfg, outcome)
        return outcome

    console.print("[magenta]▶ Generating product directive…[/magenta]")
    try:
        directive = propose(cfg, dx, kg)
        outcome.directive_id = directive.id
        outcome.directive_title = directive.title
        console.print(f"[green]{directive.id} — {directive.title}[/green]")
        try:
            conf = score_directive(cfg, directive, kg, dx.summary)
            outcome.directive_confidence = conf.score
            outcome.directive_confidence_detail = outcome_detail_string(conf)
            console.print(f"[cyan]{format_confidence_rich_line(conf)}[/cyan]")
        except Exception as e:
            console.print(f"[dim yellow]Directive confidence skipped: {e}[/dim yellow]")
    except Exception as e:
        outcome.status = CycleStatus.ERROR
        outcome.error = f"Product proposal failed: {e}"
        _log_product_cycle(cfg, outcome)
        return outcome

    if not _product_veto_window(cfg, directive, dx):
        outcome.status = CycleStatus.VETOED
        _log_product_cycle(cfg, outcome)
        mem_module.persist_cycle_outcome(
            cfg,
            kg=kg,
            directive_id=outcome.directive_id,
            directive_title=outcome.directive_title,
            status=outcome.status.value,
            delta=None,
            files_changed=[],
            pre_metric=outcome.pre_metric,
            post_metric=None,
            directive_content=directive.content,
            failure_detail=None,
            layer="product",
        )
        return outcome

    mod, prot = product_effective_scope(cfg)
    console.print("[magenta]▶ Agent implementing product directive…[/magenta]")
    if not _acquire_agent_lock(cfg):
        outcome.status = CycleStatus.ERROR
        outcome.error = "Could not acquire agent lock"
        _log_product_cycle(cfg, outcome)
        return outcome
    try:
        result = agent.run(
            cfg,
            directive,
            cycle_id=cycle_id,
            evidence=ev,
            reasoning_dir=cfg.product_reasoning_dir,
            scope_modifiable=mod,
            scope_protected=prot,
        )
    except Exception as e:
        outcome.status = CycleStatus.ERROR
        outcome.error = f"Agent error: {e}"
        _log_product_cycle(cfg, outcome)
        return outcome
    finally:
        _release_agent_lock(cfg)

    outcome.phases_completed = result.phases_completed
    if not result.success:
        outcome.status = CycleStatus.AGENT_FAILED
        outcome.error = result.error
        _log_product_cycle(cfg, outcome)
        mem_module.persist_cycle_outcome(
            cfg,
            kg=kg,
            directive_id=outcome.directive_id,
            directive_title=outcome.directive_title,
            status=outcome.status.value,
            delta=None,
            files_changed=[c.path for c in result.changes],
            pre_metric=outcome.pre_metric,
            post_metric=None,
            directive_content=directive.content,
            failure_detail=outcome.error or None,
            layer="product",
        )
        return outcome

    outcome.changes_applied = len(result.changes)
    passed, test_output = _run_tests(cfg)
    if not passed:
        outcome.status = CycleStatus.TEST_FAILED
        outcome.error = test_output[-500:]
        rb = rollback.attempt_restore(cfg, result.changes, kind="test_failure")
        outcome.rollback_attempted = rb.attempted
        outcome.rollback_succeeded = rb.succeeded if rb.attempted else None
        outcome.rollback_detail = rb.detail
        _log_product_cycle(cfg, outcome)
        failing = [m.group(1) for m in re.finditer(r"FAILED\s+(\S+)", test_output)]
        fd_parts: list[str] = []
        if outcome.error:
            fd_parts.append(outcome.error)
        if rb.attempted:
            fd_parts.append(f"rollback: {rb.detail}")
        failure_detail = "\n".join(fd_parts) if fd_parts else None
        files_changed = (
            [] if (rb.attempted and rb.succeeded) else [c.path for c in result.changes]
        )
        mem_module.persist_cycle_outcome(
            cfg,
            kg=kg,
            directive_id=outcome.directive_id,
            directive_title=outcome.directive_title,
            status=outcome.status.value,
            delta=None,
            files_changed=files_changed,
            pre_metric=outcome.pre_metric,
            post_metric=None,
            directive_content=directive.content,
            failing_tests=failing,
            failure_detail=failure_detail,
            layer="product",
        )
        return outcome

    _restart_project(cfg)
    outcome.post_metric = _read_primary_metric(cfg)
    if outcome.pre_metric is not None and outcome.post_metric is not None:
        outcome.delta = outcome.post_metric - outcome.pre_metric

    if (
        outcome.pre_metric is not None
        and outcome.post_metric is not None
        and outcome.delta is not None
        and rollback.is_metric_regression(cfg, outcome.pre_metric, outcome.post_metric)
        and cfg.cycle.rollback_enabled
        and cfg.cycle.rollback_on_metric_regression
    ):
        rb = rollback.attempt_restore(cfg, result.changes, kind="metric_regression")
        outcome.rollback_attempted = rb.attempted
        outcome.rollback_succeeded = rb.succeeded if rb.attempted else None
        outcome.rollback_detail = rb.detail
        outcome.status = CycleStatus.METRIC_REGRESSION
        fd = f"metric regression (delta={outcome.delta:+.4f}); {rb.detail}"
        _log_product_cycle(cfg, outcome)
        files_changed = (
            [] if (rb.attempted and rb.succeeded) else [c.path for c in result.changes]
        )
        mem = mem_module.persist_cycle_outcome(
            cfg,
            kg=kg,
            directive_id=outcome.directive_id,
            directive_title=outcome.directive_title,
            status=outcome.status.value,
            delta=outcome.delta,
            files_changed=files_changed,
            pre_metric=outcome.pre_metric,
            post_metric=outcome.post_metric,
            directive_content=directive.content,
            failure_detail=fd,
            layer="product",
        )
        if mem is not None and mem.completed % cfg.memory.pattern_refresh_every == 0:
            mem_module.refresh_patterns(cfg.memory_dir, kg=kg)
        console.rule(f"[bold yellow]Product cycle ended: {outcome.status.value}[/bold yellow]")
        return outcome

    outcome.status = CycleStatus.COMPLETED
    _log_product_cycle(cfg, outcome)
    mem = mem_module.persist_cycle_outcome(
        cfg,
        kg=kg,
        directive_id=outcome.directive_id,
        directive_title=outcome.directive_title,
        status=outcome.status.value,
        delta=outcome.delta,
        files_changed=[c.path for c in result.changes],
        pre_metric=outcome.pre_metric,
        post_metric=outcome.post_metric,
        directive_content=directive.content,
        failure_detail=None,
        layer="product",
    )
    if mem is not None and mem.completed % cfg.memory.pattern_refresh_every == 0:
        mem_module.refresh_patterns(cfg.memory_dir, kg=kg)

    try:
        vision_mod.evolve_vision(kg, cfg, completed_directive_id=directive.id)
    except Exception as e:
        console.print(f"[dim yellow]Vision evolution failed: {e}[/dim yellow]")

    console.rule(f"[bold green]Product cycle complete: {outcome.status.value}[/bold green]")
    return outcome
