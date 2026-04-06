"""
meta_harness/cycle.py
Orchestrates a single Meta-Harness cycle end-to-end:
  Collect → Diagnose → Propose → Veto → Agent → Test → Restart → Log
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import filelock
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import agent, diagnoser, evidence, proposer, rollback
from . import memory as mem_module
from .config import HarnessConfig
from .platform_runtime import merge_subprocess_no_window_kwargs
from .directive_confidence import format_confidence_rich_line, outcome_detail_string, score_directive
from .diagnoser import Diagnosis
from .proposer import Directive

console = Console()

# Same FileLock instance must be released that acquired (see _acquire_agent_lock / _release_agent_lock).
_agent_lock_handles: dict[str, filelock.BaseFileLock] = {}


def _acquire_agent_lock(cfg: HarnessConfig, timeout: int = 300) -> bool:
    """Wait up to timeout seconds for the agent lock. Returns True if acquired."""
    key = str(cfg.agent_lock_path.resolve())
    lock = filelock.FileLock(key + ".lock")
    try:
        lock.acquire(timeout=timeout)
    except filelock.Timeout:
        console.print("[yellow]Agent lock timeout — another agent is implementing[/yellow]")
        return False
    try:
        cfg.harness_dir.mkdir(parents=True, exist_ok=True)
        cfg.agent_lock_path.write_text("locked", encoding="utf-8")
    except OSError:
        try:
            lock.release()
        except OSError:
            pass
        raise
    _agent_lock_handles[key] = lock
    return True


def _release_agent_lock(cfg: HarnessConfig) -> None:
    key = str(cfg.agent_lock_path.resolve())
    lock = _agent_lock_handles.pop(key, None)
    if lock is not None:
        try:
            if lock.is_locked:
                lock.release()
        except OSError:
            pass
    try:
        cfg.agent_lock_path.unlink(missing_ok=True)
    except OSError:
        pass


class CycleStatus(str, Enum):
    COMPLETED = "COMPLETED"
    VETOED = "VETOED"
    TEST_FAILED = "TEST_FAILED"
    # Tests passed but primary metric regressed and rollback was configured (tree may be restored).
    METRIC_REGRESSION = "METRIC_REGRESSION"
    AGENT_FAILED = "AGENT_FAILED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ERROR = "ERROR"


@dataclass
class CycleOutcome:
    cycle_id: str
    timestamp: str
    directive_id: str = ""
    directive_title: str = ""
    status: CycleStatus = CycleStatus.ERROR
    delta: Optional[float] = None
    changes_applied: int = 0
    error: str = ""
    pre_metric: Optional[float] = None
    post_metric: Optional[float] = None
    phases_completed: int = 0
    rollback_attempted: bool = False
    rollback_succeeded: Optional[bool] = None
    rollback_detail: str = ""
    directive_confidence: Optional[float] = None
    directive_confidence_detail: str = ""


# ── Veto window ────────────────────────────────────────────────────────────────

def _veto_window(
    cfg: HarnessConfig,
    directive: Directive,
    diagnosis_opportunities: list[str] | None = None,
) -> bool:
    """
    Open a veto window. Returns True if the cycle should proceed, False if vetoed.
    Delete PENDING_VETO to abort, or use Slack / SLACK_EARLY_APPROVE to proceed immediately.
    """
    seconds = cfg.cycle.veto_seconds
    if seconds <= 0:
        return True

    if cfg.slack_early_approve_path.exists():
        cfg.slack_early_approve_path.unlink()

    cfg.pending_veto_path.write_text(
        f"Directive: {directive.id}\nTitle: {directive.title}\n"
        f"Delete this file within {seconds}s to ABORT the cycle.\n"
    )

    console.print(
        f"[yellow]Veto window open ({seconds}s). "
        f"Delete [bold].metaharness/PENDING_VETO[/bold] to abort.[/yellow]"
    )

    if cfg.slack.enabled:
        try:
            from . import slack_integration as slack

            slack.post_veto_window(
                cfg,
                directive_id=directive.id,
                directive_title=directive.title,
                directive_summary="\n".join((diagnosis_opportunities or [])[:3]),
                seconds=seconds,
                channel=cfg.maintenance_slack_channel,
            )
        except Exception as e:
            console.print(f"[dim yellow]Slack veto post failed: {e}[/dim yellow]")

    socket_handler = None
    socket_started_for_veto = False
    if cfg.slack.enabled:
        try:
            from . import slack_integration as slack

            if slack.socket_tokens_ready(cfg) and not slack.slack_socket_listener_active():
                socket_handler = slack.start_socket_mode(cfg)
                if socket_handler:
                    slack.run_socket_handler_background(
                        socket_handler, thread_name="metaharness-slack-socket-veto"
                    )
                    slack.register_slack_socket_listener(socket_handler)
                    socket_started_for_veto = True
                    console.print(
                        "[dim]Slack Socket Mode started for veto window (Approve/Veto buttons).[/dim]"
                    )
            elif slack.socket_tokens_ready(cfg) and slack.slack_socket_listener_active():
                console.print(
                    "[dim]Slack Socket Mode already active — reusing existing listener.[/dim]"
                )
        except Exception as e:
            console.print(f"[dim yellow]Slack Socket Mode failed: {e}[/dim yellow]")

    try:
        start = time.time()
        while time.time() - start < seconds:
            if cfg.slack_early_approve_path.exists():
                cfg.slack_early_approve_path.unlink()
                console.print("[green]Early approval received via Slack.[/green]")
                if cfg.slack.enabled:
                    try:
                        from . import slack_integration as slack

                        slack.update_veto_result(cfg, approved=True)
                    except Exception:
                        pass
                if cfg.pending_veto_path.exists():
                    cfg.pending_veto_path.unlink()
                return True
            if not cfg.pending_veto_path.exists():
                console.print("[red bold]Cycle vetoed by operator.[/red bold]")
                if cfg.slack.enabled:
                    try:
                        from . import slack_integration as slack

                        slack.update_veto_result(cfg, approved=False)
                    except Exception:
                        pass
                return False
            time.sleep(2)

        if cfg.pending_veto_path.exists():
            cfg.pending_veto_path.unlink()

        if cfg.slack.enabled:
            try:
                from . import slack_integration as slack

                slack.update_veto_result(cfg, approved=True)
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
            console.print("[dim]Slack Socket Mode stopped (veto window ended).[/dim]")


# ── Test runner ────────────────────────────────────────────────────────────────

def _run_tests(cfg: HarnessConfig) -> tuple[bool, str]:
    """Returns (passed, output)."""
    if cfg.test.command.strip().lower() == "none":
        return True, "(no test command configured)"

    working = cfg.project_root / cfg.test.working_dir
    cmd = cfg.test.command
    # D002: when junit_xml is false, pytest command must include --junitxml itself
    # (avoids duplicate flags). Match is case-insensitive for --junitxml / junitxml.
    if cfg.test.junit_xml and "junitxml" not in cmd.lower():
        jpath = (cfg.harness_dir / "test_results.xml").resolve()
        jpath.parent.mkdir(parents=True, exist_ok=True)
        cmd = f'{cmd} --junitxml="{jpath}"'

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=working,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=cfg.test.timeout_seconds,
            **merge_subprocess_no_window_kwargs(),
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        passed = result.returncode == 0
        # Save for evidence on next cycle
        (cfg.harness_dir / "last_test_output.txt").write_text(output)
        return passed, output
    except subprocess.TimeoutExpired:
        return False, f"Tests timed out after {cfg.test.timeout_seconds}s"
    except Exception as e:
        return False, str(e)


# ── Restart ────────────────────────────────────────────────────────────────────

def _restart_project(cfg: HarnessConfig) -> None:
    if cfg.run.command.strip().lower() == "none":
        return
    working = cfg.project_root / cfg.run.working_dir
    try:
        result = subprocess.run(
            cfg.run.command,
            shell=True,
            cwd=working,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **merge_subprocess_no_window_kwargs(),
        )
        if result.returncode != 0:
            console.print(
                f"[yellow]Warning: restart command exited {result.returncode}: "
                f"{cfg.run.command}[/yellow]"
            )
        else:
            console.print(f"[dim]Project restarted via: {cfg.run.command}[/dim]")
        if cfg.run.settle_seconds > 0:
            time.sleep(cfg.run.settle_seconds)
    except Exception as e:
        console.print(f"[yellow]Warning: could not restart project: {e}[/yellow]")


# ── Metric reading ─────────────────────────────────────────────────────────────

def _read_primary_metric(cfg: HarnessConfig) -> Optional[float]:
    if not cfg.goals.primary_metric:
        return None
    ev = evidence.collect(cfg)
    return ev.metrics.current.get(cfg.goals.primary_metric)


# ── Cycle logger ───────────────────────────────────────────────────────────────

def _log_cycle(cfg: HarnessConfig, outcome: CycleOutcome) -> None:
    cfg.maintenance_cycles_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.maintenance_cycles_dir / f"{outcome.cycle_id}.json"
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
    }
    if outcome.directive_confidence is not None:
        data["directive_confidence"] = outcome.directive_confidence
    if outcome.directive_confidence_detail:
        data["directive_confidence_detail"] = outcome.directive_confidence_detail
    path.write_text(json.dumps(data, indent=2))


# ── Main cycle ─────────────────────────────────────────────────────────────────

def run_cycle(cfg: HarnessConfig) -> CycleOutcome:
    outcome = _run_cycle_inner(cfg)
    if cfg.slack.enabled:
        try:
            from . import slack_integration as slack

            slack.post_cycle_outcome(cfg, outcome)
        except Exception as e:
            console.print(f"[dim yellow]Slack outcome post failed: {e}[/dim yellow]")
    return outcome


def _run_cycle_inner(cfg: HarnessConfig) -> CycleOutcome:
    cycle_id = f"cycle_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    now = datetime.utcnow().isoformat() + "Z"
    outcome = CycleOutcome(cycle_id=cycle_id, timestamp=now)
    console.rule(f"[bold cyan]Meta-Harness Cycle: {cycle_id}[/bold cyan]")
    kg = mem_module.get_kg(cfg)

    # ── 1. Collect evidence ────────────────────────────────────────────────────
    console.print("[cyan]▶ Collecting evidence...[/cyan]")
    ev = evidence.collect(cfg)

    if not evidence.has_sufficient_evidence(ev, cfg.cycle.min_evidence_items):
        console.print("[yellow]Insufficient evidence — skipping cycle.[/yellow]")
        outcome.status = CycleStatus.INSUFFICIENT_EVIDENCE
        _log_cycle(cfg, outcome)
        return outcome

    # ── 2. Pre-cycle metric snapshot ──────────────────────────────────────────
    outcome.pre_metric = _read_primary_metric(cfg)
    if outcome.pre_metric is not None:
        console.print(f"[dim]Pre-cycle metric ({cfg.goals.primary_metric}): {outcome.pre_metric}[/dim]")

    # ── 3. Diagnose ────────────────────────────────────────────────────────────
    console.print("[cyan]▶ Diagnosing...[/cyan]")
    try:
        dx = diagnoser.run(cfg, ev, kg)
        console.print(Panel(dx.summary, title="Diagnosis", border_style="blue"))
        if dx.opportunities:
            console.print("[dim]Opportunities:[/dim]")
            for op in dx.opportunities:
                console.print(f"  [dim]• {op}[/dim]")
    except Exception as e:
        outcome.status = CycleStatus.ERROR
        outcome.error = f"Diagnosis failed: {e}"
        _log_cycle(cfg, outcome)
        return outcome

    # ── 4. Propose directive ───────────────────────────────────────────────────
    console.print("[cyan]▶ Generating directive...[/cyan]")
    try:
        directive = proposer.run(cfg, dx, kg)
        outcome.directive_id = directive.id
        outcome.directive_title = directive.title
        console.print(f"[green]Directive generated: {directive.id} — {directive.title}[/green]")
        try:
            conf = score_directive(cfg, directive, kg, dx.summary)
            outcome.directive_confidence = conf.score
            outcome.directive_confidence_detail = outcome_detail_string(conf)
            console.print(f"[cyan]{format_confidence_rich_line(conf)}[/cyan]")
        except Exception as e:
            console.print(f"[dim yellow]Directive confidence skipped: {e}[/dim yellow]")
    except Exception as e:
        outcome.status = CycleStatus.ERROR
        outcome.error = f"Proposal failed: {e}"
        _log_cycle(cfg, outcome)
        return outcome

    # ── 5. Veto window ─────────────────────────────────────────────────────────
    proceed = _veto_window(cfg, directive, diagnosis_opportunities=dx.opportunities)
    if not proceed:
        outcome.status = CycleStatus.VETOED
        _log_cycle(cfg, outcome)
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
        )
        return outcome

    # ── 6. Agent implements changes ────────────────────────────────────────────
    console.print("[cyan]▶ Agent implementing directive...[/cyan]")
    if not _acquire_agent_lock(cfg):
        outcome.status = CycleStatus.ERROR
        outcome.error = "Could not acquire agent lock"
        _log_cycle(cfg, outcome)
        return outcome
    try:
        result = agent.run(
            cfg,
            directive,
            cycle_id=cycle_id,
            evidence=ev,
            reasoning_dir=cfg.maintenance_reasoning_dir,
        )
    except Exception as e:
        outcome.status = CycleStatus.ERROR
        outcome.error = f"Agent error: {e}"
        _log_cycle(cfg, outcome)
        return outcome
    finally:
        _release_agent_lock(cfg)

    outcome.phases_completed = result.phases_completed

    if not result.success:
        console.print(f"[red]Agent failed: {result.error}[/red]")
        outcome.status = CycleStatus.AGENT_FAILED
        outcome.error = result.error
        _log_cycle(cfg, outcome)
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
        )
        return outcome

    outcome.changes_applied = len(result.changes)
    console.print(
        f"[green]Agent applied {outcome.changes_applied} file change(s) "
        f"(phases completed: {result.phases_completed}/1)[/green]"
    )
    if result.reasoning:
        console.print(f"[dim]Reasoning: {result.reasoning[:300]}[/dim]")

    for ch in result.changes:
        tag = "[green]+[/green]" if ch.action == "write" else "[red]-[/red]"
        console.print(f"  {tag} {ch.path}")

    # ── 7. Run tests ───────────────────────────────────────────────────────────
    console.print("[cyan]▶ Running tests...[/cyan]")
    passed, test_output = _run_tests(cfg)

    if not passed:
        outcome.status = CycleStatus.TEST_FAILED
        outcome.error = test_output[-500:]
        rb = rollback.attempt_restore(cfg, result.changes, kind="test_failure")
        outcome.rollback_attempted = rb.attempted
        outcome.rollback_succeeded = rb.succeeded if rb.attempted else None
        outcome.rollback_detail = rb.detail
        if cfg.cycle.rollback_enabled and cfg.cycle.rollback_on_test_failure:
            if rb.succeeded:
                console.print(
                    "[red bold]Tests FAILED.[/red bold] "
                    "[green]Working tree restored for agent-touched paths (HEAD).[/green]"
                )
            elif rb.attempted:
                console.print(
                    f"[red bold]Tests FAILED.[/red bold] "
                    f"[yellow]Rollback failed: {rb.detail}[/yellow]"
                )
            else:
                console.print(
                    "[red bold]Tests FAILED.[/red bold] "
                    f"[dim](rollback skipped: {rb.detail})[/dim]"
                )
        else:
            console.print(
                "[red bold]Tests FAILED. Changes NOT rolled back (manual review needed).[/red bold]"
            )
        console.print(f"[dim]{test_output[-1000:]}[/dim]")
        _log_cycle(cfg, outcome)
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
        )
        return outcome

    console.print("[green]Tests passed.[/green]")

    # ── 8. Restart project ─────────────────────────────────────────────────────
    _restart_project(cfg)

    # ── 9. Measure delta ───────────────────────────────────────────────────────
    outcome.post_metric = _read_primary_metric(cfg)
    if outcome.pre_metric is not None and outcome.post_metric is not None:
        outcome.delta = outcome.post_metric - outcome.pre_metric
        direction = cfg.goals.optimization_direction
        improved = (outcome.delta > 0 and direction == "maximize") or \
                   (outcome.delta < 0 and direction == "minimize")
        color = "green" if improved else "red"
        console.print(
            f"[{color}]Delta: {outcome.delta:+.4f} "
            f"({cfg.goals.primary_metric}: {outcome.pre_metric:.4f} → {outcome.post_metric:.4f})[/{color}]"
        )

    # Metric regression: tests passed but primary metric moved the wrong way — optional rollback.
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
        # METRIC_REGRESSION: tests passed; metric got worse; working tree may be restored.
        outcome.status = CycleStatus.METRIC_REGRESSION
        fd = f"metric regression (delta={outcome.delta:+.4f}); {rb.detail}"
        _log_cycle(cfg, outcome)
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
        )
        if mem is not None and mem.completed % cfg.memory.pattern_refresh_every == 0:
            mem_module.refresh_patterns(cfg.memory_dir, kg=kg)
        console.rule(f"[bold yellow]Cycle ended: {outcome.status.value}[/bold yellow]")
        return outcome

    # ── 10. Complete ───────────────────────────────────────────────────────────
    outcome.status = CycleStatus.COMPLETED
    _log_cycle(cfg, outcome)

    # ── 11. Update memory + knowledge graph ───────────────────────────────────
    files_changed = [c.path for c in result.changes]
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
        failure_detail=None,
    )
    if mem is not None and mem.completed % cfg.memory.pattern_refresh_every == 0:
        mem_module.refresh_patterns(cfg.memory_dir, kg=kg)

    console.rule(f"[bold green]Cycle Complete: {outcome.status.value}[/bold green]")
    return outcome
