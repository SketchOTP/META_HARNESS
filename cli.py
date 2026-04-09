"""
meta_harness/cli.py
Click-based CLI: metaharness init | run | daemon | status | platform
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .dashboard import DEFAULT_HOST as DASHBOARD_DEFAULT_HOST

console = Console()


def _find_project_root(start: Path) -> Path:
    """Walk up until metaharness.toml is found, or use cwd."""
    current = start
    for _ in range(10):
        if (current / "metaharness.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start  # Fall back to cwd


def _discover_project_registry(start: Path):
    """Load ``metaharness-projects.toml`` by walking up from ``start``; ``None`` if absent."""
    from .multi_project import find_registry_file, load_project_registry

    discovered = find_registry_file(start.resolve())
    if discovered is None:
        return None
    return load_project_registry(discovered.parent, registry_file=discovered)


@click.group()
def main():
    """Meta-Harness — self-improving outer loop for any project."""
    pass


# ── init ───────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option("--force", is_flag=True, help="Overwrite existing metaharness.toml")
def init(target_dir: str, force: bool):
    """Scaffold a metaharness.toml config in your project."""
    root = Path(target_dir).resolve()
    config_dest = root / "metaharness.toml"

    if config_dest.exists() and not force:
        console.print(
            f"[yellow]metaharness.toml already exists at {config_dest}\n"
            "Use --force to overwrite.[/yellow]"
        )
        return

    template = Path(__file__).parent / "templates" / "metaharness.toml"
    shutil.copy(template, config_dest)

    # Also create .metaharness/ dirs and .gitignore entry
    harness_dir = root / ".metaharness"
    harness_dir.mkdir(exist_ok=True)
    (harness_dir / "directives").mkdir(exist_ok=True)
    (harness_dir / "cycles").mkdir(exist_ok=True)
    (harness_dir / "cycles" / "maintenance").mkdir(exist_ok=True)
    (harness_dir / "cycles" / "product").mkdir(exist_ok=True)

    gitignore = root / ".gitignore"
    entry = "\n# Meta-Harness runtime\n.metaharness/\n"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".metaharness" not in content:
            gitignore.write_text(content + entry)
    else:
        gitignore.write_text(entry.strip() + "\n")

    console.print(f"[green]✓ Initialized Meta-Harness at {root}[/green]")
    console.print(f"[dim]Edit [bold]metaharness.toml[/bold] to configure your project.[/dim]")
    console.print(f"[dim]Then run [bold]metaharness run --once[/bold] to try a cycle.[/dim]")


# ── run (single cycle) ─────────────────────────────────────────────────────────

@main.command("run")
@click.option("--once", is_flag=True, default=True, help="Run a single cycle (default)")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def run_cmd(once: bool, target_dir: str):
    """Run a single Meta-Harness cycle."""
    from .config import load_config
    from .cycle import run_cycle

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)

    if not (root / "metaharness.toml").exists():
        console.print(
            "[red]No metaharness.toml found. Run [bold]metaharness init[/bold] first.[/red]"
        )
        raise SystemExit(1)

    outcome = run_cycle(cfg)
    raise SystemExit(0 if outcome.status.value == "COMPLETED" else 1)


# ── daemon ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option(
    "--projects-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Explicit path to metaharness-projects.toml (multi-project daemon). "
    "Default: walk up from --dir for metaharness-projects.toml.",
)
@click.option(
    "--git-kg-sync",
    is_flag=True,
    default=False,
    help="After each maintenance cycle, sync git commits into the KG (also: METAHARNESS_GIT_KG_SYNC=1).",
)
def daemon(target_dir: str, projects_file: Path | None, git_kg_sync: bool):
    """Run the outer loop continuously at the configured interval."""
    from .config import load_config
    from .daemon import run_daemon, run_multi_project_daemon

    start = Path(target_dir).resolve()

    if projects_file is not None:
        pf = projects_file.resolve()
        if not pf.is_file():
            console.print(f"[red]Projects file not found: {pf}[/red]")
            raise SystemExit(1)
        from .multi_project import load_project_registry

        reg = load_project_registry(pf.parent, registry_file=pf)
        if reg is None:
            console.print(f"[red]Could not load registry: {pf}[/red]")
            raise SystemExit(1)
        run_multi_project_daemon(reg, git_kg_sync_after_cycle=git_kg_sync)
        return

    reg = _discover_project_registry(start)
    if reg is not None:
        run_multi_project_daemon(reg, git_kg_sync_after_cycle=git_kg_sync)
        return

    root = _find_project_root(start)
    cfg = load_config(root)
    run_daemon(cfg, git_kg_sync_after_cycle=git_kg_sync)


# ── platform (runtime introspection) ────────────────────────────────────────────


@main.command("platform")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def platform_cmd(target_dir: str):
    """Show resolved OS, Python launcher, and Cursor agent executable."""
    from .config import load_config
    from .platform_runtime import describe_runtime_for_harness

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    console.print(describe_runtime_for_harness(cfg.cursor.agent_bin))


# ── status ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option(
    "--projects-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Explicit path to metaharness-projects.toml (show registry in status).",
)
@click.option("-n", default=10, help="Number of cycles to show")
def status(target_dir: str, projects_file: Path | None, n: int):
    """Show recent cycle history."""
    from .config import load_config
    from .platform_runtime import describe_runtime_for_harness

    start = Path(target_dir).resolve()

    if projects_file is not None:
        pf = projects_file.resolve()
        if not pf.is_file():
            console.print(f"[red]Projects file not found: {pf}[/red]")
            raise SystemExit(1)
        from .multi_project import load_project_registry

        reg = load_project_registry(pf.parent, registry_file=pf)
        if reg is None:
            console.print(f"[red]Could not load registry: {pf}[/red]")
            raise SystemExit(1)
    else:
        reg = _discover_project_registry(start)

    if reg is not None:
        console.print(f"[bold]Multi-project registry[/bold] {reg.registry_path}")
        if reg.product_project_id:
            console.print(f"[dim]product_project_id:[/dim] {reg.product_project_id}")
        pt = Table(title="Registered projects", show_lines=True)
        pt.add_column("id", style="cyan")
        pt.add_column("enabled")
        pt.add_column("label", style="dim")
        pt.add_column("root")
        for p in reg.projects:
            pt.add_row(
                p.id,
                "yes" if p.enabled else "no",
                p.label or "—",
                str(p.root),
            )
        console.print(pt)
        console.print()

    root = _find_project_root(start)
    cfg = load_config(root)

    for line in describe_runtime_for_harness(cfg.cursor.agent_bin).splitlines():
        console.print(f"[dim]{line}[/dim]")
    console.print()

    logs: list[Path] = []
    for d in (cfg.maintenance_cycles_dir, cfg.cycles_dir):
        if d.is_dir():
            logs.extend(d.glob("*.json"))
    logs = sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True)[:n]

    if not logs:
        console.print("[dim]No cycles recorded yet.[/dim]")
        return

    table = Table(title=f"Meta-Harness — {cfg.project.name}", show_lines=True)
    table.add_column("Timestamp", style="dim")
    table.add_column("Directive")
    table.add_column("Conf")
    table.add_column("Status")
    table.add_column("Changes")
    table.add_column("Delta")

    for log in reversed(logs):
        try:
            data = json.loads(log.read_text(encoding="utf-8"))
            ts = data.get("timestamp", "?")[:19].replace("T", " ")
            d_id = data.get("directive", "-")
            title = data.get("directive_title", "")
            status_val = data.get("status", "?")
            changes = str(data.get("changes_applied", "-"))
            delta = data.get("delta")

            status_color = {
                "COMPLETED": "green",
                "VETOED": "yellow",
                "TEST_FAILED": "red",
                "AGENT_FAILED": "red",
                "INSUFFICIENT_EVIDENCE": "dim",
                "ERROR": "red",
            }.get(status_val, "white")

            delta_str = f"{delta:+.4f}" if delta is not None else "-"
            directive_str = f"{d_id}\n[dim]{title[:50]}[/dim]" if title else d_id
            dc = data.get("directive_confidence")
            conf_str = ""
            if dc is not None:
                try:
                    pct = int(round(float(dc) * 100))
                    det = str(data.get("directive_confidence_detail") or "")
                    m = re.search(r"\((high|medium|low)\)", det)
                    tier = m.group(1) if m else ""
                    conf_str = f"{pct}% ({tier})" if tier else f"{pct}%"
                except (TypeError, ValueError):
                    conf_str = "-"
            else:
                conf_str = "-"

            table.add_row(
                ts,
                directive_str,
                conf_str,
                f"[{status_color}]{status_val}[/{status_color}]",
                changes,
                delta_str,
            )
        except Exception:
            pass

    console.print(table)

    # Show pending directive if any
    pending_dirs = list(cfg.directives_dir.glob("D*_auto.md")) + list(
        cfg.directives_dir.glob("M*_auto.md")
    )
    if pending_dirs:
        latest = sorted(pending_dirs, key=lambda p: p.stat().st_mtime)[-1]
        console.print(f"\n[dim]Latest directive: {latest.name}[/dim]")


@main.command()
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option("--id", "directive_id", required=True,
              help="Directive ID (e.g. P003_auto, research_agent, my_fix)")
@click.option("--title", required=True, help="Short human-readable title")
@click.option("--status", "status_val", default="COMPLETED",
              type=click.Choice(
                  [
                      "COMPLETED",
                      "TEST_FAILED",
                      "METRIC_REGRESSION",
                      "AGENT_FAILED",
                      "VETOED",
                      "ERROR",
                  ]
              ),
              help="Outcome status (default: COMPLETED)")
@click.option("--layer", default="maintenance",
              type=click.Choice(["maintenance", "product"]),
              help="Which layer this belongs to (default: maintenance)")
@click.option("--files", default="", help="Comma-separated list of files changed")
@click.option("--pre-metric", "pre_metric", type=float, default=None,
              help="Metric value before (optional)")
@click.option("--post-metric", "post_metric", type=float, default=None,
              help="Metric value after (optional)")
@click.option("--note", default="", help="Optional note stored in cycle JSON and KG summary")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Sync even if a daemon cycle already exists for this directive ID or KG shows COMPLETED",
)
def sync(
    target_dir: str,
    directive_id: str,
    title: str,
    status_val: str,
    layer: str,
    files: str,
    pre_metric: float | None,
    post_metric: float | None,
    note: str,
    force: bool,
):
    """Ingest a human-issued directive into the KG, memory, and cycle history."""
    from datetime import datetime

    from . import memory as mem_module
    from .config import load_config
    from .knowledge_graph import KnowledgeGraph

    root = _find_project_root(Path(target_dir).resolve())
    if not (root / "metaharness.toml").exists():
        console.print(
            "[red]No metaharness.toml found. Run [bold]metaharness init[/bold] first.[/red]"
        )
        raise SystemExit(1)

    cfg = load_config(root)

    files_changed = [p.strip() for p in files.split(",") if p.strip()]

    if pre_metric is not None and post_metric is not None:
        delta = post_metric - pre_metric
    else:
        delta = None

    cycle_id = f"sync_{directive_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    timestamp = datetime.utcnow().isoformat() + "Z"

    cycle_dir = cfg.product_cycles_dir if layer == "product" else cfg.maintenance_cycles_dir
    cycle_dir.mkdir(parents=True, exist_ok=True)

    if not force:
        for path in cycle_dir.glob("*.json"):
            try:
                existing_data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if existing_data.get("directive") != directive_id:
                continue
            if existing_data.get("synced") is True:
                continue
            console.print(
                f"[yellow]Warning: daemon cycle already exists for {directive_id}. "
                f"Use --force to sync anyway.[/yellow]"
            )
            console.print(f"[dim]  Existing: {path.name}[/dim]")
            return

    payload = {
        "cycle_id": cycle_id,
        "timestamp": timestamp,
        "directive": directive_id,
        "directive_title": title,
        "status": status_val,
        "delta": delta,
        "pre_metric": pre_metric,
        "post_metric": post_metric,
        "changes_applied": len(files_changed),
        "error": "",
        "layer": layer,
        "synced": True,
        "note": note,
    }

    cycle_path = cycle_dir / f"{cycle_id}.json"
    content = note or title

    kg = KnowledgeGraph(cfg.kg_path)
    try:
        if not force:
            existing_node = kg.get_node(directive_id)
            if existing_node and existing_node.get("status") == "COMPLETED":
                console.print(
                    f"[yellow]Warning: {directive_id} already exists in KG as COMPLETED. "
                    f"Use --force to update.[/yellow]"
                )
                return

        cycle_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        kg.ingest_cycle_outcome(
            directive_id=directive_id,
            directive_title=title,
            directive_content=content,
            status=status_val,
            files_changed=files_changed,
            metric_name=cfg.goals.primary_metric or "",
            metric_before=pre_metric,
            metric_after=post_metric,
            failing_tests=[],
            failure_detail=None,
            layer=layer,
        )
    finally:
        kg.close()

    mem_module.update(
        cfg.memory_dir,
        cfg.project.name,
        directive_id,
        title,
        status_val,
        delta,
        files_changed,
        pre_metric,
        post_metric,
        cfg.goals.primary_metric or "",
        directive_content=content,
        layer=layer,
    )

    console.print(f"[green]✓ Synced {directive_id} → {status_val}[/green]")
    console.print(f"[dim]  Cycle JSON: {cycle_path}[/dim]")
    console.print(f"[dim]  KG + memory updated.[/dim]")


@main.command()
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def memmap(target_dir: str):
    """Print the ASCII memory map for this project."""
    from .config import load_config
    from . import memory as mem_module

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)

    if not cfg.memory.enabled:
        console.print("[yellow]Memory is disabled in metaharness.toml ([memory] enabled = false)[/yellow]")
        return

    mem = mem_module.load(cfg.memory_dir)
    if mem.total_cycles == 0:
        console.print("[dim]No memory yet — run at least one cycle first.[/dim]")
        return

    console.print(mem_module.render_map(mem))


@main.command()
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option("--reset", is_flag=True, help="Wipe memory (irreversible)")
def memory(target_dir: str, reset: bool):
    """Show compact memory context (what the diagnoser/proposer see) or reset memory."""
    from .config import load_config
    from . import memory as mem_module

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)

    if not cfg.memory.enabled:
        console.print("[yellow]Memory disabled.[/yellow]")
        return

    if reset:
        mem_path = cfg.memory_dir / "project_memory.json"
        if mem_path.exists():
            mem_path.unlink()
        console.print("[red]Memory wiped.[/red]")
        return

    mem = mem_module.load(cfg.memory_dir)
    kg = mem_module.get_kg(cfg)
    ctx = mem_module.compact_context(mem, cfg=cfg, kg=kg)
    if not ctx:
        console.print("[dim]No memory yet.[/dim]")
    else:
        console.print("[bold]Compact context injected into Cursor Agent prompts:[/bold]\n")
        console.print(ctx)
        tokens_est = len(ctx.split())
        console.print(f"\n[dim]~{tokens_est} tokens[/dim]")


@main.command()
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option("--cycle", "cycle_id", default="", help="Specific cycle ID (default: latest)")
@click.option("--phase", type=click.Choice(["1", "2", "all"]), default="all")
def reasoning(target_dir: str, cycle_id: str, phase: str):
    """Show the agent reasoning log for a cycle (single yolo phase; legacy multi-phase supported)."""
    from .config import load_config

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    reasoning_roots = [
        cfg.maintenance_reasoning_dir,
        cfg.product_reasoning_dir,
        cfg.reasoning_dir,
    ]

    def _all_reasoning_logs(pattern: str) -> list[Path]:
        out: list[Path] = []
        for rd in reasoning_roots:
            if rd.is_dir():
                out.extend(rd.glob(pattern))
        return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)

    if not any(rd.is_dir() and list(rd.glob("*.md")) for rd in reasoning_roots):
        console.print("[dim]No reasoning logs yet. Logs are created during agent cycles.[/dim]")
        return

    if not cycle_id:
        # Prefer current single-phase log; fall back to legacy ANALYZE / EXECUTE names
        for pattern in ("*_execute.md", "*_2_execute.md", "*_1_analyze.md", "*_1_observe.md"):
            all_logs = _all_reasoning_logs(pattern)
            if all_logs:
                break
        else:
            console.print("[dim]No reasoning logs found.[/dim]")
            return
        stem = all_logs[0].stem
        for suffix in ("_execute", "_2_execute", "_1_analyze", "_1_observe"):
            if stem.endswith(suffix):
                cycle_id = stem[: -len(suffix)]
                break
        else:
            cycle_id = stem
        console.print(f"[dim]Showing latest cycle: {cycle_id}[/dim]\n")

    # phase "1" = primary agent log; "2" = legacy EXECUTE-only; "all" = ordered list
    phases_to_show = {
        "1": [("execute", "Agent — implementation (yolo)")],
        "2": [("2_execute", "EXECUTE — Implementation (legacy)")],
        "all": [
            ("execute", "Agent — implementation (yolo)"),
            ("1_analyze", "ANALYZE — Architecture + plan (legacy)"),
            ("2_execute", "EXECUTE — Implementation (legacy)"),
        ],
    }[phase]

    _reasoning_suffix_alts: dict[str, list[str]] = {
        "execute": ["execute", "2_execute", "3_execute"],
        "1_analyze": ["1_analyze", "1_observe"],
        "2_execute": ["2_execute", "3_execute"],
    }

    for suffix, title in phases_to_show:
        alts = _reasoning_suffix_alts.get(suffix, [suffix])
        path = None
        for rd in reasoning_roots:
            if not rd.is_dir():
                continue
            for a in alts:
                cand = rd / f"{cycle_id}_{a}.md"
                if cand.is_file():
                    path = cand
                    break
            if path is not None:
                break
        if path and path.exists():
            console.rule(f"[bold cyan]Phase: {title}[/bold cyan]")
            console.print(path.read_text(encoding="utf-8")[:8000])
        else:
            console.print(f"[dim]{title}: not found ({cycle_id}_{suffix}.md)[/dim]")


# ── graph (knowledge graph) ───────────────────────────────────────────────────


@main.group()
def graph():
    """Query the SQLite knowledge graph (directives, files, metrics, causal edges)."""
    pass


@graph.command("search")
@click.argument("query")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option("-n", default=20, help="Max results")
def graph_search(query: str, target_dir: str, n: int):
    """Full-text search over graph node names and summaries."""
    from .config import load_config
    from .knowledge_graph import KnowledgeGraph

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    kg = KnowledgeGraph(cfg.kg_path)
    ids = kg.search(query, limit=n)
    if not ids:
        console.print("[dim]No matches.[/dim]")
        return
    for nid in ids:
        node = kg.get_node(nid)
        if node:
            console.print(
                f"[bold]{nid}[/bold]  ({node['type']}) {node.get('name', '')}"
            )


@graph.command("node")
@click.argument("node_id")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def graph_node(node_id: str, target_dir: str):
    """Show one node by id."""
    from .config import load_config
    from .knowledge_graph import KnowledgeGraph

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    kg = KnowledgeGraph(cfg.kg_path)
    node = kg.get_node(node_id)
    if not node:
        console.print(f"[red]Node not found: {node_id}[/red]")
        raise SystemExit(1)
    console.print_json(data=node)


@graph.command("history")
@click.option("--file", "file_path", default=None, help="File path (as tracked on modified edges)")
@click.option("--entity", "entity_id", default=None, help="Entity node id (e.g. entity:config_key:foo)")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def graph_history(file_path: str, entity_id: str, target_dir: str):
    """Show file modification history or entity value history."""
    from .config import load_config
    from .knowledge_graph import KnowledgeGraph

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    kg = KnowledgeGraph(cfg.kg_path)
    if file_path:
        h = kg.file_history(file_path)
        console.print_json(data=h)
    elif entity_id:
        h = kg.entity_history(entity_id)
        console.print_json(data=h)
    else:
        console.print("[yellow]Specify --file or --entity[/yellow]")
        raise SystemExit(1)


@graph.command("stats")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def graph_stats(target_dir: str):
    """Node/edge counts and type breakdown."""
    from .config import load_config
    from .knowledge_graph import KnowledgeGraph

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    kg = KnowledgeGraph(cfg.kg_path)
    console.print_json(data=kg.stats())


@graph.command("sync-git")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print how many commits would be ingested without writing the KG.",
)
@click.option(
    "--full",
    is_flag=True,
    help="Backfill first-parent history (cap with --max-commits / --since).",
)
@click.option(
    "--max-commits",
    type=int,
    default=None,
    help="Max commits to walk/ingest in this run.",
)
@click.option(
    "--since",
    type=int,
    default=None,
    help="Synonym cap for newest commits to process (combined with --max-commits as min).",
)
def graph_sync_git(
    target_dir: str,
    dry_run: bool,
    full: bool,
    max_commits: int | None,
    since: int | None,
):
    """Record git commits and touched paths in the knowledge graph (cursor stored in KG)."""
    from .config import load_config
    from .git_kg_sync import sync_git_to_kg

    root = _find_project_root(Path(target_dir).resolve())
    if not (root / "metaharness.toml").exists():
        console.print(
            "[red]No metaharness.toml found. Run [bold]metaharness init[/bold] first.[/red]"
        )
        raise SystemExit(1)

    cfg = load_config(root)
    try:
        r = sync_git_to_kg(
            cfg,
            dry_run=dry_run,
            full=full,
            max_commits=max_commits,
            since=since,
        )
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)

    if r.warning:
        console.print(f"[yellow]{r.warning}[/yellow]")
    if r.initialized_cursor:
        console.print(
            "[dim]Initialized git sync cursor to HEAD (no history ingested). "
            "Use [bold]--full[/bold] for backfill.[/dim]"
        )
    elif dry_run:
        console.print(f"[dim]Dry-run: would ingest {r.commits_processed} commit(s).[/dim]")
    else:
        console.print(f"[green]Ingested {r.commits_processed} commit(s).[/green]")
    if r.cursor_sha:
        console.print(f"[dim]Cursor SHA: {r.cursor_sha[:12]}…[/dim]")


# ── vision (KG-backed product vision) ───────────────────────────────────────────


@main.group()
def vision():
    """Vision management commands."""
    pass


@vision.command("show")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def vision_show(target_dir: str):
    """Show the current evolved vision from the knowledge graph."""
    from .config import load_config
    from . import vision as vision_mod
    from .memory import get_kg

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    kg = get_kg(cfg)
    try:
        v = vision_mod.load_vision(kg)
        if not v:
            console.print("[dim]No vision in KG yet. Run a product cycle first.[/dim]")
            return
        console.print(vision_mod.vision_prompt_block(kg, cfg))
        console.print(f"\n[dim]Evolution count: {v.get('evolution_count', 0)}[/dim]")
        console.print(f"[dim]Last evolved: {v.get('last_evolved_at', 'never')}[/dim]")
    finally:
        kg.close()


@vision.command("evolve")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def vision_evolve(target_dir: str):
    """Manually trigger a vision evolution pass."""
    from .config import load_config
    from . import vision as vision_mod
    from .memory import get_kg

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    kg = get_kg(cfg)
    try:
        vision_mod.seed_vision(kg, cfg)
        updated = vision_mod.evolve_vision(kg, cfg)
        console.print(f"[green]Vision evolved. Count: {updated.get('evolution_count')}[/green]")
        console.print(f"[dim]Features wanted: {len(updated.get('features_wanted', []))}[/dim]")
        console.print(f"[dim]Features done (derived): {len(vision_mod.derive_features_done(kg))}[/dim]")
    finally:
        kg.close()


# ── evidence ────────────────────────────────────────────────────────────────────


@main.group()
def product():
    """Product agent (vision-driven feature directives)."""
    pass


@product.command("run")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def product_run(target_dir: str):
    """Run a single product agent cycle."""
    from .config import load_config
    from .product_agent import run_product_cycle

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    if not cfg.product.enabled:
        console.print(
            "[yellow]Product agent disabled. Set [product] enabled = true in metaharness.toml[/yellow]"
        )
        raise SystemExit(1)
    outcome = run_product_cycle(cfg)
    raise SystemExit(0 if outcome.status.value == "COMPLETED" else 1)


@product.command("status")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option("-n", default=10, help="Number of cycles to show")
def product_status(target_dir: str, n: int):
    """Show recent product agent cycle history."""
    from .config import load_config

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    cdir = cfg.product_cycles_dir
    logs = sorted(cdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    if not logs:
        console.print("[dim]No product cycles recorded yet.[/dim]")
        return
    for p in reversed(logs):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            ts = data.get("timestamp", "?")[:19].replace("T", " ")
            dc = data.get("directive_confidence")
            conf = ""
            if dc is not None:
                try:
                    pct = int(round(float(dc) * 100))
                    det = str(data.get("directive_confidence_detail") or "")
                    m = re.search(r"\((high|medium|low)\)", det)
                    tier = m.group(1) if m else ""
                    conf = f" {pct}%({tier})" if tier else f" {pct}%"
                except (TypeError, ValueError):
                    conf = ""
            console.print(
                f"[dim]{ts}[/dim]  {data.get('directive')}  {data.get('status')}{conf}  "
                f"{data.get('directive_title', '')[:60]}"
            )
        except Exception:
            pass


@product.command("roadmap")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def product_roadmap(target_dir: str):
    """Show product-layer directives from the knowledge graph."""
    from .config import load_config
    from .knowledge_graph import KnowledgeGraph, build_cross_layer_context

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    kg = KnowledgeGraph(cfg.kg_path)
    nodes = kg.get_nodes_by_layer("product", limit=20)
    if not nodes:
        console.print("[dim]No product-layer nodes in the knowledge graph yet.[/dim]")
        return
    console.print("[bold]Product (KG)[/bold]")
    for n in nodes:
        t = (n["data"].get("title") or n["name"] or n["id"]).strip()
        console.print(f"  • [cyan]{n['id']}[/cyan] {t}  ({n['status']})")
    console.print()
    x = build_cross_layer_context(kg, "product")
    if x.strip():
        console.print("[bold]Maintenance context[/bold]")
        console.print(x)


@main.command("dashboard")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option(
    "--host",
    default=DASHBOARD_DEFAULT_HOST,
    show_default=True,
    help="Bind address (0.0.0.0 for all interfaces)",
)
@click.option("--port", default=8765, show_default=True, type=int, help="HTTP port")
def dashboard_cmd(target_dir: str, host: str, port: int):
    """Start a read-only local web UI for cycles, metrics, and KG/memory paths."""
    from .config import load_config
    from . import dashboard as dash

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    if not (root / "metaharness.toml").exists():
        console.print("[red]No metaharness.toml found. Run [bold]metaharness init[/bold] first.[/red]")
        raise SystemExit(1)
    url = f"http://{host}:{port}/"
    console.print(f"[green]Meta-Harness dashboard[/green] — [bold]{url}[/bold]")
    console.print("[dim]Read-only data; no authentication (local operator tool). Ctrl+C to stop.[/dim]")
    try:
        dash.run_dashboard_server(cfg, host=host, port=port)
    except OSError as e:
        console.print(f"[red]Could not bind {host}:{port}: {e}[/red]")
        raise SystemExit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


@main.command("evidence")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
@click.option("--max-chars", default=12000, help="Truncate assembled prompt sections")
def evidence_cmd(target_dir: str, max_chars: int):
    """Collect evidence and print prompt-oriented sections (logs, metrics, tests, AST)."""
    from . import evidence as ev_mod
    from .config import load_config

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    ev = ev_mod.collect(cfg)
    console.print(ev_mod.to_prompt_sections(ev, max_chars=max_chars))


# ── slack ──────────────────────────────────────────────────────────────────────


@main.group()
def slack():
    """Slack controls (configure [slack] in metaharness.toml — one app per project)."""
    pass


@slack.command("listen")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def slack_listen(target_dir: str):
    """Start Slack Socket Mode listener (foreground, for testing)."""
    from .config import load_config
    from . import slack_integration as si

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    if not (root / "metaharness.toml").exists():
        console.print("[red]No metaharness.toml found.[/red]")
        raise SystemExit(1)
    if not cfg.slack.enabled:
        console.print("[yellow]Slack disabled.[/yellow]")
        return
    console.print("[cyan]Starting Slack Socket Mode (CTRL-C to stop)...[/cyan]")
    try:
        si.run_socket_mode(cfg)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass


@main.group()
def research():
    """Research paper ingestion commands."""
    pass


@research.command("eval")
@click.argument("url")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def research_eval(url: str, target_dir: str):
    """Fetch and evaluate a research paper for implementation relevance."""
    from . import research as research_mod
    from .config import load_config

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)

    console.print(f"[cyan]Fetching {url}...[/cyan]")
    paper = research_mod.fetch_paper(url)

    if paper.fetch_error:
        console.print(f"[red]Fetch failed: {paper.fetch_error}[/red]")
        return

    console.print(f"[dim]Title: {paper.title}[/dim]")
    ab = paper.abstract[:200] if paper.abstract else ""
    console.print(f"[dim]Abstract: {ab}...[/dim]")
    console.print("[cyan]Evaluating...[/cyan]")
    ev = research_mod.evaluate_paper(cfg, paper)

    color = {"implement": "green", "monitor": "yellow", "discard": "red"}.get(
        ev.recommendation, "white"
    )
    console.print(f"[{color}]Recommendation: {ev.recommendation.upper()}[/{color}]")
    console.print(f"Confidence: {ev.confidence:.0%}")
    console.print(f"Applicable to: {ev.applicable_to}")
    console.print(f"Difficulty: {ev.implementation_difficulty}")
    console.print(f"Impact: {ev.expected_impact}")
    console.print(f"Reason: {ev.reason}")

    if ev.recommendation == "implement":
        if research_mod.queue_paper(cfg, ev):
            from .slack_integration import notify_research_queue_from_evaluation

            notify_research_queue_from_evaluation(cfg, ev)
            console.print("[green]✓ Added to product agent queue[/green]")
        else:
            console.print(
                "[yellow]Could not add to queue (full — drop a monitor item or clear first).[/yellow]"
            )


@research.command("queue")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def research_queue(target_dir: str):
    """Show the current research implementation queue."""
    from . import research as research_mod
    from .config import load_config

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    queue = research_mod.get_queue(cfg)
    if not queue:
        console.print("[dim]Queue is empty.[/dim]")
        return
    for item in queue:
        t = str(item.get("title", "") or "")
        console.print(f"  • {t[:60]}")
        console.print(f"    → {item.get('applicable_to', '')} | {item.get('difficulty', '')}")


@research.command("clear")
@click.argument("url")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def research_clear(url: str, target_dir: str):
    """Remove a paper from the implementation queue."""
    from . import research as research_mod
    from .config import load_config

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    removed = research_mod.clear_queue_item(cfg, url)
    if removed:
        console.print("[green]✓ Removed from queue[/green]")
    else:
        console.print("[yellow]URL not found in queue[/yellow]")


@slack.command("test")
@click.option("--dir", "target_dir", default=".", help="Project root directory")
def slack_test_cmd(target_dir: str):
    """Post a test message to verify Slack integration."""
    from .config import load_config
    from . import slack_integration as si

    root = _find_project_root(Path(target_dir).resolve())
    cfg = load_config(root)
    if not cfg.slack.enabled:
        console.print(
            "[yellow]Slack is disabled. Set [[slack]] enabled = true in metaharness.toml[/yellow]"
        )
        return
    try:
        si.slack_test(cfg)
        console.print("[green]✓ Slack test message sent.[/green]")
    except Exception as e:
        console.print(f"[red]Slack test failed: {e}[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
