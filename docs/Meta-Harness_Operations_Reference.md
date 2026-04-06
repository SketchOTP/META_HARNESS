# Meta-Harness Operations Reference
*Last updated: April 5, 2026 — 326 tests passing, coverage 25.36%*

---

# Slack Commands

## Daemon Control

- `/metaharness pause` — Pause the daemon. No new cycles fire. Cycles already in-flight still complete. Writes `DAEMON_PAUSE` file.
- `/metaharness resume` — Resume after a pause. Removes `DAEMON_PAUSE`. Next scheduled cycle fires normally.

## Maintenance Cycles — Veto & Approve

- `Approve button` *(tap in the posted Slack message)* — Skip remaining veto window and implement immediately.
- `Veto button` *(tap in the posted Slack message)* — Abort the directive. Records as VETOED.
- `/metaharness proceed` — Text fallback for Approve if buttons don't render.
- `/metaharness veto` — Text fallback for Veto if buttons don't render.

## Product Cycles — Veto & Approve

- `/metaharness product proceed` — Early-approve the pending product directive. Default veto window is 600s — this skips the wait.
- `/metaharness product veto` — Abort the pending product directive.

## Status & Observability

- `/metaharness status` — Recent maintenance cycle history. Directive IDs, statuses, deltas.
- `/metaharness memory` — Compact memory context (~80 tokens). What the diagnoser and proposer currently see.
- `/metaharness memmap` — Full ASCII memory map. Directive chain, file heat map, metric trajectory, learned patterns.
- `/metaharness product status` — Recent product cycle history. P-series directives, statuses, deltas.
- `/metaharness product roadmap` — Vision roadmap. Features done vs wanted from `[vision]` config section.
- `/metaharness help` — List all available commands.

## Research Agent

- `/metaharness research <url>` — Fetch, parse, and evaluate a paper for implementation relevance. The slash handler **acks immediately**; the harness evaluates in a background thread and posts a **verdict** message when done. If the verdict is **implement** and **enqueue succeeds**, a short optional channel post (“Research — ready to implement”) is sent **once**, using the same path as `metaharness research eval` (`notify_research_queue_from_evaluation` → `notify_research_queue_item`). That ping is suppressed when `[slack] notify_research_queue = false`, when enqueue fails (e.g. queue full), or for **monitor** / **discard**. You will typically see **two** channel messages for a successful **implement** when notifications are on: the ping, then the longer verdict (not duplicate logic—different content).
- `/metaharness research queue` — Show all papers currently queued for the product agent.
- `/metaharness research discard <url>` — Remove a specific paper from the queue by its URL.

## Veto Window Timing

- Maintenance veto window: `300s` (5 min)
- Product veto window: `600s` (10 min)
- Manual override if Slack is down: create file `.metaharness\SLACK_EARLY_APPROVE` with content `proceed`

## Scheduled Cycles

- Maintenance: `07:00, 12:00, 17:00, 22:00` local time
- Product: `09:00, 14:00, 19:00` local time

---

# Daemon Health Check

`/metaharness status` shows cycle history but does NOT confirm the process is alive. Use this to verify:

**To check if daemon is running:**
- Look at the most recent cycle timestamp in `/metaharness status`
- If no cycle has fired at a scheduled time, the daemon is likely dead
- On the machine: check for a running `metaharness daemon` process in Task Manager or PowerShell:
  `Get-Process | Where-Object { $_.MainWindowTitle -like "*metaharness*" }`

**To restart the daemon:**
```
py scripts\generate_metrics.py
metaharness daemon
```

**Signs the daemon is alive:**
- Slack posts veto window messages at scheduled times
- `/metaharness status` shows cycles timestamped at 07:00, 12:00, 17:00, or 22:00
- Terminal window shows `Meta-Harness daemon started` and is still open

**Signs the daemon is dead:**
- No Slack activity at scheduled times
- Status shows last cycle was many hours ago
- Terminal window closed or shows an exception

---

# Pre-Cycle Checklist

`generate_metrics.py` MUST be run manually before every `metaharness run`. If skipped, the cycle operates on stale metrics and the KG records incorrect baseline values.

**Before every manual run:**
```
py scripts\generate_metrics.py
metaharness run
```

**Before starting the daemon:**
```
py scripts\generate_metrics.py
metaharness daemon
```

The daemon does NOT auto-run `generate_metrics.py`. This is intentional — metrics generation is a separate concern. The script reads pytest junit XML and coverage XML and writes `metrics.json`.

---

# Protected Files — Never Modify

The following files must never be touched by any agent directive. If a proposed directive targets any of these, **veto immediately**.

- `metaharness.toml` — project config, all agent behaviour derives from this
- `pyproject.toml` — package definition (exception: adding new dependencies is allowed)
- `setup.py` / `setup.cfg` — package setup
- `scripts/` — all scripts including `generate_metrics.py` and `run_cycle.bat`
- `.metaharness/` — runtime state, KG, cycle logs, directives (gitignored)
- `.git/` — version control

If an agent proposes modifying any of these, the KG and memory will note the veto. The proposer learns from vetoes over time.

---

# Directive ID Naming Conventions

| Prefix | Layer | Example | Notes |
|--------|-------|---------|-------|
| `M_NNN_auto` | Maintenance | `M025_auto` | Current maintenance format |
| `P_NNN_auto` | Product | `P005_auto` | Product agent directives |
| `D_NNN_auto` | Legacy maintenance | `D022_auto` | Early directives, treated as maintenance |
| `research_agent` | Human-issued | `research_agent` | Human directives synced manually |

**Reading the memmap directive chain:**
- `✓` = COMPLETED
- `✗` = failed (TEST_FAILED or AGENT_FAILED)
- `⊘` = VETOED
- `+0.00` = delta on primary metric (test_pass_rate)

Numbers increment sequentially. Gaps indicate vetoed or failed cycles that weren't written to the KG.

---

# When to Veto

Veto a directive immediately if any of the following are true:

- **Touches protected files** — `metaharness.toml`, `scripts/`, `.metaharness/`
- **Proposes what was just vetoed** — proposer is looping. Let one more cycle pass; if it proposes the same thing again, manually add a note to the KG.
- **Test count would decrease** — any directive that removes or skips tests without a clear reason.
- **Third consecutive cycle targeting `cursor_client.py`** without a coverage gain — the harness is fixating. Veto and let it find a different target.
- **Modifies `metaharness.toml`** — agents must never change their own config.
- **`changes_applied: 0` in a COMPLETED cycle** — agent ran but touched nothing. Not harmful but worth noting.
- **Delta is negative** — metric went backwards. Check what changed before approving the next cycle.

**When NOT to veto:**
- Delta is `+0.000` — flat is fine, most cycles hold the line on `test_pass_rate = 1.0`
- Coverage is low — the agents are actively working on this; low coverage alone is not a reason to veto
- You don't understand the directive title — read the directive file in `.metaharness/directives/` first

---

# KG Sync Procedure

Human-issued directives bypass the daemon pipeline. No cycle JSON is written automatically, so `metaharness status` and `product status` show stale entries until you sync.

**After completing any human-issued work, run:**
```
metaharness sync --id <directive_id> --title "<title>" --layer <maintenance|product> --files "file1.py,file2.py" --post-metric 1.0 --pre-metric 1.0
```

**Example — closing a product directive:**
```
metaharness sync --id P003_auto --title "Dashboard memmap panel and KG snapshot view" --layer product --files "dashboard.py,tests/test_dashboard.py" --post-metric 1.0 --pre-metric 1.0 --note "All 10 tests passing after P005 coverage fix"
```

**Example — recording a new human-issued feature:**
```
metaharness sync --id research_agent --title "Research paper ingestion pipeline via Slack and CLI" --layer product --files "research.py,config.py,slack_integration.py,product_agent.py,cli.py" --post-metric 1.0 --pre-metric 1.0
```

After syncing, verify with:
```
metaharness product status
metaharness memmap
```

`synced: true` is written into every sync'd cycle JSON so automated tooling can distinguish human syncs from daemon cycles.

---

# Known Failure Modes & Recovery

## Daemon dies silently
**Symptoms:** No Slack veto messages at scheduled times. Status shows last cycle was hours ago.
**Recovery:** Restart the daemon.
```
py scripts\generate_metrics.py
metaharness daemon
```

## Cursor CLI timeout
**Symptoms:** Cycle ends with `AGENT_FAILED`. Status shows `agent_timeout` in error column. KG records the failure.
**Recovery:** Nothing to do — the daemon will retry at the next scheduled time. If it times out three cycles in a row, check that Cursor CLI is working:
```
C:\Users\sketc\AppData\Local\cursor-agent\agent.CMD --version
```

## Socket Mode drops
**Symptoms:** Veto window opens but Approve/Veto buttons don't respond. No `Slack approve handler fired` in terminal.
**Recovery:** Use text fallback commands immediately:
- `/metaharness proceed` to approve
- `/metaharness veto` to abort
- Or on the machine: `echo proceed > .metaharness\SLACK_EARLY_APPROVE`

Socket Mode restarts automatically on the next daemon cycle.

## TEST_FAILED loop
**Symptoms:** Same directive class fails 2–3 cycles in a row. Memmap shows `✗ → ✗ → ✗`.
**When to let it run:** First 2 failures — the harness is learning. Check memmap for learned failure patterns.
**When to intervene:** Third consecutive failure on the same target file. Veto the next proposal and manually inspect the failing tests:
```
py -m pytest tests\ -v --tb=short 2>&1 | Select-Object -First 60
```

## Coverage reporting 0% for tested modules
**Symptoms:** `coverage_pct` in `metrics.json` is far lower than expected. `memmap` shows low coverage despite many tests.
**Cause:** `.coveragerc` source layout mismatch with flat package layout.
**Recovery:** Let the product agent target this — it's aware of the issue. Do not manually edit `.coveragerc` unless you understand the flat layout implications.

## `generate_metrics.py` not run before cycle
**Symptoms:** Cycle fires on stale `test_pass_rate`. Delta looks wrong.
**Recovery:** Not recoverable for that cycle. Run `generate_metrics.py` before the next cycle. Optionally use `metaharness sync` to correct the KG record.

---

# Research Queue Workflow

The research agent does NOT immediately implement papers. The flow is:

1. Drop `/metaharness research <url>` in Slack
2. Harness fetches the paper (arxiv HTML + PDF, direct PDF, or HTML fallback)
3. Cursor plan mode evaluates relevance against the project vision
4. Verdict posts in ~30–60s:
   - **implement** → paper is added to `.metaharness/research/queue.json` when `queue_paper` succeeds; optional Slack ping only on successful enqueue and when `[slack] notify_research_queue` is true (see Research Agent above)
   - **monitor** → saved to backlog, not queued; no queue ping
   - **discard** → not queued, reason posted; no queue ping
5. At the next product cycle, the product agent reads the queue and injects it as context into the diagnosis and proposal prompts
6. The product agent may propose a directive to implement the paper's technique — this still goes through the normal veto window

**The queue is advisory.** The product agent uses queued papers as context but is not forced to implement one every cycle.

**To check what's queued:**
```
/metaharness research queue
```
or
```
metaharness research queue
```

**To remove a paper:**
```
/metaharness research discard <url>
```

**Queue cap:** 10 items. When full, the oldest `monitor` item is replaced. If all 10 are `implement`, new papers are rejected until you discard one.

---

# Environment Variables

These are set as Windows user environment variables on the host machine.

| Variable | Value | Notes |
|----------|-------|-------|
| `SLACK_BOT_TOKEN` | `xoxb-...` | Bot token. Starts with `xoxb-`. Required for all Slack features. |
| `SLACK_APP_TOKEN` | `xapp-...` | App-level token for Socket Mode. Starts with `xapp-`. Required for veto buttons. |
| `META_HARNESS_DEBUG` | `1` | Currently set. Shows Cursor CLI command and diagnoser debug lines. **Unset for clean production output.** |

**To unset META_HARNESS_DEBUG for production:**
In Windows: System Properties → Environment Variables → User variables → delete `META_HARNESS_DEBUG`

Or per-session in PowerShell:
```
$env:META_HARNESS_DEBUG = ""
```

If `SLACK_BOT_TOKEN` or `SLACK_APP_TOKEN` are missing or expired, Slack integration silently degrades — veto buttons won't appear and Socket Mode won't start. The daemon continues running but all approvals must be done via file-based override.

---

# Current Metric Baselines
*As of April 5, 2026*

| Metric | Value |
|--------|-------|
| `test_pass_rate` | `1.0` (326 passed, 2 skipped) |
| `coverage_pct` | `25.36%` |
| `branch_coverage_pct` | `18.54%` |
| `test_count` | `328` |
| `test_duration` | `~6.6s` |

**North star targets (from vision config):**
- `test_pass_rate`: maintain `1.0`
- `coverage_pct`: increase toward configured north star (agents are actively working on this)

If a cycle reports `coverage_pct` below `25%` or `test_pass_rate` below `1.0` after a COMPLETED cycle, inspect what changed before approving the next directive.

---

# Two-Layer Architecture Summary

Meta-Harness runs two independent agents on separate schedules sharing one KG.

**Maintenance Agent**
- Schedule: `07:00, 12:00, 17:00, 22:00`
- Directive prefix: `M_NNN_auto` (legacy: `D_NNN_auto`)
- Focus: stability, test coverage, bug fixes, code quality
- Veto window: 300s

**Product Agent**
- Schedule: `09:00, 14:00, 19:00`
- Directive prefix: `P_NNN_auto`
- Focus: new features, vision execution, roadmap items
- Veto window: 600s
- Reads research queue as additional context

**Agent mutex:** Only one agent implements at a time. If both fire simultaneously, the second waits up to 300s for the lock. This prevents concurrent file modifications.

**Shared KG:** Both agents read and write to the same `knowledge_graph.db`. The product agent can see maintenance failures and vice versa. This is intentional — cross-layer context improves proposals.