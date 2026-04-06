"""
scripts/kg_sync_manual.py

One-off KG + memory sync for two human-verified completions:

  1. P003_auto  — Dashboard memmap/KG snapshot view
                  Was TEST_FAILED. All 10 tests now pass after P005 fixed
                  the coverage infrastructure. Close as COMPLETED.

  2. research_agent — Research paper ingestion pipeline (human-issued directive)
                  New file: research.py. Modified: config.py,
                  slack_integration.py, product_agent.py, cli.py,
                  pyproject.toml, tests/test_research.py.
                  18 new tests, 276 total passed. Close as COMPLETED.

Run from repo root:
    py scripts\\kg_sync_manual.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on the path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meta_harness.config import load_config
from meta_harness.knowledge_graph import KnowledgeGraph
from meta_harness import memory as mem_mod

cfg = load_config(ROOT)
kg = KnowledgeGraph(cfg.kg_path)
mem = mem_mod.load(cfg.memory_dir)

NOW_METRIC = 1.0  # test_pass_rate held at 1.0 throughout

# ── 1. Close P003_auto ────────────────────────────────────────────────────────
print("Syncing P003_auto → COMPLETED ...")

P003_FILES = [
    "dashboard.py",
    "tests/test_dashboard.py",
]

P003_CONTENT = (
    "Fix dashboard.py: memmap panel and KG snapshot view. "
    "All 10 tests in tests/test_dashboard.py now pass after P005 "
    "coverage infrastructure work. Verified: py -m pytest tests/test_dashboard.py -v"
)

kg.ingest_cycle_outcome(
    directive_id="P003_auto",
    directive_title="Dashboard memmap panel and KG snapshot view",
    directive_content=P003_CONTENT,
    status="COMPLETED",
    files_changed=P003_FILES,
    metric_name="test_pass_rate",
    metric_before=1.0,
    metric_after=1.0,
    failing_tests=[],
    failure_detail=None,
    layer="product",
)

mem_mod.update(
    cfg.memory_dir,
    cfg.project.name,
    "P003_auto",
    "Dashboard memmap panel and KG snapshot view",
    "COMPLETED",
    0.0,
    P003_FILES,
    1.0,
    1.0,
    cfg.goals.primary_metric,
    kg=kg,
    directive_content=P003_CONTENT,
    layer="product",
)
print("  ✓ P003_auto closed as COMPLETED")


# ── 2. Record research agent as COMPLETED ─────────────────────────────────────
print("Syncing research_agent → COMPLETED ...")

RESEARCH_FILES = [
    "research.py",
    "config.py",
    "slack_integration.py",
    "product_agent.py",
    "cli.py",
    "pyproject.toml",
    "tests/test_research.py",
]

RESEARCH_CONTENT = (
    "Research paper ingestion pipeline. Human-issued directive. "
    "New module research.py: fetch_paper (arxiv/pdf/html), evaluate_paper via "
    "cursor_client.json_call plan mode, queue_paper, get_queue, clear_queue_item, "
    "format_slack_verdict. Queue at .metaharness/research/queue.json, cap 10 items. "
    "config.py: research_dir, research_queue_path derived paths. "
    "slack_integration.py: /metaharness research <url> with background thread, "
    "research queue/discard subcommands. "
    "product_agent.py: queue injection in diagnose + propose prompts. "
    "cli.py: research eval|queue|clear command group. "
    "pyproject.toml: httpx>=0.27.0, pypdf>=4.0.0. "
    "18 new tests, 276 total passed, 2 skipped. "
    "End-to-end verified: CLI eval, Slack /metaharness research, correct DISCARD on arxiv:1706.03762."
)

kg.ingest_cycle_outcome(
    directive_id="research_agent",
    directive_title="Research paper ingestion pipeline via Slack and CLI",
    directive_content=RESEARCH_CONTENT,
    status="COMPLETED",
    files_changed=RESEARCH_FILES,
    metric_name="test_pass_rate",
    metric_before=1.0,
    metric_after=1.0,
    failing_tests=[],
    failure_detail=None,
    layer="product",
)

mem_mod.update(
    cfg.memory_dir,
    cfg.project.name,
    "research_agent",
    "Research paper ingestion pipeline via Slack and CLI",
    "COMPLETED",
    0.0,
    RESEARCH_FILES,
    1.0,
    1.0,
    cfg.goals.primary_metric,
    kg=kg,
    directive_content=RESEARCH_CONTENT,
    layer="product",
)
print("  ✓ research_agent recorded as COMPLETED")


# ── Summary ───────────────────────────────────────────────────────────────────
kg.close()
stats = mem_mod.load(cfg.memory_dir)
print(f"\nMemory: {stats.total_cycles} total cycles, "
      f"{stats.completed} completed, "
      f"{stats.failed} failed, "
      f"{stats.vetoed} vetoed")
print("Done.")