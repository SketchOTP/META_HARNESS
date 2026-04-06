"""
meta_harness/daemon.py
Runs the outer loop continuously at a fixed interval or at scheduled local times.
"""
from __future__ import annotations

import signal
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console

from .config import HarnessConfig, load_config
from .cycle import run_cycle
from .multi_project import MultiProjectRegistry, enabled_projects_in_order
from .platform_runtime import runtime_status_line

console = Console()
_running = True


def _control_plane_pause_path(control_plane_root: Path) -> Path:
    """Shared pause for multi-project mode: file next to ``metaharness-projects.toml``."""
    return Path(control_plane_root) / "DAEMON_PAUSE"


def _control_plane_paused(control_plane_root) -> bool:
    return _control_plane_pause_path(control_plane_root).exists()


def _wait_until_control_plane_unpaused(control_plane_root: Path) -> None:
    """Block while ``DAEMON_PAUSE`` exists at the control-plane root (multi-project only)."""
    import time

    while _control_plane_paused(control_plane_root) and _running:
        console.print(
            "[yellow]Multi-project daemon paused[/yellow] "
            f"(`{ _control_plane_pause_path(control_plane_root) }`). Waiting…"
        )
        time.sleep(5)


def _git_kg_sync_enabled(explicit_flag: bool) -> bool:
    from .git_kg_sync import should_run_git_kg_sync_from_env

    return explicit_flag or should_run_git_kg_sync_from_env()


def _handle_signal(sig, frame):
    global _running
    console.print("\n[yellow]Shutdown signal received — stopping after current cycle.[/yellow]")
    _running = False


def _daemon_paused(cfg: HarnessConfig) -> bool:
    return cfg.daemon_pause_path.exists()


def _wait_until_unpaused(cfg: HarnessConfig) -> None:
    sc = cfg.slack.slash_command.lstrip("/") if cfg.slack.enabled else "metaharness"
    while _daemon_paused(cfg) and _running:
        console.print(
            "[yellow]Daemon paused[/yellow] "
            f"(`.metaharness/DAEMON_PAUSE` or `/{sc} pause`). Waiting…"
        )
        time.sleep(5)


def _next_scheduled_time(schedule: list[str], *, now: datetime | None = None) -> datetime:
    """Return the next datetime matching any HH:MM in ``schedule`` (local time)."""
    if not schedule:
        raise ValueError("schedule must be non-empty")
    base = now if now is not None else datetime.now()
    candidates: list[datetime] = []
    for t in schedule:
        parts = str(t).strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"invalid schedule entry (expected HH:MM): {t!r}")
        h, m = int(parts[0]), int(parts[1])
        candidate = base.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates)


def _interruptible_sleep(total_seconds: float, cfg: HarnessConfig) -> None:
    """Sleep up to ``total_seconds`` in small chunks; honor ``_running`` and DAEMON_PAUSE."""
    if total_seconds <= 0:
        return
    elapsed = 0.0
    while elapsed < total_seconds and _running:
        if _daemon_paused(cfg):
            _wait_until_unpaused(cfg)
            break
        chunk = min(5.0, total_seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk


def _run_product_loop(c: HarnessConfig) -> None:
    """Run product cycles on ``c.product`` schedule or interval until shutdown."""
    from .product_agent import run_product_cycle

    if c.product.schedule and not c.product.catch_up:
        nxt = _next_scheduled_time(c.product.schedule)
        wait = max(0.0, (nxt - datetime.now()).total_seconds())
        if wait > 0:
            console.print(
                f"[dim]Product agent: first cycle at {nxt.strftime('%Y-%m-%d %H:%M')} "
                f"({wait / 60:.0f} min)[/dim]"
            )
            _interruptible_sleep(wait, c)

    while _running:
        run_product_cycle(c)
        if not _running:
            break
        if c.product.schedule:
            nxt = _next_scheduled_time(c.product.schedule)
            wait = max(0.0, (nxt - datetime.now()).total_seconds())
            console.print(
                f"[dim]Next product cycle at {nxt.strftime('%Y-%m-%d %H:%M')} "
                f"({wait / 60:.0f} min)[/dim]"
            )
            _interruptible_sleep(wait, c)
        elif c.product.interval_seconds > 0:
            _interruptible_sleep(float(c.product.interval_seconds), c)
        else:
            break


def _interruptible_sleep_between_multi_rounds(
    total_seconds: float,
    timing_cfg: HarnessConfig,
    control_plane_root: Path,
) -> None:
    """Like :func:`_interruptible_sleep` plus control-plane ``DAEMON_PAUSE`` (multi-project)."""
    if total_seconds <= 0:
        return
    elapsed = 0.0
    while elapsed < total_seconds and _running:
        if _control_plane_paused(control_plane_root):
            _wait_until_control_plane_unpaused(control_plane_root)
            break
        if _daemon_paused(timing_cfg):
            _wait_until_unpaused(timing_cfg)
            break
        chunk = min(5.0, total_seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk


def run_daemon(cfg: HarnessConfig, *, git_kg_sync_after_cycle: bool = False) -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    console.print(f"[dim]{runtime_status_line()}[/dim]")

    use_schedule = bool(cfg.cycle.schedule)
    interval = cfg.cycle.interval_seconds

    if not use_schedule and interval <= 0:
        console.print(
            "[red]Daemon needs either [cycle] interval_seconds > 0 or a non-empty "
            "schedule = [\"HH:MM\", ...] (local time).\n"
            "Use [bold]metaharness run --once[/bold] for a single cycle.[/red]"
        )
        return

    socket_handler = None
    if (
        cfg.slack.enabled
        and cfg.slack.socket_autostart_with_daemon
        and cfg.slack.socket_mode
    ):
        try:
            from . import slack_integration as slack_mod

            if slack_mod.socket_tokens_ready(cfg):
                h = slack_mod.start_socket_mode(cfg)
                if h:
                    slack_mod.run_socket_handler_background(
                        h, thread_name="metaharness-slack-socket"
                    )
                    slack_mod.register_slack_socket_listener(h)
                    socket_handler = h
                    console.print("[green]Slack Socket Mode started.[/green]")
        except Exception as e:
            console.print(f"[yellow]Slack Socket Mode failed to start: {e}[/yellow]")

    product_thread: threading.Thread | None = None
    if cfg.product.enabled:
        prod_sched = bool(cfg.product.schedule)
        prod_int = cfg.product.interval_seconds > 0
        if prod_sched or prod_int:

            product_thread = threading.Thread(
                target=_run_product_loop,
                args=(cfg,),
                name="metaharness-product-agent",
                daemon=True,
            )
            product_thread.start()
            console.print("[green]Product Agent thread started.[/green]")
        else:
            console.print(
                "[dim]Product agent enabled but no schedule/interval — use `metaharness product run`.[/dim]"
            )

    if use_schedule:
        console.print(
            f"[cyan]Meta-Harness daemon started. Schedule (local): "
            f"{', '.join(cfg.cycle.schedule)}. CTRL-C to stop.[/cyan]"
        )
    else:
        console.print(
            f"[cyan]Meta-Harness daemon started. "
            f"Cycle interval: {interval}s. CTRL-C to stop.[/cyan]"
        )

    if use_schedule and not cfg.cycle.catch_up:
        nxt = _next_scheduled_time(cfg.cycle.schedule)
        wait = max(0.0, (nxt - datetime.now()).total_seconds())
        if wait > 0:
            console.print(
                f"[dim]Daemon started. First cycle at "
                f"{nxt.strftime('%Y-%m-%d %H:%M')} "
                f"({wait / 60:.0f} min)[/dim]"
            )
            _interruptible_sleep(wait, cfg)

    try:
        while _running:
            _wait_until_unpaused(cfg)
            if not _running:
                break
            _ = run_cycle(cfg)
            if _git_kg_sync_enabled(git_kg_sync_after_cycle):
                try:
                    from .git_kg_sync import sync_git_to_kg

                    r = sync_git_to_kg(cfg, dry_run=False, full=False)
                    if r.commits_processed or r.initialized_cursor:
                        cs = (r.cursor_sha or "")[:12]
                        console.print(
                            f"[dim]Git KG sync: {r.commits_processed} commit(s); "
                            f"cursor={cs or '?'}…[/dim]"
                        )
                except Exception as e:
                    console.print(f"[yellow]Git KG sync failed (non-fatal): {e}[/yellow]")
            if not _running:
                break

            if use_schedule:
                next_run = _next_scheduled_time(cfg.cycle.schedule)
                wait = max(0.0, (next_run - datetime.now()).total_seconds())
                console.print(
                    f"[dim]Next cycle at {next_run.strftime('%Y-%m-%d %H:%M')} "
                    f"({wait / 60:.0f} min)[/dim]"
                )
                _interruptible_sleep(wait, cfg)
            else:
                console.print(f"[dim]Next cycle in {interval}s...[/dim]")
                _interruptible_sleep(float(interval), cfg)
    finally:
        if socket_handler:
            try:
                socket_handler.close()
            except Exception:
                pass
            try:
                from . import slack_integration as slack_mod

                slack_mod.unregister_slack_socket_listener(socket_handler)
            except Exception:
                pass
        console.print("[yellow]Daemon stopped.[/yellow]")


def run_multi_project_daemon(
    registry: MultiProjectRegistry,
    *,
    git_kg_sync_after_cycle: bool = False,
) -> None:
    """
    Run maintenance cycles sequentially for each enabled project in the registry.

    Pause behavior (multi-project):
    - **Control plane**: ``DAEMON_PAUSE`` next to ``metaharness-projects.toml`` pauses the whole
      daemon (all projects) until removed.
    - **Per project**: each project's ``.metaharness/DAEMON_PAUSE`` is honored before that
      project's cycle (same as single-project mode).

    Timing (interval / schedule) uses the **first enabled** project's ``metaharness.toml``
    ``[cycle]`` section between full rounds.

    Slack Socket Mode is not started (one process, many Slack apps); use per-project
    ``metaharness slack listen`` if needed.

    Product agent: set ``product_project_id`` in the registry to run the product loop for that
    id only; otherwise the product thread is not started.
    """
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    console.print(f"[dim]{runtime_status_line()}[/dim]")

    enabled = enabled_projects_in_order(registry)
    if not enabled:
        console.print("[red]Multi-project registry has no enabled projects.[/red]")
        return

    timing_cfg = load_config(enabled[0].root)
    use_schedule = bool(timing_cfg.cycle.schedule)
    interval = timing_cfg.cycle.interval_seconds

    if not use_schedule and interval <= 0:
        console.print(
            "[red]Multi-project daemon needs the first enabled project's [cycle] to have "
            "interval_seconds > 0 or a non-empty schedule.[/red]"
        )
        return

    console.print(
        "[cyan]Multi-project mode:[/cyan] "
        f"{len(enabled)} enabled project(s); registry: {registry.registry_path}"
    )
    console.print(
        "[dim]Control-plane pause: "
        f"{_control_plane_pause_path(registry.control_plane_root)}; "
        "per-project pause: each project's .metaharness/DAEMON_PAUSE[/dim]"
    )
    console.print(
        "[yellow]Slack Socket Mode autostart is disabled in multi-project mode "
        "(one daemon, multiple projects).[/yellow]"
    )

    product_thread: threading.Thread | None = None
    prod_cfg: HarnessConfig | None = None
    if registry.product_project_id:
        match = next((p for p in registry.projects if p.id == registry.product_project_id), None)
        if match is not None and not match.enabled:
            console.print(
                f"[yellow]product_project_id {registry.product_project_id!r} is disabled in "
                f"the registry; product thread not started.[/yellow]"
            )
        elif match is not None and match.enabled:
            prod_cfg = load_config(match.root)
            if prod_cfg.product.enabled:
                prod_sched = bool(prod_cfg.product.schedule)
                prod_int = prod_cfg.product.interval_seconds > 0
                if prod_sched or prod_int:

                    product_thread = threading.Thread(
                        target=_run_product_loop,
                        args=(prod_cfg,),
                        name="metaharness-product-agent",
                        daemon=True,
                    )
                    product_thread.start()
                    console.print(
                        f"[green]Product Agent thread started for project "
                        f"[bold]{registry.product_project_id}[/bold].[/green]"
                    )
                else:
                    console.print(
                        "[dim]Product agent has no schedule/interval for designated project — "
                        "use `metaharness product run --dir ...`.[/dim]"
                    )
            else:
                console.print(
                    "[yellow]product_project_id points to a project with [product] disabled; "
                    "product thread not started.[/yellow]"
                )
    else:
        console.print(
            "[dim]Product agent thread not started in multi-project mode "
            "(set product_project_id in metaharness-projects.toml to pin one project).[/dim]"
        )

    if use_schedule:
        console.print(
            f"[cyan]Schedule between rounds (from first project): "
            f"{', '.join(timing_cfg.cycle.schedule)}. CTRL-C to stop.[/cyan]"
        )
    else:
        console.print(
            f"[cyan]Interval between full rounds: {interval}s (from first project's "
            f"[cycle]). CTRL-C to stop.[/cyan]"
        )

    if use_schedule and not timing_cfg.cycle.catch_up:
        nxt = _next_scheduled_time(timing_cfg.cycle.schedule)
        wait = max(0.0, (nxt - datetime.now()).total_seconds())
        if wait > 0:
            console.print(
                f"[dim]Daemon started. First full round at "
                f"{nxt.strftime('%Y-%m-%d %H:%M')} "
                f"({wait / 60:.0f} min)[/dim]"
            )
            _interruptible_sleep_between_multi_rounds(wait, timing_cfg, registry.control_plane_root)

    cp = registry.control_plane_root
    try:
        while _running:
            _wait_until_control_plane_unpaused(cp)
            if not _running:
                break
            for proj in enabled:
                if not _running:
                    break
                cfg = load_config(proj.root)
                label = proj.label or proj.id
                console.print(
                    f"[bold cyan]━━ Project[/bold cyan] [green]{proj.id}[/green] "
                    f"({label}) → {proj.root}"
                )
                _wait_until_unpaused(cfg)
                if not _running:
                    break
                _ = run_cycle(cfg)
                if _git_kg_sync_enabled(git_kg_sync_after_cycle):
                    try:
                        from .git_kg_sync import sync_git_to_kg

                        r = sync_git_to_kg(cfg, dry_run=False, full=False)
                        if r.commits_processed or r.initialized_cursor:
                            cs = (r.cursor_sha or "")[:12]
                            console.print(
                                f"[dim][{proj.id}] Git KG sync: {r.commits_processed} commit(s); "
                                f"cursor={cs or '?'}…[/dim]"
                            )
                    except Exception as e:
                        console.print(
                            f"[yellow][{proj.id}] Git KG sync failed (non-fatal): {e}[/yellow]"
                        )
            if not _running:
                break

            if use_schedule:
                next_run = _next_scheduled_time(timing_cfg.cycle.schedule)
                wait = max(0.0, (next_run - datetime.now()).total_seconds())
                console.print(
                    f"[dim]Next full round at {next_run.strftime('%Y-%m-%d %H:%M')} "
                    f"({wait / 60:.0f} min)[/dim]"
                )
                _interruptible_sleep_between_multi_rounds(wait, timing_cfg, cp)
            else:
                console.print(f"[dim]Next full round in {interval}s...[/dim]")
                _interruptible_sleep_between_multi_rounds(float(interval), timing_cfg, cp)
    finally:
        console.print("[yellow]Daemon stopped.[/yellow]")
