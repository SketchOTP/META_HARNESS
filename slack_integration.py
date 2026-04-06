"""
meta_harness/slack_integration.py

Per-project Slack: veto Block Kit, cycle summaries, slash commands, Socket Mode.
Optional dependency: pip install 'meta-harness[slack]'

Slack app OAuth scopes (Bot Token): ``chat:write`` is required to post the veto
message; ``chat:update`` is required to replace blocks after Approve/Veto. Some
workspace tiers or admin settings can block message updates — if ``chat_update``
returns ``not_allowed_token_type`` or ``missing_scope``, re-check the app install
scopes in api.slack.com.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from .config import HarnessConfig
from .research import ResearchEvaluation

console = Console()

# Ephemeral slash-command responses (Slack ~3000 chars).
_SLACK_EPHEMERAL_MAX = 2900
_SLACK_SEP = "─" * 34
# Heavy rule for memmap (distinct from memory/status).
_SLACK_MEMMAP_RULE = "━" * 30

_DIRECTIVE_CHAIN_GLYPH: dict[str, str] = {
    "COMPLETED": "✅",
    "TEST_FAILED": "✗",
    "METRIC_REGRESSION": "📉",
    "AGENT_FAILED": "✗",
    "VETOED": "⊘",
    "ERROR": "✗",
    "INSUFFICIENT_EVIDENCE": "…",
}

_CYCLE_STATUS_EMOJI: dict[str, str] = {
    "COMPLETED": "✅",
    "TEST_FAILED": "🔴",
    "METRIC_REGRESSION": "📉",
    "AGENT_FAILED": "🔴",
    "VETOED": "⊘",
    "INSUFFICIENT_EVIDENCE": "⏭",
    "ERROR": "🔴",
}


def _truncate_slack_ephemeral(text: str, max_len: int = _SLACK_EPHEMERAL_MAX) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 24].rstrip() + "\n_…(truncated)_"


def _slack_format_memory(cfg: HarnessConfig, mem: Any) -> str:
    """Rich mrkdwn summary from `ProjectMemory` (not the agent-injection compact string)."""
    name = (getattr(mem, "project_name", None) or cfg.project.name or "project").strip() or "project"
    lines: list[str] = [
        f"📊 *Harness Memory — {name}*",
        _SLACK_SEP,
    ]

    total = int(getattr(mem, "total_cycles", 0) or 0)
    if total == 0:
        lines.append("_No harness cycles recorded in memory yet._")
        return _truncate_slack_ephemeral("\n".join(lines))

    completed = int(getattr(mem, "completed", 0) or 0)
    failed = int(getattr(mem, "failed", 0) or 0)
    vetoed = int(getattr(mem, "vetoed", 0) or 0)
    lines.append(
        f"🔄 *Cycles:* {total} total | {completed}✓ completed | {failed}✗ failed | {vetoed}⊘ vetoed"
    )

    metric_name = (getattr(mem, "metric_name", None) or "").strip()
    metric_current = getattr(mem, "metric_current", None)
    if metric_name and metric_current is not None:
        baseline = getattr(mem, "metric_baseline", None)
        best = getattr(mem, "metric_best", None)
        b = f"{baseline:.3f}" if isinstance(baseline, (int, float)) else "?"
        c = f"{float(metric_current):.3f}"
        best_part = f" | best={float(best):.3f}" if isinstance(best, (int, float)) else ""
        lines.append(f"📈 *Metric ({metric_name}):* {b} → {c}{best_part}")

    file_touches: dict[str, int] = getattr(mem, "file_touches", None) or {}
    file_successes: dict[str, int] = getattr(mem, "file_successes", None) or {}
    if file_touches:
        lines.append("")
        lines.append("🔥 *Hot Files:*")
        top = sorted(file_touches.items(), key=lambda x: x[1], reverse=True)[:5]
        for path, count in top:
            display = Path(path).name if path else path
            succ = int(file_successes.get(path, 0) or 0)
            pct = round(succ / count * 100) if count else 0
            lines.append(f"  • {display} ×{count} ({pct}%✓)")

    directives: list[Any] = list(getattr(mem, "directives", None) or [])
    completed_dirs = [d for d in directives if (d.get("status") if isinstance(d, dict) else None) == "COMPLETED"]
    wins: list[str] = []
    for d in reversed(completed_dirs):
        if not isinstance(d, dict):
            continue
        did = d.get("id") or "?"
        title = (d.get("title") or "").strip() or "(no title)"
        if len(title) > 52:
            title = title[:49] + "…"
        wins.append(f"  • {did} — {title}")
        if len(wins) >= 3:
            break
    if wins:
        lines.append("")
        lines.append("✅ *Recent Wins:*")
        lines.extend(wins)

    pat_lines: list[str] = []
    for p in (getattr(mem, "failure_patterns", None) or [])[:6]:
        pat_lines.append(f"  • {p}")
    for p in (getattr(mem, "dead_ends", None) or [])[:4]:
        pat_lines.append(f"  • {p} _(dead end)_")
    for p in (getattr(mem, "success_patterns", None) or [])[:4]:
        pat_lines.append(f"  • {p} _(success)_")
    if pat_lines:
        lines.append("")
        lines.append("⚠️ *Learned Patterns:*")
        lines.extend(pat_lines[:12])

    return _truncate_slack_ephemeral("\n".join(lines))


def _slack_memmap(mem: Any, cfg: HarnessConfig) -> str:
    """
    Slack mrkdwn memory map (separate from terminal ``memory.render_map`` ASCII).
    Same underlying ``ProjectMemory`` fields; tuned for emoji, *bold* headers, and ━ dividers.
    """
    name = (getattr(mem, "project_name", None) or cfg.project.name or "project").strip() or "project"
    lines: list[str] = [
        f"*📊 Memory Map — {name}*",
        _SLACK_MEMMAP_RULE,
    ]

    total = int(getattr(mem, "total_cycles", 0) or 0)
    if total == 0:
        lines.append("_No harness memory yet._")
        return "\n".join(lines)

    completed = int(getattr(mem, "completed", 0) or 0)
    failed = int(getattr(mem, "failed", 0) or 0)
    vetoed = int(getattr(mem, "vetoed", 0) or 0)
    lu = (getattr(mem, "last_updated", None) or "").strip()
    if lu and "T" in lu:
        time_part = lu.split("T", 1)[1][:5]
        upd = f"{time_part} UTC"
    elif lu:
        upd = lu[:16] + ("…" if len(lu) > 16 else "")
    else:
        upd = "—"
    lines.append(f"{total} cycles | {completed}✓ {failed}✗ {vetoed}⊘ | Updated {upd}")
    lines.append("")

    directives_all: list[Any] = list(getattr(mem, "directives", None) or [])
    if directives_all:
        lines.append("*🔗 Directive Chain*")
        max_cells = 48
        if len(directives_all) > max_cells:
            n_omit = len(directives_all) - max_cells
            window = directives_all[-max_cells:]
            lines.append(f"_(…{n_omit} older directives omitted)_")
        else:
            window = directives_all
        row_size = 6
        for row_start in range(0, len(window), row_size):
            row = window[row_start : row_start + row_size]
            parts: list[str] = []
            for d in row:
                if not isinstance(d, dict):
                    continue
                st = str(d.get("status") or "")
                g = _DIRECTIVE_CHAIN_GLYPH.get(st, "?")
                did = d.get("id") or "?"
                parts.append(f"{g} {did}")
            line = " → ".join(parts)
            if len(line) > 320:
                line = line[:317] + "…"
            lines.append(line)
        lines.append("")

    file_touches: dict[str, int] = getattr(mem, "file_touches", None) or {}
    file_successes: dict[str, int] = getattr(mem, "file_successes", None) or {}
    if file_touches:
        lines.append("*🔥 File Heat Map*")
        max_touches = max(file_touches.values())
        bar_w = 12
        sorted_files = sorted(file_touches.items(), key=lambda x: x[1], reverse=True)[:8]
        for path, count in sorted_files:
            display = Path(path).name if path else str(path)
            if len(display) > 28:
                display = display[:25] + "…"
            bar_len = max(1, round(count / max_touches * bar_w))
            bar = "█" * bar_len + "░" * (bar_w - bar_len)
            succ = int(file_successes.get(path, 0) or 0)
            pct = round(succ / count * 100) if count else 0
            lines.append(f"{display} ×{count}  {bar}  {pct}%✓")
        lines.append("")

    metric_name = (getattr(mem, "metric_name", None) or "").strip()
    trajectory: list[Any] = list(getattr(mem, "metric_trajectory", None) or [])
    if trajectory and metric_name:
        lines.append(f"*📈 Metric: {metric_name}*")
        baseline = getattr(mem, "metric_baseline", None)
        current = getattr(mem, "metric_current", None)
        best = getattr(mem, "metric_best", None)
        if isinstance(baseline, (int, float)) and isinstance(current, (int, float)):
            gain = float(current) - float(baseline)
            b_str = f"{float(baseline):.3f}"
            c_str = f"{float(current):.3f}"
        else:
            gain = 0.0
            b_str = "?"
            c_str = "?"
        best_str = f"{float(best):.3f}" if isinstance(best, (int, float)) else "?"
        lines.append(
            f"baseline={b_str} → current={c_str} | best={best_str} | gain={gain:+.3f}"
        )
        tail = trajectory[-20:]
        ids = [t[0] for t in tail if isinstance(t, (list, tuple)) and t]
        dot_count = min(len(tail), 24)
        dots = "●" * dot_count + ("…" if len(tail) > 24 else "")
        if len(ids) >= 2:
            span = f"({ids[0]}→{ids[-1]})"
        elif len(ids) == 1:
            span = f"({ids[0]})"
        else:
            span = ""
        lines.append(f"{dots} {span}".rstrip())
        lines.append("")

    fail_pats = list(getattr(mem, "failure_patterns", None) or [])
    succ_pats = list(getattr(mem, "success_patterns", None) or [])
    dead = list(getattr(mem, "dead_ends", None) or [])
    if fail_pats or succ_pats or dead:
        lines.append("*🧠 Learned Patterns*")
        if fail_pats:
            lines.append(" | ".join(f"⚠️ {p}" for p in fail_pats[:8]))
        if succ_pats:
            lines.append(" | ".join(f"✅ {p}" for p in succ_pats[:6]))
        if dead:
            lines.append(" | ".join(f"🧱 {p}" for p in dead[:5]))

    return "\n".join(lines).rstrip()


def _slack_format_product_cycles(cfg: HarnessConfig) -> str:
    name = (cfg.project.name or "project").strip() or "project"
    cdir = cfg.product_cycles_dir
    logs = sorted(cdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    lines = [f"🚀 *Product cycles — {name}*", _SLACK_SEP]
    if not logs:
        lines.append("_No product cycles yet._")
        return _truncate_slack_ephemeral("\n".join(lines))
    for p in logs:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        st = str(data.get("status") or "?")
        emoji = _CYCLE_STATUS_EMOJI.get(st, "•")
        did = data.get("directive") or "?"
        title = (data.get("directive_title") or "").strip() or "(no title)"
        if len(title) > 40:
            title = title[:37] + "…"
        lines.append(f"{emoji} {did} | {title} | {st}")
    return _truncate_slack_ephemeral("\n".join(lines))


def _slack_format_product_roadmap(cfg: HarnessConfig) -> str:
    from .knowledge_graph import KnowledgeGraph, build_cross_layer_context

    name = (cfg.project.name or "project").strip() or "project"
    lines = [f"🗺 *Product roadmap (KG) — {name}*", _SLACK_SEP]
    try:
        kg = KnowledgeGraph(cfg.kg_path)
        nodes = kg.get_nodes_by_layer("product", limit=15)
    except Exception as e:
        lines.append(f"_Could not read graph: {e}_")
        return _truncate_slack_ephemeral("\n".join(lines))
    if not nodes:
        lines.append("_No product-layer directives in the knowledge graph yet._")
    else:
        for n in nodes:
            title = (n["data"].get("title") or n["name"] or n["id"]).strip()
            if len(title) > 48:
                title = title[:45] + "…"
            lines.append(f"• `{n['id']}` — {title} — _{n['status']}_")
    lines.append("")
    lines.append("*Maintenance snapshot (compact)*")
    ctx = build_cross_layer_context(kg, "product")
    if ctx.strip():
        lines.append(ctx[:2000])
    else:
        lines.append("_No maintenance context._")
    return _truncate_slack_ephemeral("\n".join(lines))


def _research_background(cfg: HarnessConfig, url: str) -> None:
    from . import research as research_mod

    paper = research_mod.fetch_paper(url)
    usable = bool(
        (paper.title or "").strip()
        or (paper.abstract or "").strip()
        or (paper.body_excerpt or "").strip()
    )
    if paper.fetch_error and not usable:
        post_message(
            cfg,
            f"❌ Could not fetch paper: {paper.fetch_error}\n`{url}`",
        )
        return
    evaluation = research_mod.evaluate_paper(cfg, paper)
    verdict = research_mod.format_slack_verdict(evaluation)
    if evaluation.recommendation == "implement":
        queued = research_mod.queue_paper(cfg, evaluation)
        if queued:
            notify_research_queue_from_evaluation(cfg, evaluation)
    post_message(cfg, verdict)


def _slack_format_status(cfg: HarnessConfig) -> str:
    """Recent cycle JSON files as mrkdwn, newest first."""
    name = (cfg.project.name or "project").strip() or "project"
    logs: list[Path] = []
    for d in (cfg.maintenance_cycles_dir, cfg.cycles_dir):
        if d.is_dir():
            logs.extend(d.glob("*.json"))
    logs = sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    lines = [f"📋 *Recent Cycles — {name}*", _SLACK_SEP]
    if not logs:
        lines.append("_No cycles yet._")
        return _truncate_slack_ephemeral("\n".join(lines))

    for p in logs:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        st = str(data.get("status") or "?")
        emoji = _CYCLE_STATUS_EMOJI.get(st, "•")
        did = data.get("directive") or "?"
        title = (data.get("directive_title") or "").strip() or "(no title)"
        if len(title) > 42:
            title = title[:39] + "…"
        nf = data.get("changes_applied")
        if nf is None and isinstance(data.get("files_changed"), list):
            nf = len(data["files_changed"])
        if isinstance(nf, int):
            files_part = f"{nf} files"
        else:
            files_part = "? files"
        delta = data.get("delta")
        if isinstance(delta, (int, float)):
            dpart = f"Δ{float(delta):+.3f}"
        else:
            dpart = "Δ n/a"
        lines.append(f"{emoji} {did} | {title} | {files_part} | {dpart}")

    return _truncate_slack_ephemeral("\n".join(lines))

# One Socket Mode connection per process (Slack app token). Daemon registers on start;
# `metaharness run` starts a temporary listener during the veto window only if none active.
_slack_socket_lock = threading.Lock()
_slack_socket_handler: Any = None


def slack_socket_listener_active() -> bool:
    with _slack_socket_lock:
        return _slack_socket_handler is not None


def register_slack_socket_listener(handler: Any) -> None:
    global _slack_socket_handler
    with _slack_socket_lock:
        _slack_socket_handler = handler


def unregister_slack_socket_listener(handler: Any) -> None:
    global _slack_socket_handler
    with _slack_socket_lock:
        if _slack_socket_handler is handler:
            _slack_socket_handler = None


def reset_slack_socket_listener_state() -> None:
    """Clear the process-global Socket Mode handler (used by tests; safe no-op if none set)."""
    global _slack_socket_handler
    with _slack_socket_lock:
        _slack_socket_handler = None


def _socket_client_still_connected(handler: Any) -> bool:
    """True only when the underlying Slack client reports a real boolean True (not MagicMock)."""
    client = getattr(handler, "client", None)
    if client is None:
        return False
    fn = getattr(client, "is_connected", None)
    if not callable(fn):
        return False
    try:
        r = fn()
    except Exception:
        return False
    return r is True


def _run_socket_thread(handler: Any) -> None:
    """
    Run Socket Mode in a background thread without ``handler.start()`` — ``start()`` installs
    SIGINT handlers via ``signal.signal``, which fails on Windows when not on the main thread.
    ``connect()`` only opens the WebSocket.
    """
    try:
        handler.connect()
        while _socket_client_still_connected(handler):
            time.sleep(1)
    except Exception as e:
        console.print(f"[dim yellow]Socket thread error: {e}[/dim yellow]")


def run_socket_handler_background(handler: Any, *, thread_name: str = "metaharness-slack-socket") -> None:
    """Start ``handler`` on a daemon thread (uses ``connect``, not ``start``)."""
    threading.Thread(
        target=_run_socket_thread,
        args=(handler,),
        name=thread_name,
        daemon=True,
    ).start()


try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    from slack_sdk import WebClient

    _SLACK_AVAILABLE = True
except ImportError:
    App = Any  # type: ignore[misc, assignment]
    SocketModeHandler = Any  # type: ignore[misc, assignment]
    WebClient = Any  # type: ignore[misc, assignment]
    _SLACK_AVAILABLE = False

_SLACK_VETO_TS_FILE = "slack_veto_ts.txt"
_SLACK_PRODUCT_VETO_TS_FILE = "slack_product_veto_ts.txt"


def _slack_veto_ts_path(cfg: HarnessConfig) -> Path:
    return cfg.harness_dir / _SLACK_VETO_TS_FILE


def _slack_product_veto_ts_path(cfg: HarnessConfig) -> Path:
    return cfg.harness_dir / _SLACK_PRODUCT_VETO_TS_FILE


def _get_tokens(cfg: HarnessConfig) -> tuple[str, str]:
    """Return (bot_token, app_token). Raises RuntimeError if missing or invalid."""
    bot = (cfg.slack.bot_token or os.environ.get(cfg.slack.bot_token_env, "") or "").strip()
    app = (cfg.slack.app_token or os.environ.get(cfg.slack.app_token_env, "") or "").strip()
    if not bot:
        raise RuntimeError(
            f"Slack bot token not found. Set {cfg.slack.bot_token_env} environment variable."
        )
    if not app:
        raise RuntimeError(
            f"Slack app token not found. Set {cfg.slack.app_token_env} environment variable."
        )
    if not app.startswith("xapp-"):
        raise RuntimeError(
            "Slack app token must be an App-Level Token starting with 'xapp-' "
            "(Socket Mode — create one with connections:write in your Slack app)."
        )
    return bot, app


def _bot_token(cfg: HarnessConfig) -> str:
    return (cfg.slack.bot_token or os.environ.get(cfg.slack.bot_token_env, "") or "").strip()


def slack_ready(cfg: HarnessConfig) -> bool:
    return bool(cfg.slack.enabled and _bot_token(cfg) and cfg.slack.post_channel)


def socket_tokens_ready(cfg: HarnessConfig) -> bool:
    if not cfg.slack.enabled or not cfg.slack.socket_mode or not _SLACK_AVAILABLE:
        return False
    if not slack_ready(cfg):
        return False
    app = (cfg.slack.app_token or os.environ.get(cfg.slack.app_token_env, "") or "").strip()
    return bool(app.startswith("xapp-"))


def _web_client(cfg: HarnessConfig) -> Any:
    return WebClient(token=_bot_token(cfg))


def post_message(cfg: HarnessConfig, text: str) -> Optional[str]:
    if not cfg.slack.enabled or not _SLACK_AVAILABLE:
        return None
    if not slack_ready(cfg):
        return None
    try:
        client = _web_client(cfg)
        resp = client.chat_postMessage(channel=cfg.slack.post_channel, text=text)
        if resp.get("ok"):
            return str(resp.get("ts", "")) or None
    except Exception:
        pass
    return None


def notify_research_queue_item(cfg: HarnessConfig, item: dict[str, Any]) -> None:
    """Post a short mrkdwn alert when a paper lands on the implementation queue."""
    if not cfg.slack.notify_research_queue:
        return
    title = (item.get("title") or item.get("url") or "Research paper").strip()
    url = (item.get("url") or "").strip()
    applicable = (item.get("applicable_to") or "").strip()
    difficulty = (item.get("difficulty") or item.get("implementation_difficulty") or "").strip()
    lines = [
        "📌 *Research — ready to implement*",
        f"*{title}*",
    ]
    if url:
        lines.append(url if url.startswith("http") else f"https://{url}")
    scope_bits = [x for x in (applicable, difficulty) if x]
    if scope_bits:
        lines.append("_" + " | ".join(scope_bits) + "_")
    text = _truncate_slack_ephemeral("\n".join(lines))
    post_message(cfg, text)


def notify_research_queue_from_evaluation(cfg: HarnessConfig, evaluation: ResearchEvaluation) -> None:
    """
    Post the short “ready to implement” ping after ``queue_paper`` returned True.

    Used by both ``metaharness research eval`` and ``/metaharness research <url>`` so the
    Slack payload stays identical. Respect ``[slack] notify_research_queue`` inside
    :func:`notify_research_queue_item`.
    """
    notify_research_queue_item(
        cfg,
        {
            "title": evaluation.title,
            "url": evaluation.url,
            "applicable_to": evaluation.applicable_to,
            "difficulty": evaluation.implementation_difficulty,
        },
    )


def post_veto_window(
    cfg: HarnessConfig,
    directive_id: str,
    directive_title: str,
    directive_summary: str,
    seconds: int,
    *,
    channel: Optional[str] = None,
) -> Optional[str]:
    if not cfg.slack.enabled or not _SLACK_AVAILABLE or not cfg.slack.post_veto_window:
        return None
    if not slack_ready(cfg):
        return None
    post_ch = (channel or "").strip() or cfg.slack.post_channel
    if not post_ch:
        return None
    summary = (directive_summary or "")[:400]
    header_text = f"🔔 Veto Window Open — {directive_id}"
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{directive_title}*\n{summary}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"⏱ {seconds}s veto window",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "action_id": "mh_approve",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Veto", "emoji": True},
                    "action_id": "mh_veto",
                    "style": "danger",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Or delete `.metaharness/PENDING_VETO` to veto from terminal",
                }
            ],
        },
    ]
    try:
        client = _web_client(cfg)
        resp = client.chat_postMessage(
            channel=post_ch,
            text=header_text,
            blocks=blocks,
        )
        if resp.get("ok"):
            ts = str(resp.get("ts", ""))
            if ts:
                ts_file = _slack_veto_ts_path(cfg)
                ts_file.parent.mkdir(parents=True, exist_ok=True)
                ts_file.write_text(f"{post_ch}\n{ts}", encoding="utf-8")
                console.print(f"[dim]Slack veto ts saved: {ts}[/dim]")
                console.print(f"[dim]Slack veto ts path: {ts_file}[/dim]")
            return ts or None
    except Exception:
        pass
    return None


def _read_veto_ts_file(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return "", ""
    if "\n" in raw:
        ch, ts = raw.split("\n", 1)
        return ch.strip(), ts.strip()
    return "", raw


def update_veto_result(cfg: HarnessConfig, approved: bool) -> None:
    if not cfg.slack.enabled or not _SLACK_AVAILABLE:
        return
    ts_path = cfg.harness_dir / _SLACK_VETO_TS_FILE
    try:
        if not ts_path.exists():
            console.print(
                "[yellow]slack_veto_ts.txt not found — skipping message update[/yellow]"
            )
            console.print(f"[dim]update_veto_result: looked for {ts_path.resolve()}[/dim]")
            return
        console.print(f"[dim]update_veto_result: found {ts_path.resolve()}[/dim]")
        ch_stored, ts = _read_veto_ts_file(ts_path)
        if not ts:
            console.print("[yellow]slack_veto_ts.txt empty — skipping message update[/yellow]")
            ts_path.unlink(missing_ok=True)
            return
        bot_token = _bot_token(cfg)
        if not bot_token:
            console.print("[yellow]update_veto_result: no bot token[/yellow]")
            return
        ch = ch_stored or cfg.slack.post_channel
        if not ch:
            console.print("[yellow]update_veto_result: no Slack channel configured[/yellow]")
            return
        console.print(
            f"[dim]update_veto_result: chat_update channel={ch!r} ts={ts!r} approved={approved}[/dim]"
        )
        client = WebClient(token=bot_token)
        text = "✅ Approved — proceeding" if approved else "🚫 Vetoed by operator"
        resp = client.chat_update(channel=ch, ts=ts, text=text, blocks=[])
        if not resp.get("ok"):
            console.print(
                f"[yellow]update_veto_result: chat_update failed: {resp.get('error')}[/yellow]"
            )
            return
        console.print("[dim]update_veto_result: chat_update ok — removed slack_veto_ts.txt[/dim]")
        ts_path.unlink(missing_ok=True)
    except Exception as e:
        console.print(f"[yellow]update_veto_result error: {e}[/yellow]")


def post_product_veto_window(
    cfg: HarnessConfig,
    directive_id: str,
    directive_title: str,
    directive_summary: str,
    seconds: int,
) -> Optional[str]:
    if not cfg.slack.enabled or not _SLACK_AVAILABLE or not cfg.slack.post_veto_window:
        return None
    if not slack_ready(cfg):
        return None
    post_ch = cfg.product_slack_channel
    if not post_ch:
        return None
    summary = (directive_summary or "")[:400]
    header_text = f"🚀 Product Directive — {directive_id}"
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Build proposal:* {directive_title}\n{summary}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"⏱ {seconds}s to veto this product cycle",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve build", "emoji": True},
                    "action_id": "mh_product_approve",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Veto", "emoji": True},
                    "action_id": "mh_product_veto",
                    "style": "danger",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Or delete `.metaharness/PENDING_PRODUCT_VETO` to abort from terminal",
                }
            ],
        },
    ]
    try:
        client = _web_client(cfg)
        resp = client.chat_postMessage(
            channel=post_ch,
            text=header_text,
            blocks=blocks,
        )
        if resp.get("ok"):
            ts = str(resp.get("ts", ""))
            if ts:
                ts_file = _slack_product_veto_ts_path(cfg)
                ts_file.parent.mkdir(parents=True, exist_ok=True)
                ts_file.write_text(f"{post_ch}\n{ts}", encoding="utf-8")
            return ts or None
    except Exception:
        pass
    return None


def update_product_veto_result(cfg: HarnessConfig, approved: bool) -> None:
    if not cfg.slack.enabled or not _SLACK_AVAILABLE:
        return
    ts_path = _slack_product_veto_ts_path(cfg)
    try:
        if not ts_path.exists():
            return
        ch_stored, ts = _read_veto_ts_file(ts_path)
        if not ts:
            ts_path.unlink(missing_ok=True)
            return
        bot_token = _bot_token(cfg)
        if not bot_token:
            return
        ch = ch_stored or cfg.product_slack_channel
        if not ch:
            return
        client = WebClient(token=bot_token)
        text = "✅ Approved — product build proceeding" if approved else "🚫 Product build vetoed"
        resp = client.chat_update(channel=ch, ts=ts, text=text, blocks=[])
        if resp.get("ok"):
            ts_path.unlink(missing_ok=True)
    except Exception as e:
        console.print(f"[yellow]update_product_veto_result error: {e}[/yellow]")


def post_cycle_outcome(cfg: HarnessConfig, outcome: Any) -> None:
    if not cfg.slack.enabled or not _SLACK_AVAILABLE or not cfg.slack.post_cycle_result:
        return
    if not slack_ready(cfg):
        return
    emoji = {
        "COMPLETED": "✅",
        "TEST_FAILED": "🔴",
        "METRIC_REGRESSION": "📉",
        "AGENT_FAILED": "🔴",
        "VETOED": "⊘",
        "INSUFFICIENT_EVIDENCE": "⏭",
        "ERROR": "💥",
    }.get(outcome.status.value, "•")
    lines = [
        f"{emoji}  {outcome.directive_id} — {outcome.directive_title}",
        f"Status: {outcome.status.value}",
    ]
    if getattr(outcome, "directive_confidence", None) is not None:
        d = getattr(outcome, "directive_confidence_detail", "") or ""
        lines.append(
            f"Predicted success: {int(round(float(outcome.directive_confidence) * 100))}% — {d[:200]}"
        )
    if outcome.pre_metric is not None and outcome.post_metric is not None:
        d = outcome.delta
        if d is not None:
            lines.append(
                f"Delta: {outcome.pre_metric} → {outcome.post_metric} ({d:+.4f})"
            )
        else:
            lines.append(
                f"Delta: {outcome.pre_metric} → {outcome.post_metric}"
            )
    lines.append(f"Changes: {outcome.changes_applied} file(s)")
    lines.append(f"Phases: {outcome.phases_completed}/1")
    text = "\n".join(lines)
    try:
        client = _web_client(cfg)
        client.chat_postMessage(channel=cfg.slack.post_channel, text=text)
    except Exception:
        pass


def handle_slash_command(cfg: HarnessConfig, command: str, args: str) -> str:
    sub = (command or "").strip().lower()
    sc = cfg.slack.slash_command.lstrip("/")

    if sub == "product":
        parts2 = args.strip().split(None, 1)
        pverb = (parts2[0] if parts2 else "help").lower()
        if pverb in ("help", ""):
            return (
                f"*/{sc} product*\n"
                "• `product status` — recent product cycles\n"
                "• `product roadmap` — product KG + maintenance snapshot\n"
                "• `product proceed` — approve pending *product* veto from Slack\n"
                "• `product veto` — abort product cycle (remove PENDING_PRODUCT_VETO)"
            )
        if pverb == "status":
            return _slack_format_product_cycles(cfg)
        if pverb == "roadmap":
            return _slack_format_product_roadmap(cfg)
        if pverb == "proceed":
            cfg.slack_early_product_approve_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.slack_early_product_approve_path.write_text("", encoding="utf-8")
            return "Product early approve set — product veto window will proceed if open."
        if pverb == "veto":
            cfg.pending_product_veto_path.unlink(missing_ok=True)
            return "Product cycle vetoed ⊘"
        return f"Unknown `product` subcommand. Try `/{sc} product help`."

    if sub == "research":
        from . import research as research_mod

        a = (args or "").strip()
        if not a:
            return (
                "Usage: `/metaharness research <url>`\n"
                "• `research <url>` — evaluate a research paper for implementation\n"
                "• `research discard <url>` — remove from implementation queue\n"
                "• `research queue` — show pending research queue"
            )
        if a.lower() == "queue":
            queue = research_mod.get_queue(cfg)
            if not queue:
                return "📚 Research queue is empty."
            lines = ["*📚 Research Queue*", "━" * 30]
            for item in queue:
                t = str(item.get("title", "") or "")
                if len(t) > 50:
                    t = t[:47] + "…"
                lines.append(f"• *{t}*")
                lines.append(
                    f"  → `{item.get('applicable_to', '')}` | {item.get('difficulty', '')}"
                )
            return _truncate_slack_ephemeral("\n".join(lines))
        if a.lower().startswith("discard "):
            target_url = a[8:].strip()
            removed = research_mod.clear_queue_item(cfg, target_url)
            return "✅ Removed from queue." if removed else "❌ URL not found in queue."
        url = a
        threading.Thread(
            target=_research_background,
            args=(cfg, url),
            daemon=True,
            name="metaharness-research",
        ).start()
        return f"🔍 Evaluating paper — result posts in ~30s\n`{url}`"

    if sub == "vision":
        from . import vision as vision_mod
        from .memory import get_kg

        kg = get_kg(cfg)
        try:
            v = vision_mod.load_vision(kg)
            if not v:
                return "No vision in KG yet. Run a product cycle first."
            wanted = v.get("features_wanted", [])
            done_count = len(vision_mod.derive_features_done(kg))
            lines = [
                "*Product Vision*",
                "=" * 30,
                v.get("statement", "")[:300],
                "",
                f"*Features wanted ({len(wanted)}):*",
            ]
            for w in wanted[:8]:
                lines.append(f"  - {w}")
            lines.append(f"\n*Features shipped (derived from KG):* {done_count}")
            lines.append(f"_Evolution count: {v.get('evolution_count', 0)}_")
            return "\n".join(lines)[:2900]
        finally:
            kg.close()

    if sub == "platform":
        from .platform_runtime import format_slack_runtime_block

        return format_slack_runtime_block(cfg.cursor.agent_bin)

    if sub in ("help", ""):
        return (
            f"*/{sc}* — Meta-Harness\n"
            "• `platform` — OS, Python launcher, Cursor agent resolution\n"
            "• `status` — last 5 maintenance cycles (formatted)\n"
            "• `memory` — harness memory summary (mrkdwn)\n"
            "• `memmap` — memory map (mrkdwn, like memory/status)\n"
            "• `pause` — pause daemon (DAEMON_PAUSE)\n"
            "• `resume` — resume daemon\n"
            "• `proceed` — approve pending *maintenance* veto window from Slack\n"
            "• `veto` — abort maintenance cycle (remove PENDING_VETO)\n"
            "• `product …` — product agent (`help`, `status`, `roadmap`, `proceed`)\n"
            "• `research <url>` — evaluate a research paper for implementation\n"
            "• `research discard <url>` — remove from implementation queue\n"
            "• `research queue` — show pending research queue\n"
            "• `vision` — show current evolved product vision\n"
            "• `help` — this message"
        )

    if sub == "status":
        return _slack_format_status(cfg)

    if sub == "memory":
        from . import memory as mem_module

        if not cfg.memory.enabled:
            return "Memory disabled in metaharness.toml."
        mem = mem_module.load(cfg.memory_dir)
        return _slack_format_memory(cfg, mem)

    if sub == "memmap":
        from . import memory as mem_module

        if not cfg.memory.enabled:
            return "Memory disabled in metaharness.toml."
        mem = mem_module.load(cfg.memory_dir)
        return _truncate_slack_ephemeral(_slack_memmap(mem, cfg))

    if sub == "pause":
        cfg.daemon_pause_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.daemon_pause_path.write_text("", encoding="utf-8")
        return "Daemon paused ⏸"

    if sub == "resume":
        cfg.daemon_pause_path.unlink(missing_ok=True)
        return "Daemon resumed ▶"

    if sub == "proceed":
        cfg.slack_early_approve_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.slack_early_approve_path.write_text("", encoding="utf-8")
        return "Early approve signal set — cycle will proceed if in veto window."

    if sub == "veto":
        cfg.pending_veto_path.unlink(missing_ok=True)
        return "Maintenance cycle vetoed ⊘"

    return f"Unknown command `{sub}`. Try `/{sc} help`."


def _register_socket_handlers(bolt_app: Any, cfg: HarnessConfig) -> None:
    cmd = f"/{cfg.slack.slash_command.lstrip('/')}"

    @bolt_app.command(cmd)
    def slash_handler(ack, command):
        text = (command.get("text") or "").strip()
        parts = text.split(None, 1)
        verb = parts[0] if parts else "help"
        rest = parts[1] if len(parts) > 1 else ""
        try:
            out = handle_slash_command(cfg, verb, rest)
            return ack(text=out, response_type="ephemeral")
        except Exception as e:
            return ack(text=f"Error: {e}", response_type="ephemeral")

    @bolt_app.action("mh_approve")
    def handle_approve(ack):
        ack()  # must be first — Slack times out ~3s waiting for acknowledgement
        try:
            console.print("[dim]Slack approve handler fired[/dim]")
            cfg.slack_early_approve_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.slack_early_approve_path.write_text("approved", encoding="utf-8")
            update_veto_result(cfg, approved=True)
        except Exception as e:
            console.print(f"[yellow]Slack approve handler error: {e}[/yellow]")

    @bolt_app.action("mh_veto")
    def handle_veto(ack):
        ack()  # must be first — Slack times out ~3s waiting for acknowledgement
        try:
            update_veto_result(cfg, approved=False)
            cfg.pending_veto_path.unlink(missing_ok=True)
        except Exception as e:
            console.print(f"[yellow]Slack veto handler error: {e}[/yellow]")

    @bolt_app.action("mh_product_approve")
    def handle_product_approve(ack):
        ack()
        try:
            cfg.slack_early_product_approve_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.slack_early_product_approve_path.write_text("approved", encoding="utf-8")
            update_product_veto_result(cfg, approved=True)
        except Exception as e:
            console.print(f"[yellow]Slack product approve handler error: {e}[/yellow]")

    @bolt_app.action("mh_product_veto")
    def handle_product_veto(ack):
        ack()
        try:
            update_product_veto_result(cfg, approved=False)
            cfg.pending_product_veto_path.unlink(missing_ok=True)
        except Exception as e:
            console.print(f"[yellow]Slack product veto handler error: {e}[/yellow]")


def start_socket_mode(cfg: HarnessConfig) -> Any:
    """
    Build Bolt app + SocketModeHandler. Does not open the socket; caller runs
    ``run_socket_handler_background(handler)`` from a worker thread, or ``handler.start()``
    when blocking on the main thread (CLI).
    """
    if not cfg.slack.enabled or not _SLACK_AVAILABLE:
        return None
    if not socket_tokens_ready(cfg):
        return None
    try:
        bot, app_token = _get_tokens(cfg)
    except RuntimeError:
        return None
    bolt_app = App(token=bot)
    _register_socket_handlers(bolt_app, cfg)
    return SocketModeHandler(bolt_app, app_token)


def slack_test(cfg: HarnessConfig) -> None:
    if not cfg.slack.enabled:
        return
    if not _SLACK_AVAILABLE:
        raise RuntimeError("slack-bolt / slack-sdk not installed. pip install 'meta-harness[slack]'")
    if not slack_ready(cfg):
        raise RuntimeError(
            "Slack not configured: set [slack] enabled = true, channel, and bot token."
        )
    msg = f"🤖 Meta-Harness Slack integration working — {cfg.project.name}"
    try:
        client = _web_client(cfg)
        resp = client.chat_postMessage(channel=cfg.slack.post_channel, text=msg)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "chat_postMessage failed"))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Slack post failed: {e}") from e


def run_socket_mode(cfg: HarnessConfig) -> None:
    """Blocking Socket Mode (used by CLI when not using background thread)."""
    if not cfg.slack.enabled or not _SLACK_AVAILABLE:
        raise RuntimeError("Slack is disabled or slack-bolt is not installed.")
    _get_tokens(cfg)
    handler = start_socket_mode(cfg)
    if not handler:
        raise RuntimeError(
            "Slack Socket Mode could not start — check tokens, channel, and socket_mode."
        )
    handler.start()
