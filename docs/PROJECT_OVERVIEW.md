# META_HARNESS — Project overview

This file is the **living summary** of the repository: purpose, architecture, and what has shipped. **Whenever you change behavior, add APIs, or ship a meaningful fix, append a row under [Update log](#update-log)** (newest first) with the date, a one-line title, and bullet details if needed.

---

## Goal

Ship a **portable, single-layer “meta harness”** that any codebase can adopt: the project **self-analyzes** (from evidence + LLM diagnosis), **self-adapts** (directives + agent-applied patches inside declared scope), **self-tests**, and **tracks outcomes** in local memory—while **keeping token/context use lean** (compact memory injection, bounded evidence). Each project owns its own `metaharness.toml` and `.metaharness/` data so it can **grow independently** once goals and scope are defined.

**Non-goal (by design):** a second “evolution layer” that rewrites harness code or global config via a separate autonomous loop (see reference doc below for historical ANIMA behavior). Debug and operational complexity stay in **one** outer loop.

---

## What this codebase does

End-to-end flow per **cycle** (see `cycle.py`):

1. **Collect** logs, metrics, git context, and file hints (`evidence.py`).
2. **Gate** on minimum evidence (`cycle.min_evidence_items`).
3. **Diagnose** via Cursor Agent with structured JSON output (`diagnoser.py` + `cursor_client.py`).
4. **Propose** one markdown directive (`proposer.py`), stored under `.metaharness/directives/`.
5. **Veto window** — operator may abort by removing `PENDING_VETO`, or use Slack / `SLACK_EARLY_APPROVE` to proceed early (`cycle.py`).
6. **Implement** — two-phase agent (ANALYZE → EXECUTE) writes allowed files only (`agent.py`, `scope` in config); ANALYZE loads a relevance-filtered file set (`scope.modifiable`, directive mentions, optional `cursor.max_files_per_cycle`).
7. **Test** — configured shell command; optional **git rollback** of agent paths (`[cycle]` rollback flags, `rollback.py`) after failures or metric regression.
8. **Restart** — optional project start command after success.
9. **Delta** — optional primary metric from metrics JSON vs optimization direction.
10. **Log + memory** — per-cycle JSON under `.metaharness/cycles/`; rolling memory + pattern refresh (`memory.py`).

**Daemon** (`daemon.py`): repeats cycles on `interval_seconds`; honors `.metaharness/DAEMON_PAUSE`; can start **Slack Socket Mode** in a background thread when configured.

**CLI** (`cli.py`): `init`, `run`, `daemon` (optional `metaharness-projects.toml` multi-project), `status` (lists registry when discovered), `platform` (resolved OS / Python launcher / Cursor agent), `memory`, `memmap`, `dashboard`, `reasoning`, `graph sync-git` (git commits → KG), `slack listen`, `slack test`.

### Local dashboard (`metaharness dashboard`)

Read-only **localhost** HTTP server (`dashboard.py`): maintenance/product cycle tables, latest metrics, knowledge graph **table row counts**, a **read-only KG snapshot** (counts by `node_type`, recent nodes and edges from SQLite `mode=ro`), and the same **ASCII memory map** as `metaharness memmap` when `[memory]` is enabled and at least one cycle has been recorded. Section **anchors** and `GET /health` (`text/plain` `OK`) support quick navigation and uptime checks.

---

## Repository layout (Python package)

Flat package mapped to `meta_harness` (see `pyproject.toml`).

| Module | Role |
|--------|------|
| `config.py` | Load `metaharness.toml`; derived paths under `.metaharness/` |
| `evidence.py` | Gather evidence for diagnosis |
| `diagnoser.py` | LLM diagnosis → `Diagnosis` |
| `proposer.py` | LLM directive → `Directive` + saved `.md` |
| `cursor_client.py` | Subprocess to Cursor `agent` CLI |
| `agent.py` | Scoped multi-phase apply to disk |
| `cycle.py` | Orchestrates one full cycle; Slack hooks for veto + outcomes |
| `rollback.py` | Optional per-path `git` restore of agent-touched files to HEAD |
| `daemon.py` | Interval loop, pause file, optional Slack thread; optional multi-project round-robin (`run_multi_project_daemon`) |
| `multi_project.py` | Optional `metaharness-projects.toml` registry loader + discovery |
| `memory.py` | `project_memory.json`, compact prompt context, ASCII map, pattern inference |
| `dashboard.py` | Local read-only web UI for cycles, metrics, KG snapshot, memmap (`metaharness dashboard`) |
| `slack_integration.py` | Per-project Slack: posts, Block Kit veto buttons, slash commands, Socket Mode |
| `cli.py` | Click entrypoint `metaharness` |
| `git_kg_sync.py` | Git-first-parent → KG ingest; cursor stored as KG node `git_sync_cursor` |

**Templates:** `templates/metaharness.toml` — default config scaffold; `templates/metaharness-projects.toml` — multi-project registry starter; `templates/git-hook-post-commit.toml` — optional post-commit hook example for `metaharness graph sync-git`.

**Docs:**

| Doc | Role |
|-----|------|
| `README.md` | Install, quick start, user-facing config reference |
| `docs/ANIMAs_META_HARNESS_EXAMPLE.md` | Historical ANIMA-specific harness (two layers, `~/.anima/`, etc.) — **reference only**, not runtime |
| `docs/PROJECT_OVERVIEW.md` | This file — goals, inventory, update log |
| `docs/MULTI_PROJECT.md` | Multi-project registry, pause behavior, `product_project_id` |

---

## Configuration & per-project isolation

- **Project config:** `metaharness.toml` at the target repo root.
- **Runtime state:** `.metaharness/` (directives, cycles, memory, veto files, optional Slack veto context). **Do not commit** (`.gitignore` updated by `metaharness init`).
- **Slack:** `[slack]` section — one Slack app per deployed project is assumed; tokens via env vars recommended (`bot_token_env`, `app_token_env`).

---

## Dependencies (high level)

- **CLI / UX:** `click`, `rich`
- **Config:** `tomllib` / `tomli`, `tomli-w`
- **Evidence:** `gitpython`, `watchdog`
- **Slack:** `slack-bolt`, `slack-sdk`
- **Runtime:** Python ≥ 3.10, **Cursor Agent CLI** on `PATH`

---

## Update log

_Add new entries at the **top** after each meaningful change._

| Date | Summary | Notes |
|------|---------|--------|
| 2026-04-06 | **Cross-platform runtime (Windows + Linux)** | New `platform_runtime.py`: OS detection, `resolve_python_launcher()` (`py` on Windows when available; `python3`/`python` on POSIX; optional `META_HARNESS_PYTHON`), `resolve_cursor_agent_executable()` (PATH + Windows shims + Linux `~/.local/bin` and common prefixes), `merge_subprocess_no_window_kwargs()` for `CREATE_NO_WINDOW` on Windows only. `cursor_client` / `cycle` use shared subprocess flags; `CursorAgentBinaryNotFound` for clear errors. CLI: `metaharness platform`; `status` prints runtime lines; `daemon` logs one-line runtime; Slack `/metaharness platform`. Tests: `tests/test_platform_runtime.py`, updates in `test_cursor_client.py` / `test_cli.py`. |
| 2026-04-06 | **Self-host `run_cycle.*`: canonical pytest-cov** | `scripts/run_cycle.bat` and `scripts/run_cycle.sh` invoke pytest with `--cov=. --cov-config=.coveragerc` (per `coverage_policy.py` / `.coveragerc`) instead of `--cov=meta_harness` alone so `coverage.xml` and `metrics.json` track the flat layout honestly. |
| 2026-04-05 | **Multi-project daemon (`metaharness-projects.toml`)** | Optional registry at a control-plane root: `multi_project.py` loads `[[projects]]` with unique ids and `metaharness.toml` under each `root`. `daemon.run_multi_project_daemon` runs `run_cycle` + optional git KG sync **sequentially** per enabled project; control-plane `DAEMON_PAUSE` + per-project `.metaharness/DAEMON_PAUSE`; timing between full rounds from first project’s `[cycle]`; Slack Socket autostart off; optional `product_project_id` for one product thread. CLI: `metaharness daemon [--projects-file]` discovers registry walk-up; `metaharness status` lists registry when present. Docs: `docs/MULTI_PROJECT.md`; template: `templates/metaharness-projects.toml`; tests: `tests/test_multi_project.py`. |
| 2026-04-05 | **Git → knowledge graph sync (`graph sync-git`)** | New `git_kg_sync.py`: first-parent commits become `git_commit` nodes with `touches` → `file:*` edges; cursor persisted as `meta` node `git_sync_cursor` (no `.metaharness/` prompt edits). Default first run sets cursor to HEAD without backfill; `--full` / `--max-commits` / `--since` for history; `--dry-run`. Optional `METAHARNESS_GIT_KG_SYNC=1` or `metaharness daemon --git-kg-sync` runs sync after each maintenance cycle (failures logged, non-fatal). Tests: `tests/test_git_kg_sync.py`. Template: `templates/git-hook-post-commit.toml`. |
| 2026-04-05 | **Automatic rollback (opt-in)** | `[cycle]` adds `rollback_enabled`, `rollback_on_test_failure`, `rollback_on_metric_regression`, `rollback_require_git` (defaults in `config.py`). `rollback.py` restores `agent.run` paths via `git checkout` / untracked delete; skips ambiguous worktrees unless `rollback_require_git = false`. `cycle.py` / `product_agent.py`: `TEST_FAILED` may record rollback; new `CycleStatus.METRIC_REGRESSION` when tests pass but primary metric regresses and rollback runs. Cycle JSON logs `rollback_*`; KG/memory use accurate `files_changed` after restore. Tests: `tests/test_rollback.py`, `tests/test_cycle.py` (rollback + metric regression). |
| 2026-04-05 | **Test coverage: daemon / product_agent / Slack formatters (extends `test_critical_coverage_boost`)** | More unit tests in `tests/test_critical_coverage_boost.py`: `_next_scheduled_time` invalid entry, `_interruptible_sleep` / signal handler, `product_agent` `_str_list` / `_product_from_response` / `_strip_leading_yaml_frontmatter` / `_vision_prompt_block` + `run_product_cycle` Slack post failure path, `slack_integration` `_truncate_slack_ephemeral` and empty memory/cycle formatters, `cursor_client` `_balanced_chunk` bounds + `_nonzero_exit_message` + `_cli_interrupted_detail`. Representative **`pytest tests/ --cov=. --cov-config=.coveragerc`**: TOTAL **~70% → ~71%** line coverage on the same machine. |
| 2026-04-05 | **Test coverage: critical modules (`test_critical_coverage_boost`)** | Adds `tests/test_critical_coverage_boost.py` targeting `cursor_client`, `cycle`, `diagnoser`, `evidence`, `knowledge_graph`, and `memory` (helpers, `_run_tests` / `_restart_project`, lock timeout, KG ingest/sparkline, etc.). With `pytest tests/ --cov=. --cov-config=.coveragerc`, line **`coverage_pct`** in `metrics.json` moves up (e.g. **71.13% → 72.85%** on a representative run); use the same flags as `.coveragerc` / `coverage_policy` — not `pytest --cov=meta_harness` alone on this flat layout. |
| 2026-04-05 | **Slack: `reset_slack_socket_listener_state` + test teardown** | `slack_integration.reset_slack_socket_listener_state()` clears the process-global Socket Mode handler; `tests/test_slack.py` autouse fixture calls it after each test so listener state cannot leak across cases. **Do not** bind `slack_integration` at `cycle.py` module import time: after `test_conftest_coverage_registration` reloads `meta_harness.*`, a stale `cycle` object would keep an old submodule reference while monkeypatches target the new `sys.modules` entry — veto tests keep lazy `from . import slack_integration` inside `_veto_window` / `run_cycle` Slack blocks. |
| 2026-04-05 | **Evidence: reconcile stale metrics with `last_test_output.txt`** | When pytest collection/import fails, `metrics.json` can still show a high `test_pass_rate` from older junit artifacts. `evidence.collect`: `_pytest_log_indicates_incomplete_run` (e.g. `ERROR collecting`, `ImportError while importing test module`) drives `_reconcile_metrics_with_pytest_tail` — clamps `test_pass_rate` to `0.0`, bumps `test_failed`/`test_count` when present, appends a `MetricsBundle.anomalies` line; `_collect_tests` forces `TestEvidence` to a synthetic `collection` failure when the raw log shows incomplete runs. Tests: `tests/test_evidence.py` (Cases A/B). **Tests:** `tests/test_diagnoser.py`, `test_proposer.py`, `test_product_agent.py` patch `cursor_client` on the same submodule object each module uses (avoids stale `sys.modules` after `test_conftest_coverage_registration`); `test_slack.py::test_veto_window_skips_socket_when_listener_already_active` rebinds `slack_integration` via `importlib.import_module`. |
| 2026-04-05 | **Dashboard: memmap panel + KG snapshot + `/health`** | `dashboard.py`: mini-TOC anchors (Cycles, Metrics, Knowledge graph, Memory map); read-only SQLite snapshot (nodes by `node_type`, recent nodes/edges); `memory.render_map` in `<pre class="memmap">` when memory enabled and cycles exist (aligned with CLI `memmap`); `GET /health` → `200` `text/plain` `OK`. Tests: `tests/test_dashboard.py` (`test_build_dashboard_html_memmap_section`, `test_build_dashboard_html_kg_snapshot`, HTTP health assert). |
| 2026-04-05 | **Canonical coverage policy module (`coverage_policy`)** | `meta_harness/coverage_policy.py` defines the flat-layout pytest-cov fragments (`--cov=.`, `--cov-config`, XML report, `tests/`) aligned with `.coveragerc`; `tests/test_coverage_policy.py` fails if policy or `[run]` drifts. Operators should align `scripts/run_cycle.*` with this in maintenance (not changed here). |
| 2026-04-05 | **`extract_json` fenced JSON + default `json_retries`** | `_extract_json_via_fenced_raw_decode` uses `(?:\r?\n)+` after fence lang (blank lines after ` ```json `), iterates openings in reverse and returns the first successful `raw_decode` (last fence in the document), including **unclosed** fences and trailing prose after a complete value. Default `CursorConfig.json_retries` is **3** (four subprocess attempts when TOML omits the key). Tests: `test_extract_json_fenced_blank_lines_after_json_opener`, `test_extract_json_unclosed_json_fence*`, `test_json_call_unclosed_json_fence`, `test_load_defaults` asserts `json_retries == 3`. |
| 2026-04-05 | **`extract_json` oversized fenced block inners** | When a single fence inner exceeds `_EXTRACT_JSON_MULTI_MAX_INNER` and `json.loads` fails, scan the last `_EXTRACT_JSON_MULTI_MAX_OFFSET` chars with `_multi_candidate_json_scan`, then `_suffix_oriented_json_extract`. Test: `test_extract_json_oversized_fence_inner_finds_trailing_json`. |
| 2026-04-05 | **`json_call` custom `parse_retry_user_suffix` escalates** | First parse-only retry appends only the caller string; later parse retries append the same escalating default paragraphs (`mild` then `strict`) after it. Test: `test_json_call_parse_retry_custom_suffix_escalates_on_later_retries`. |
| 2026-04-05 | **`json_call` parse retries + escalating default suffix** | Without `parse_retry_user_suffix`, parse-only retries append `_default_parse_retry_suffix(0)` then a stricter `_default_parse_retry_suffix(1+)`. New test: `test_json_call_parse_retry_succeeds_on_third_attempt_default_escalating_suffix`. (Default `json_retries` when TOML omits the key: see newest **`extract_json` fenced JSON** row.) |
| 2026-04-04 | **Canonical flat-layout coverage + `.coveragerc` guard** | Documented in `README.md` (`## Coverage (flat layout)`): use `pytest tests/ --cov=. --cov-config=.coveragerc` so pytest-cov traces the working tree; `pytest --cov=meta_harness` alone can mis-report on this layout. Self-hosting example updated to match. New `tests/test_coveragerc_layout.py` asserts `.coveragerc` `[run]` keeps `branch = true`, `source = .`, and `tests/*` under `omit`, plus top-of-file comment still contrasts `--cov=.` vs `--cov=meta_harness`. |
| 2026-04-04 | **`json_call` parse-failure repair suffix** | `cursor_client.json_call`: optional `parse_retry_user_suffix`; after a nonempty stdout that still fails `extract_json`, retries append that suffix or `_DEFAULT_JSON_REPAIR_SUFFIX` (empty-stdout retries do not). `diagnoser.run` passes a diagnosis-schema suffix. Tests: `test_cursor_client.py` (parse vs empty retry prompts), `test_diagnoser.py` (inline retry succeeds without `diagnose_repair`). |
| 2026-04-04 | **Last Cursor CLI failure artifact** | `cursor_client.py`: `_persist_last_cursor_failure` writes bounded UTF-8 `last_cursor_failure.txt` under `harness_dir` on non-zero exit, JSON parse failure after retries, `FileNotFoundError`, and `TimeoutExpired` (labels `json_call:*`, `json_call:parse`, `agent_call:*`). `evidence.py`: `cursor_cli_failure_excerpt` (≤4k chars). `diagnoser._build_prompt`: “Last Cursor / Agent CLI failure” section when excerpt non-empty. Tests: `test_cursor_client.py`, `test_evidence.py`, `test_diagnoser.py`. |
| 2026-04-04 | **Cursor CLI exit codes in errors** | `cursor_client.py`: non-zero subprocess exits use `_nonzero_exit_message` (`Cursor agent CLI exited with code N: …` with stderr, else stdout, else `(no stdout/stderr)`). `FileNotFoundError` / `TimeoutExpired` append `(agent binary: …)`. `complete()` inherits the same strings via `agent_call`. Tests updated in `tests/test_cursor_client.py`. |
| 2026-04-04 | **ANALYZE empty-plan retry** | `agent.py` `_phase_observe_and_plan`: if the first JSON response parses but `changes` is empty, one follow-up `json_call` with an appended harness prompt; merged `1_analyze` reasoning shows attempt 1 and 2. Still surfaces `Plan produced no changes.` when both attempts are empty. Tests in `tests/test_agent.py`. |
| 2026-04-04 | **ANALYZE file relevance + cap** | `agent.py`: loads `scope.modifiable` matches plus paths/filenames cited in the directive; skips protected and noise dirs (`_is_readable`); if nothing matches, falls back to all readable non-protected. `[cursor] max_files_per_cycle` (0 = unlimited): when over cap, keeps directive-mentioned files first, then orders by `evidence.git_recent_paths`, then name. `evidence.py` fills `git_recent_paths` via `git log --name-only`; `cycle.py` passes `evidence` into `agent.run`. |
| 2026-04-04 | **Added `docs/PROJECT_OVERVIEW.md`** | Living overview + changelog; documents goal, single-layer stance, module map, Slack/daemon behavior, maintenance rule for future entries. |
| 2026-04-04 | **Slack integration (per-project)** | `[slack]` in `metaharness.toml`; `slack_integration.py`: veto window posts + Block Kit buttons, slash commands (`status`, `memory`, `pause`, `resume`, `veto`, `proceed`, `help`), `metaharness slack listen` / `slack test`; daemon pause via `DAEMON_PAUSE`; early proceed via `SLACK_EARLY_APPROVE`; cycle outcome posts; optional Socket Mode thread with `metaharness daemon`. Dependencies: `slack-bolt`, `slack-sdk`. |
| (prior) | **Generalized Meta-Harness package** | Extracted from ANIMa-oriented outer loop; flat `meta_harness` package, `metaharness` CLI, evidence → diagnose → propose → veto → agent → test → restart → metric delta → memory. See `README.md` Origin. |

---

## How to update this document

1. After a feature, fix, or behavior change worth tracking: add a **new first row** in the table above (ISO date `YYYY-MM-DD`).
2. If the change alters architecture or goals, update the **Goal**, **What this codebase does**, or **Repository layout** sections in the same PR/commit.
3. Keep entries factual (what changed, where), not marketing.
