# Meta-Harness

**A self-improving outer loop for any project.** Meta-Harness wraps a codebase with an autonomous **maintenance cycle** that collects evidence, diagnoses behavior with the **Cursor Agent CLI** (default model **`composer-2`**), proposes a single scoped **directive**, enforces an optional **veto window**, applies changes only inside declared **`[scope]`** globs, runs tests (and optional **rollback**), and records outcomes in **SQLite memory**, a **knowledge graph**, and cycle JSON logs.

This repository is the **reference implementation**: a flat Python package (`meta_harness`) plus the `metaharness` CLI. Runtime state for each deployed project lives under **`.metaharness/`** next to that project’s `metaharness.toml` (not inside the package).

---

## Table of contents

1. [What it does (one cycle)](#what-it-does-one-cycle)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Quick start](#quick-start)
5. [Repository layout and architecture](#repository-layout-and-architecture)
6. [Configuration (`metaharness.toml`)](#configuration-metaharnesstoml)
7. [CLI reference](#cli-reference)
8. [Runtime artifacts (`.metaharness/`)](#runtime-artifacts-metaharness)
9. [Knowledge graph and memory](#knowledge-graph-and-memory)
10. [Maintenance vs product agent](#maintenance-vs-product-agent)
11. [Multi-project daemon](#multi-project-daemon)
12. [Slack integration](#slack-integration)
13. [Research papers and queue](#research-papers-and-queue)
14. [Dashboard](#dashboard)
15. [Git → knowledge graph sync](#git--knowledge-graph-sync)
16. [Human work sync (`metaharness sync`)](#human-work-sync-metaharness-sync)
17. [Rollback](#rollback)
18. [Self-hosting this repo](#self-hosting-this-repo)
19. [Testing and development](#testing-and-development)
20. [Environment variables and operator files](#environment-variables-and-operator-files)
21. [Troubleshooting](#troubleshooting)
22. [Scope, safety, and protected files](#scope-safety-and-protected-files)
23. [Further documentation](#further-documentation)
24. [Origin](#origin)

---

## What it does (one cycle)

End-to-end flow (see `cycle.py`, `agent.py`, `diagnoser.py`, `proposer.py`):

1. **Evidence** — Logs, metrics JSON, git context, optional AST/coverage/deps (`evidence.py`). Minimum sessions are gated by `[cycle]` / `[maintenance]` `min_evidence_items`.
2. **Diagnose** — Cursor Agent returns structured JSON (`diagnoser.py` + `cursor_client.json_call`). Prompts expect a **fenced `json` code block** when models return markdown; parsing is resilient.
3. **Propose** — One markdown directive saved under `.metaharness/directives/` (`proposer.py`). IDs look like `M042_auto` (maintenance) or `P014_auto` (product), depending on layer.
4. **Veto window** — While `.metaharness/PENDING_VETO` exists, the run can be aborted by **deleting that file**. Optional Slack Block Kit buttons and **`SLACK_EARLY_APPROVE`** files skip the wait.
5. **Implement** — Scoped **ANALYZE → EXECUTE** (yolo) path: relevance-filtered files from `scope.modifiable`, directive mentions, and `git_recent_paths`; optional cap `[cursor] max_files_per_cycle` (`agent.py`).
6. **Test** — Shell command from `[test]`; optional junit XML for metrics.
7. **Restart** — `[run].command` after success (often `none` or `pip install -e .` for libraries).
8. **Metric delta** — Optional comparison on `[goals].primary_metric` vs `optimization_direction` using `metrics.json` (via `MetricsBundle.current`, not dict access on the bundle).
9. **Log + memory + KG** — Per-cycle JSON under `.metaharness/cycles/`; `memory.py` and `knowledge_graph.py` ingest outcomes.

**Daemon** (`daemon.py`): repeats maintenance cycles using **`interval_seconds`** or **`schedule`** (local `HH:MM`); honors **`DAEMON_PAUSE`**; optional **Slack Socket Mode** thread when configured. **`catch_up`** (default `false`): first run waits for the next scheduled slot instead of running immediately.

---

## Prerequisites

- **Python ≥ 3.10**
- **[Cursor Agent CLI](https://cursor.com/install)** — `agent` on `PATH` (or `agent_bin` full path). Authenticate with `agent login` or set **`CURSOR_API_KEY`** (see `agent --help`).
- **Optional:** `[slack]` extras — `pip install "meta-harness[slack]"` or install `slack-bolt` / `slack-sdk` for Slack features.

Agent work is **always** via the Cursor CLI (e.g. `composer-2`), not LM Studio or other local servers unless you change the harness to use them.

---

## Installation

### From PyPI

```bash
pip install meta-harness
# Optional Slack:
pip install "meta-harness[slack]"
```

### From this repository (editable)

The package uses a **flat layout**: Python modules live at the repo root but install as `meta_harness` (`pyproject.toml` maps `meta_harness` → `.`).

```bash
cd META_HARNESS
pip install -e .
# Dev dependencies (pytest, coverage):
pip install -e ".[dev]"
```

Entry point: **`metaharness`** → `meta_harness.cli:main`.

---

## Quick start

```bash
cd my-project
metaharness init
# Edit metaharness.toml — project name, scope, test command, goals
metaharness run --once
metaharness status
# Continuous runs (after setting interval_seconds or schedule in [cycle])
metaharness daemon
```

---

## Repository layout and architecture

### Installed package (`meta_harness`)

| Module | Role |
|--------|------|
| `config.py` | Load `metaharness.toml`; derived paths under `.metaharness/` |
| `evidence.py` | Evidence bundle for diagnosis (metrics, tests, git, optional AST/coverage) |
| `diagnoser.py` | LLM diagnosis → structured `Diagnosis` |
| `proposer.py` | LLM directive → `Directive` + saved `.md` |
| `cursor_client.py` | Subprocess to `agent`; UTF-8 decode; `--trust`; JSON extraction + retries |
| `agent.py` | Scoped ANALYZE/EXECUTE to disk; file mutex (`filelock`) |
| `cycle.py` | One full maintenance cycle; veto; Slack hooks; rollback hooks |
| `rollback.py` | Optional `git` restore of agent-touched paths |
| `daemon.py` | Scheduled/interval loop; pause; multi-project orchestration |
| `multi_project.py` | `metaharness-projects.toml` registry |
| `memory.py` | `project_memory.json`, compact context, ASCII map, pattern inference |
| `knowledge_graph.py` | SQLite KG (directives, files, metrics, edges, FTS) |
| `dashboard.py` | Local HTTP UI for cycles, metrics, KG snapshot, memmap |
| `slack_integration.py` | Posts, Block Kit, slash commands, Socket Mode |
| `product_agent.py` | Vision-driven product cycles |
| `vision.py` | KG-backed vision statement evolution |
| `research.py` | Paper fetch/eval/queue for product context |
| `git_kg_sync.py` | Ingest git commits into KG |
| `directive_confidence.py` | Directive confidence scoring |
| `coverage_policy.py` | Canonical pytest-cov fragments for this flat layout |
| `cli.py` | Click CLI |

### Top-level repo files (not only `meta_harness/` subfolder)

Sources are at the **repository root** next to `pyproject.toml` (flat layout). **`tests/`** is not a Python package, so `from tests.conftest` does not work. Pytest loads **`conftest.py`** at the repo root (coverage / `sys.modules` hygiene) and **`tests/conftest.py`** (registers the flat `meta_harness` package for pytest-cov). Import shared test utilities accordingly (see `tests/test_conftest_coverage_registration.py` for loading `tests/conftest.py` explicitly when needed).

### Templates and scripts

| Path | Purpose |
|------|---------|
| `templates/metaharness.toml` | Default config for `metaharness init` |
| `templates/metaharness-projects.toml` | Multi-project registry starter |
| `templates/git-hook-post-commit.toml` | Optional post-commit hook example for `graph sync-git` |
| `scripts/generate_metrics.py` | Builds `metrics.json` from junit + `coverage.xml` |
| `scripts/run_cycle.bat` / `scripts/run_cycle.sh` | Self-host: tests → metrics → `metaharness run` |
| `scripts/kg_sync_manual.py`, `scripts/fix_memory_duplicates.py`, … | Maintenance utilities |

### Tests

The suite is large (hundreds of tests; run `py -3 -m pytest tests/ --collect-only -q` for the current count). **`tests/conftest.py`** clears premature `meta_harness` imports so **pytest-cov** traces correctly.

---

## Configuration (`metaharness.toml`)

The CLI walks up from `--dir` until it finds **`metaharness.toml`**.

### Core sections

- **`[project]`** — `name`, `description` (used in LLM prompts).
- **`[run]`** — `command`, `working_dir`, `settle_seconds` (post-change wait before evidence).
- **`[test]`** — `command`, `timeout_seconds`, `junit_xml` (if `true`, cycle may append junit flags — avoid duplicating `--junitxml` in `[test].command` when `junit_xml` is true; this repo’s self-host sets `junit_xml = false` because the command already includes junit).
- **`[evidence]`** — `log_patterns`, `metrics_patterns`, `max_age_hours`, `max_log_lines`, toggles for AST/git/coverage/deps collection.
- **`[scope]`** — `modifiable` and `protected` glob lists; agent only writes under modifiable and never under protected.
- **`[cycle]`** or **`[maintenance]`** — `interval_seconds`, `schedule` (`HH:MM` local), `veto_seconds`, `min_evidence_items`, `max_directive_history`, `rollback_*`, `catch_up`.
- **`[cursor]`** — `agent_bin`, `model`, `model_fast`, `agent_timeout` / `timeout_seconds`, `json_timeout`, `json_retries`, `max_files_per_cycle`.
- **`[memory]`** — `enabled`, `backend` (e.g. `graph`), `pattern_refresh_every`, `max_context_nodes`.
- **`[goals]`** — `objectives`, `primary_metric`, `optimization_direction`.
- **`[slack]`** — `enabled`, token env names, `channel`, `slash_command`, Socket Mode flags, posting toggles.
- **`[product]`** — Product agent: `enabled`, `schedule`, `veto_seconds`, `slack_channel`, `modifiable` / `protected`, `catch_up`.
- **`[vision]`** — Long-form vision, features wanted/done, north-star metric (feeds product agent / KG).

See **`templates/metaharness.toml`** for commented defaults. This repo’s root **`metaharness.toml`** is the authoritative **self-host** configuration.

---

## CLI reference

All commands accept **`--dir`** (project root; default `.`) unless noted. Config discovery walks upward for `metaharness.toml`.

| Command | Purpose |
|---------|---------|
| **`metaharness init`** | Scaffold `metaharness.toml`, `.metaharness/` dirs, `.gitignore` entry |
| **`metaharness run`** | Single maintenance cycle (default `--once`) |
| **`metaharness daemon`** | Loop: maintenance cycles. **`--projects-file`** for multi-project. **`--git-kg-sync`** or **`METAHARNESS_GIT_KG_SYNC=1`** runs git→KG after each maintenance cycle |
| **`metaharness status`** | Recent cycle table; **`--projects-file`** shows multi-project registry |
| **`metaharness sync`** | Record a human-completed directive in KG + memory + cycle JSON (`--id`, `--title`, `--status`, `--layer`, `--files`, metrics, `--force`) |
| **`metaharness memory`** | Print compact memory context; **`--reset`** wipes |
| **`metaharness memmap`** | ASCII memory map |
| **`metaharness reasoning`** | Show agent reasoning logs for a cycle (`--cycle`, `--phase`) |
| **`metaharness graph search`** | FTS search on KG |
| **`metaharness graph node`** | Dump one node by id |
| **`metaharness graph history`** | File or entity history (`--file` / `--entity`) |
| **`metaharness graph stats`** | Node/edge counts |
| **`metaharness graph sync-git`** | Ingest git commits into KG (`--dry-run`, `--full`, `--max-commits`, `--since`) |
| **`metaharness vision show`** | Show evolved vision from KG |
| **`metaharness vision evolve`** | Manually evolve vision |
| **`metaharness product run`** | One product cycle |
| **`metaharness product status`** | Product cycle history |
| **`metaharness product roadmap`** | Product-layer KG nodes + cross-layer context |
| **`metaharness dashboard`** | Local web UI (`--host`, default port **8765**) |
| **`metaharness evidence`** | Print collected evidence sections (debugging prompts) |
| **`metaharness slack listen`** | Foreground Socket Mode listener |
| **`metaharness slack test`** | Post a test message |
| **`metaharness research eval <url>`** | Fetch + evaluate paper; may queue for product agent |
| **`metaharness research queue`** | Show implementation queue |
| **`metaharness research clear <url>`** | Remove from queue |

Run **`metaharness --help`** and **`metaharness <cmd> --help`** for options.

---

## Runtime artifacts (`.metaharness/`)

Created by `metaharness init` and cycles. **Do not commit** (add to `.gitignore`).

| Artifact | Purpose |
|----------|---------|
| `directives/` | Generated directive `.md` files |
| `cycles/maintenance/`, `cycles/product/` | Per-cycle JSON logs |
| `reasoning/maintenance/`, `reasoning/product/`, `reasoning/` | Agent reasoning markdown |
| `prompts/` | Saved prompts for debugging |
| `PENDING_VETO` | Present during veto window — delete to abort |
| `SLACK_EARLY_APPROVE`, `SLACK_EARLY_APPROVE_PRODUCT` | Early proceed markers |
| `DAEMON_PAUSE` | Pause daemon for this project |
| `knowledge_graph.db` | SQLite KG (default path) |
| `memory/` | Memory JSON backend files |
| `last_cursor_failure.txt` | Last Cursor CLI failure context (bounded UTF-8) |

---

## Knowledge graph and memory

- **`knowledge_graph.py`** — SQLite store: directives, files touched, metrics, causal edges; **FTS** search via `graph search`. Cycle outcomes are ingested automatically.
- **`memory.py`** — Rolling memory, compact prompt injection, **pattern inference** (extension-level patterns need enough samples). **`COMPLETED`** directives count as wins without requiring a positive metric delta in pattern logic.

**Important:** Work done **outside** harness cycles is not visible until you **`metaharness sync`** or otherwise update the KG, or the harness may repeat proposals.

---

## Maintenance vs product agent

- **Maintenance** — Default `metaharness run` / daemon loop: diagnostics, stability, tests, refactors within `[scope]`.
- **Product** — Optional **`[product]`** + **`metaharness product run`** or scheduled product cycles: vision-driven feature directives, separate cycle logs under product paths, often different `modifiable` globs (see config).

---

## Multi-project daemon

Optional **`metaharness-projects.toml`** lists multiple project roots (each with its own `metaharness.toml` and `.metaharness/`). The daemon runs **one full cycle per project in sequence**, then waits for the next round using **`[cycle]`** of the **first enabled** project.

- **Control-plane pause:** `DAEMON_PAUSE` next to the registry file pauses the whole round-robin.
- **Per-project pause:** each repo’s `.metaharness/DAEMON_PAUSE` still applies.
- **`product_project_id`** — If set, only that project runs the product background loop in multi-project mode.
- **Slack:** Socket autostart is typically **off** in multi-project mode (one process, multiple apps); use **`metaharness slack listen`** per need.

Details: **`docs/MULTI_PROJECT.md`**.

---

## Slack integration

Configure **`[slack]`** in `metaharness.toml`. Tokens via env vars (**`SLACK_BOT_TOKEN`**, **`SLACK_APP_TOKEN`**) or inline (not recommended). Features include veto posts, cycle outcome posts, slash commands (`status`, `memory`, `memmap`, `pause`, `resume`, `veto`, `proceed`, `product`, `research`, etc.), and Socket Mode.

**Bolt:** Handlers must **`ack()` immediately** before slow work. Handler signatures should only include parameters Bolt injects and that you use.

---

## Research papers and queue

**`metaharness research eval <url>`** fetches and evaluates a paper (arXiv, PDF, HTML). Recommendations may **`implement`**, **`monitor`**, or **`discard`**. When the model recommends **`implement`**, the harness calls **`queue_paper`**; only if that returns success (the queue is not full of non-droppable items) does it send the optional Slack “ready to implement” ping via **`notify_research_queue_from_evaluation`** — the same helper used by **`/metaharness research <url>`** after the slash command **acks** immediately. The ping is gated by **`[slack] notify_research_queue`** (default **true**); it is skipped for **`monitor`** / **`discard`**, when enqueue fails, or when Slack is disabled. A full verdict message (CLI: printed; Slack: posted after background work) is separate from that short ping. **`research queue`** / **`research clear`** manage the list.

---

## Dashboard

**`metaharness dashboard`** serves a **read-only** local web UI (default **http://127.0.0.1:8765** — bind with `--host 0.0.0.0` if needed). Shows maintenance/product cycles, metrics, KG snapshot, memory map, **`GET /health`** → `OK`. No authentication — treat as an **operator tool** on trusted networks only.

---

## Git → knowledge graph sync

**`metaharness graph sync-git`** records **first-parent** git history as `git_commit` nodes with edges to touched files; cursor position is stored in the KG. Use **`--full`** / **`--max-commits`** / **`--since`** for backfill. After each maintenance cycle, optional **`--git-kg-sync`** or **`METAHARNESS_GIT_KG_SYNC=1`** runs sync (failures logged, non-fatal).

---

## Human work sync (`metaharness sync`)

If you implement work **without** a harness cycle, run **`metaharness sync`** so the KG and memory reflect reality. The command **warns and skips** if a conflicting daemon cycle or **COMPLETED** KG node exists unless **`--force`**.

---

## Rollback

When **`[cycle] rollback_enabled`** is true, the harness can restore agent-touched paths via **`rollback.py`** after **test failure** or **metric regression** (depending on flags), with optional **`rollback_require_git`** to avoid ambiguous worktrees.

---

## Self-hosting this repo

Use this checkout as the project root (already contains `metaharness.toml`).

1. **Editable install:** `pip install -e ".[dev]"`.
2. **Tests + coverage + metrics** — Prefer **`scripts/run_cycle.bat`** (Windows) or **`scripts/run_cycle.sh`** (Unix) from the repo root. They run pytest with **`--cov=. --cov-config=.coveragerc`**, write junit + **`coverage.xml`**, then **`python scripts/generate_metrics.py`** → **`metrics.json`**.
3. **Cycle:** `metaharness run`.

**Coverage:** This repo needs **`pytest tests/ --cov=. --cov-config=.coveragerc`** so the flat layout is traced honestly. **`pytest --cov=meta_harness`** alone can mis-report. See **`.coveragerc`** and **`coverage_policy.py`**.

---

## Testing and development

```bash
py -3 -m pytest tests/ --cov=. --cov-config=.coveragerc --cov-report=term-missing -q
```

- **Mocking Cursor:** Patch **`meta_harness.<module>.cursor_client.json_call`** (or `agent_call`) on the **same submodule** the code under test imports — not a different import path — or the real CLI may run.
- **Mocking Slack:** Patch the module path **callers** resolve (e.g. **`meta_harness.slack_integration.socket_tokens_ready`**).
- **`META_HARNESS_DEBUG`** — Extra logging in `cursor_client` / `diagnoser` when set.

**`pyproject.toml`** is intentionally **protected** in default scopes — agent directives should not change packaging without explicit human approval.

---

## Environment variables and operator files

| Variable / file | Purpose |
|-----------------|--------|
| **`CURSOR_API_KEY`** | Cursor API auth for `agent` |
| **`SLACK_BOT_TOKEN`**, **`SLACK_APP_TOKEN`** | Slack (names configurable via `metaharness.toml`) |
| **`METAHARNESS_GIT_KG_SYNC`** | Non-empty truthy → daemon may run git→KG after maintenance cycle |
| **`META_HARNESS_DEBUG`** | Verbose harness logging |
| **`.env`** | Loaded from package directory when `python-dotenv` is available (see `config.py`) |
| **`.metaharness/PENDING_VETO`** | Delete to veto |
| **`.metaharness/DAEMON_PAUSE`** | Pause daemon |
| **`SLACK_EARLY_APPROVE`** | Skip veto wait (maintenance) |

---

## Troubleshooting

### `AGENT_FAILED` and `failure_detail` tags

| Prefix | Meaning |
|--------|---------|
| `[phase:analyze]` / `[phase:execute]` / `[phase:apply]` | Harness phase |
| `[agent_fail:cli_nonzero]` | `agent` exited non-zero |
| `[agent_fail:json_parse]` | No parseable JSON after retries |
| `[agent_fail:empty_stdout]` | No usable stdout |
| `[agent_fail:timeout]` | Subprocess timeout |
| `[agent_fail:agent_binary_missing]` | `agent` not found |

**`.metaharness/last_cursor_failure.txt`** holds the latest raw context. Evidence may include a **`cursor_cli_failure_excerpt`** for diagnosis.

### Windows

- Subprocess stdout is decoded as **UTF-8** with replacement to avoid ANSI code-page issues.
- Use **`--trust`** on `agent` invocations so non-interactive runs are not blocked by workspace trust prompts.

### Stale metrics

If pytest fails during collection, **`evidence.py`** can reconcile **`metrics.json`** against the pytest log tail so **`test_pass_rate`** is not misleadingly high.

---

## Scope, safety, and protected files

- Only **`[scope].modifiable`** globs are writable; **`protected`** always wins.
- Typical **`protected`**: `metaharness.toml`, **`pyproject.toml`**, `.git/**`, sometimes `scripts/**` and `.metaharness/**` in self-host config.
- Directives that reference **other repositories** must be executed in those repos — the harness only applies changes within the configured project root.

---

## Further documentation

| Doc | Contents |
|-----|----------|
| **`docs/PROJECT_OVERVIEW.md`** | Living architecture summary and **dated update log** — add an entry when you ship meaningful behavior changes |
| **`docs/MULTI_PROJECT.md`** | Multi-project registry and pause behavior |
| **`docs/ANIMAs_META_HARNESS_EXAMPLE.md`** | Historical two-layer example — **reference only**, not runtime |
| **`AGENTS.md`** (repo / Cursor rules) | Operator preferences and workspace facts for AI agents |

---

## Origin

Developed as the outer loop for **ANIMa** (Anima Machinae) at Ghost Animus LLC, generalized for any project.

Based on the Meta-Harness paper by Lee et al. (Stanford, MIT, KRAFTON, 2024): [https://yoonholee.com/meta-harness/](https://yoonholee.com/meta-harness/)
