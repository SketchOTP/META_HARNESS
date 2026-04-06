# META_HARNESS — How the ANIMA meta harness works (from code)

This document describes the **autonomous outer loop** and **Layer 2 evolution** exactly as implemented in the repository. It is derived from the Python modules listed in each section, not from product marketing or external papers.

---

## 1. What “meta harness” refers to in this repo

The entry point is `anima_outer_loop_daemon.py`. Its module docstring defines the **Meta-Harness Autonomous Outer Loop Daemon** as a process that watches `~/.anima/traces/` for **new complete sessions**; when enough accumulate, it runs a cycle: **diagnose → propose → veto window → agent execute → pytest → delta check → restart ANIMA → archive traces → repeat**.

Two layers appear in code:

| Layer | Role in code |
|--------|----------------|
| **Layer 1** | `run_cycle()` — config/curriculum-style changes driven by a **staged HexCurse directive** (`D###_auto.md`) and the Cursor `agent` CLI. |
| **Layer 2** | `run_evolution_cycle()` — harness **code or `~/.anima/config.json`** changes from a **JSON proposal** produced by `build_evolution_prompt()` + `_call_agent()` + `parse_agent_response()`. |

---

## 2. Files, directories, and locks (authoritative paths)

From `anima_outer_loop_daemon.py`:

| Symbol | Path / meaning |
|--------|----------------|
| `REPO_ROOT` | Directory containing `anima_outer_loop_daemon.py` (repo root). |
| `TRACES_DIR` | `Path.home() / ".anima" / "traces"` |
| `CYCLES_LOG` | `~/.anima/outer_loop_cycles.jsonl` — append-only JSON lines per cycle. |
| `CONFIG_FILE` | `~/.anima/outer_loop_config.json` — merged over `DEFAULT_CONFIG`. |
| `VETO_FILE` | `~/.anima/ / "PENDING_VETO"` — presence during the window means “pending”; **absence before expiry = veto** (see §6). |
| `LOCK_FILE` | `~/.anima/outer_loop_running.lock` — if present, `run_cycle()` returns `SKIPPED_LOCKED`. |
| `EVOLUTION_APPROVAL_FILE` | `~/.anima/PENDING_EVOLUTION_APPROVAL` — first-time **code** edits may require deleting this file to approve. |
| `DIGEST_STATE_FILE` | `~/.anima/outer_loop_digest.json` — daily digest bookkeeping. |

`SessionWatcher.cycle_marker` (same module): `~/.anima/outer_loop_last_cycle.marker` — mtime defines “sessions since last cycle” for the **watcher** (see §4).

Knowledge base (`anima/evaluation/knowledge_base.py`): `KB_FILE = ~/.anima/outer_loop_knowledge.json`.

Evolution trust (`anima/evaluation/evolution_config.py`): `TRUST_STATE_FILE = ~/.anima/evolution_trust.json`.

Diagnosis output (`anima/evaluation/diagnose.py`): writes under the chosen traces dir, notably `diagnosis_latest.json` (via `_atomic_write`).

---

## 3. Configuration loading and defaults

`load_config()` in `anima_outer_loop_daemon.py`:

- Ensures parent dir for `CONFIG_FILE` exists.
- If the JSON file exists, returns `{**DEFAULT_CONFIG, **saved}` (saved keys override defaults).
- If missing, writes `DEFAULT_CONFIG` to disk and returns a copy.

Important `DEFAULT_CONFIG` keys used later in the same file:

- **Triggering:** `sessions_per_cycle` (default `3`), `poll_interval_seconds`, `enabled`.
- **Veto:** `veto_window_minutes`, `veto_poll_seconds`, `notify_desktop`.
- **Layer 1 agent:** `model_primary`, `model_fallback1`, `model_fallback2`, `max_pytest_retries` (used implicitly via the model list loop).
- **Health gate:** `min_delta_threshold` (default `-0.02`).
- **Runtime:** `anima_restart_wait_seconds`.
- **Diagnosis / proposer coupling:** `use_llm_proposer` (read again inside `diagnose.py` from the same JSON file — see §5).
- **Layer 2:** `enable_layer2_evolution`, `layer2_every_n_cycles`, `model_evolution`, `require_explicit_approval`.
- **Slack / synthesis:** `slack_*`, `enable_post_cycle_synthesis`, `slack_daily_digest`, `slack_daily_digest_hour`.

`MAX_POST_CHANGE_WAIT_HOURS = 24` caps how long post-change session collection waits before logging with whatever data exists.

---

## 4. When a cycle starts: `SessionWatcher.watch()`

Implemented in `anima_outer_loop_daemon.py`:

1. Logs trigger threshold and poll interval; optionally starts ANIMA via `restart_anima()` if `pgrep` says nothing matches `python.*-m anima$`.
2. Ensures `cycle_marker` exists (`_update_marker()` on first run).
3. **Loop forever:**
   - `cfg = load_config()` — picks up operator edits without daemon restart.
   - If `not cfg.get("enabled", True)`, sleep 60s and continue.
   - Optional **daily digest** when `slack_daily_digest`, Slack is configured, and `_should_send_digest(cfg)`.
   - `count, _ = count_new_complete_sessions(self.cycle_marker)`.
   - If `count >= cfg["sessions_per_cycle"]`, calls `run_cycle(cfg)`, logs outcome, then **`_update_marker()`** (updates marker **after** the cycle, so the next batch counts sessions newer than this time).

### 4.1 What counts as a “new complete session”

`count_new_complete_sessions(baseline_marker)`:

- Iterates **directories** under `TRACES_DIR`.
- Skips names starting with `pre_` or `diagnosis`.
- Requires `manifest.json`; parses JSON.
- Session counts only if `complete` is true, `ticks >= 100`, and directory `st_mtime > baseline_marker.stat().st_mtime` (or all sessions if marker missing: baseline time `0`).

So “complete” is **manifest-driven**, not inferred from file count alone.

The **diagnose** path uses `anima/evaluation/trace_reader.load_all_sessions()`, which by default loads **complete-only** traces (`complete_only=True`) from manifests — aligned with the idea of a finished session bundle.

---

## 5. Layer 1 — Diagnosis (`run_diagnosis`)

`run_diagnosis()` runs:

```text
python -m anima.evaluation.diagnose
```

with timeout 120s. Failure → `run_cycle` returns `ABORTED_DIAGNOSIS` and logs Slack if configured.

Inside `anima/evaluation/diagnose.py`, `run_diagnosis()`:

1. Loads sessions via `load_all_sessions(traces_dir)`.
2. If fewer than `min_sessions`, may return `{}` early (and print to stderr).
3. Calls `aggregate(...)` from `anima.evaluation.aggregator` to produce cross-session stats (health trend, dead channels, zero addons, dream health, etc.).
4. Reads `use_llm_proposer` and `model_primary` from `~/.anima/outer_loop_config.json` via `_outer_loop_proposer_prefs()`.
5. If `use_llm` is true, tries `llm_propose(model=model_primary)` from `anima.evaluation.llm_proposer`; on success uses that list as **recommendations**; on failure or exception falls back to `generate_recommendations(stats)` from `anima.evaluation.recommender`. If `use_llm` is false, always rule-based recommender.
6. Builds **flags** from stats (vocal, chronic dead channels, trend, etc.).
7. Writes outputs with `_atomic_write` (creates parent dirs, temp file + `os.replace`).

Downstream consumers include:

- `outer_loop_proposer` — reads `diagnosis_latest.json`.
- `run_evolution_cycle` — reads `behavioral_health.trend` from the same file for the evolution prompt context.
- `_load_diagnosis_latest()` in the daemon — Slack / KB shaping.

---

## 6. Layer 1 — Proposing work (`run_proposer`)

`run_proposer()` runs:

```text
python -m anima.evaluation.outer_loop_proposer
```

On success, the daemon finds the **most recently mtime-sorted** `HEXCURSE/DIRECTIVES/D*_auto.md` and returns that `Path`. Failure modes: non-zero exit, or no auto directive file.

`anima/evaluation/outer_loop_proposer.py`:

- Loads diagnosis from `traces_dir/diagnosis_latest.json` (`_load_diagnosis`).
- Picks next filename `D006_auto.md` … `D099_auto.md` via `_next_auto_directive_id`.
- `generate_directive()` builds a **HexCurse markdown directive** summarizing health table, flags, **high/medium** recommendations as actionable steps (`_format_change_block`), and **low** priority as auto-rejected list.
- Writes the `.md` file and (per module docstring) appends a queued entry to `HEXCURSE/DIRECTIVES.md`.

The proposer **does not** apply edits; it only **stages** human/agent-readable instructions.

---

## 7. Layer 1 — Veto window (`wait_for_veto` / `poll_veto_until`)

`wait_for_veto()`:

1. `wait_minutes = max(2, cfg["veto_window_minutes"])`.
2. `write_veto_file(directive_name, cfg, expires_at)` — writes JSON to `PENDING_VETO` with directive name and expiry metadata.
3. Optional desktop notification.
4. If Slack configured: builds intro text (Layer 1 uses `_layer1_veto_slack_intro`; Layer 2 can inject `cfg["_slack_veto_intro"]` before calling) and `_slack_post`.
5. Returns `poll_veto_until(...)`.

`poll_veto_until()` semantics:

- **While** `time.time() < expires_at`:
  - If **`VETO_FILE` does not exist** → treat as **veto** → return **`False`** (“do not proceed”).
  - Else if Slack thread ts set and `_slack_check_veto` finds the word `veto` in a reply → delete veto file, post confirmation, return **`False`**.
  - Sleep `veto_poll_seconds`.
- After timeout → **unlink** `VETO_FILE` if still present → return **`True`** (“proceed”).

So: **file deleted early = abort cycle**; **file left in place until timeout = proceed**. This matches the log line that says deleting the file vetoes.

When the operator vetoes Layer 1, `run_cycle` calls `archive_traces(directive_name + "_vetoed")` and returns `VETOED`.

---

## 8. Layer 1 — Agent execution and pytest

After veto clears:

- `models = [model_primary, model_fallback1, model_fallback2]`.
- For each `model`, `run_agent(directive_path, model)`:
  - Invokes Cursor CLI: `agent -p --yolo --model <model> --output-format text` with a **prompt string** instructing the agent to read and execute the directive path, run pytest, report DONE.
  - Timeout 1800s.
- On agent success, `run_pytest()` runs `python -m pytest tests/ -q --tb=short` (timeout 300s).
- If pytest fails, `rollback_config(config_backup)` and try next model.
- If all models exhausted → rollback config, `restart_anima`, log `ABORTED_PYTEST`.

`backup_config()` copies `~/.anima/config.json` to `~/.anima/config.json.outer_loop_backup` at **start** of `run_cycle` (inside try, after lock).

---

## 9. Layer 1 — Post-apply: archive, restart, wait, delta

On agent + pytest success:

1. `archive_traces(directive_name)` — moves each session directory (except `pre_*` and `diagnosis`) into `TRACES_DIR / f"pre_{directive_name}_baseline"`.
2. `restart_anima(cfg)` — `pkill` patterns for `python -m anima` and Godot anima project; wait; optionally wait if ports 5556/5557 busy; `nohup ./run_anima.sh` if present.
3. **Post-change wait:** loop until `count_new_complete_sessions(LOCK_FILE) >= sessions_per_cycle` or 24h timeout. Note the marker here is **`LOCK_FILE`** (the cycle lock path), not `cycle_marker` — new sessions must be newer than lock file mtime.
4. `delta = compute_post_delta(pre_health, TRACES_DIR)` — mean of `behavioral_health.json` scores under `TRACES_DIR` minus `pre_mean` (implementation uses current tree vs stored pre mean; see `get_health_scores`).

If `delta < min_delta_threshold`:

- Rollback config, restart ANIMA, `log_cycle(..., "ABORTED_DELTA", ...)`, `_post_cycle_knowledge_layer1(...)`, optional large-degradation Slack, return `ABORTED_DELTA`.

If delta OK:

- `log_cycle(..., "COMPLETED", ...)`, `_post_cycle_knowledge_layer1(...)`, Slack, desktop notify.

---

## 10. Knowledge base updates (`knowledge_base.update_after_cycle`)

`anima/evaluation/knowledge_base.py`:

- `update_after_cycle(...)` **only runs persistence logic** when `outcome in ("COMPLETED", "ABORTED_DELTA")`. Other outcomes do not update KB through this function.
- Increments `cycle_count`.
- On `ABORTED_DELTA`: appends to `failed_approaches`, may append `config_key` to `do_not_repeat`, trims lists.
- On `COMPLETED` with `delta > 0.005`: appends to `successful_changes`.
- May update `current_hypothesis` from optional `synthesis`.

`_post_cycle_knowledge_layer1` / `_post_cycle_knowledge_layer2` in the daemon:

- If `enable_post_cycle_synthesis`, call `synthesize_lesson(...)` which loads KB, formats context via `format_for_prompt`, then runs **`agent`** subprocess (same CLI) with a paragraph-only prompt.
- Then `update_after_cycle(..., is_evolution=False/True, ...)`.

Layer 1 proposal snippet for KB is built by `_layer1_proposal_dict_for_kb()` from **first matching** high/medium recommendation in `diagnosis_latest.json` (config/curriculum/action_bias shapes).

`format_for_prompt()` (used in synthesis and evolution prompt KB block) includes recent slices of hypothesis, behavioral_understanding, do_not_repeat, failed/successful lists, and optionally evolution history + `git show` stats.

---

## 11. When Layer 2 runs

At the end of a **successful** Layer 1 cycle (`run_cycle` returns after `COMPLETED` path), if `cfg.get("enable_layer2_evolution")`:

- Reads `CYCLES_LOG` lines, counts lines containing `"COMPLETED"`.
- `n = max(1, int(cfg.get("layer2_every_n_cycles", 3)))`.
- If `comp_count > 0` and `comp_count % n == 0`, calls `run_evolution_cycle(cfg)`.

Exceptions are caught and logged; Layer 2 failure does not change the Layer 1 return value (already `COMPLETED`).

---

## 12. Layer 2 — Prompt, agent call, parse

`run_evolution_cycle()` in `anima_outer_loop_daemon.py`:

1. `pre_health = compute_pre_health()` — same mean over `behavioral_health.json` in `TRACES_DIR`.
2. Loads `diagnosis_latest.json` for `behavioral_health.trend` if present.
3. `build_evolution_prompt(pre_health, health_trend)` from `anima/evaluation/evolution_reasoner.py`:
   - Pulls recent traces, cycle log, diagnosis, **evolvable file excerpts** from paths in `evolution_config.EVOLVABLE_FILES`, frozen list, `INTERFACE_CONTRACTS`, recent Layer 1 auto-directives, and **`_kb_block_for_evolution()`** (`knowledge_base.load` + `kb_format_for_prompt(..., include_evolution_diffs=True)`).
4. `_call_agent(prompt, model)` runs subprocess `agent -p --yolo --model <model_evolution> ...` with **prompt on stdin** (not argv), cwd `REPO_ROOT`, timeout 600s.
5. `parse_agent_response(stdout)` validates JSON, required keys, **frozen target** via `evolution_config.is_frozen`, and **non-empty implementation** for `code_change` / `config_key` for `config_change` (with nested `config_change` / `for config_change` / `for_config_change` unwrap for config). Returns `None` on any failure → `ABORTED_PARSE`.

---

## 13. Layer 2 — Approval, veto, apply, verify

Still in `run_evolution_cycle`:

- **`needs_explicit`** when `code_change` and `requires_explicit_approval(target_file)` and `require_explicit_approval` is true: writes `PENDING_EVOLUTION_APPROVAL` JSON; operator must **delete file** to approve within window; else `REJECTED_EXPLICIT`. On approval, `record_approval()` appends to `evolution_trust.json`.

- Otherwise uses same **`wait_for_veto`** as Layer 1, with Slack intro built from proposal summary (`_get_proposal_diff_summary`, diagnosis blocks). Veto → `VETOED`.

- `backup_config()` again, then `apply_proposal(proposal)` from `anima/evaluation/evolution_applier.py`:
  - **`config_change`:** `apply_config_change` — mutates `~/.anima/config.json` with backup `.json.evolution_backup`, nested implementation keys mirrored from applier.
  - **`code_change`:** `apply_code_change` — evolvable/frozen checks, backup, writes full file or applies patch per implementation (see applier source), then optional **`_validate_curriculum_change`**, **`_validate_reward_change`**, then **`verify_interface_contracts()`** running `verify_launch.py`. Failures restore from backup.

- Full **`pytest`** again; failure rolls back config and `restore_file(code_backup)` if any.

- Same post-apply **archive_traces**, **restart_anima**, **session wait** vs `LOCK_FILE`, **delta** vs `min_delta_threshold`. On bad delta: rollback config + code, `_post_cycle_knowledge_layer2`, optional large degradation Slack.

- On success with `code_change`, `_git_commit_evolution` may commit.

---

## 14. Evolution scope and trust (single source of truth)

`anima/evaluation/evolution_config.py`:

- **`EVOLVABLE_FILES`** — allowed relative paths (`reward.py`, `action_bias.py`, `curriculum.py`, `__main__.py`, `config.py`).
- **`FROZEN_FILES`** — must never be edited by autonomous evolution (brain, IPC, PPO, imprint, STM, DB schema, `verify_launch.py`, etc.).
- **`INTERFACE_CONTRACTS`** — documented invariants; enforced in part by `verify_launch.py` after code changes.
- **`is_frozen` / `is_evolvable`** — path comparison against `REPO_ROOT`.
- **Trust:** `requires_explicit_approval` is true until the file appears in `approved_files` in `evolution_trust.json`.

---

## 15. CLI surfaces for the daemon

`main()` in `anima_outer_loop_daemon.py`:

- Default: `SessionWatcher(cfg).watch()` infinite loop.
- `--once`: single `run_cycle(cfg)`.
- `--status`: last lines of `outer_loop_cycles.jsonl`.
- `--veto`: operator helper (see `cmd_veto`).
- `--dry-run`: prints session count vs threshold and Layer 2 flags without running a cycle.
- `--slack-test`: posts sample Slack messages.

Logging: stderr + optional `~/.anima/logs/outer_loop_daemon.log` unless `INVOCATION_ID` is set (systemd).

---

## 16. Module map (quick reference)

| Concern | Primary module(s) |
|--------|---------------------|
| Watcher, Layer 1 cycle, veto, restart, Slack, digest | `anima_outer_loop_daemon.py` |
| Trace aggregation + diagnosis JSON | `anima/evaluation/diagnose.py`, `aggregator.py`, `trace_reader.py` |
| Directive staging | `anima/evaluation/outer_loop_proposer.py` |
| Rule-based recommendations | `anima/evaluation/recommender.py` |
| LLM recommendations (Layer 1) | `anima/evaluation/llm_proposer.py` |
| Persistent KB | `anima/evaluation/knowledge_base.py` |
| Layer 2 prompt + parse | `anima/evaluation/evolution_reasoner.py` |
| Apply proposal + validation | `anima/evaluation/evolution_applier.py` |
| Evolvable/frozen sets | `anima/evaluation/evolution_config.py` |

---

*Generated from repository source layout as of the doc author’s read of the files above; if behavior seems wrong, trust the code and tests over this summary.*
