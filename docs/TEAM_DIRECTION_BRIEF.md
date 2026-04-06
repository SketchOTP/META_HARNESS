# Meta-Harness — team direction brief

**Generated:** 2026-04-06  
**Repo:** `N:\META_HARNESS` (flat `meta_harness` package + `metaharness` CLI)

This document consolidates **repo layout**, **current objectives**, **directive backlog context**, **files to align on**, and **latest test results** so you can steer the team without hunting through chat.

---

## 1. Current repo tree (source-oriented)

Omitted: `.git/`, `.metaharness/` (runtime; gitignored), `__pycache__/`, `.pytest_cache/`, `.coverage`, `coverage.xml`, `meta_harness.egg-info/`, editor/tool folders (`.cursor/`, `.serena/`), and local env files.

```
META_HARNESS/
├── AGENTS.md
├── README.md
├── pyproject.toml              # protected — do not change via agent directives
├── metaharness.toml            # protected — project + harness behavior
├── .coveragerc
├── .env                        # local secrets (not committed)
│
├── __init__.py                 # package marker (flat layout → installs as meta_harness)
├── agent.py
├── cli.py                      # metaharness CLI entry
├── config.py
├── conftest.py                 # pytest: sys.modules hygiene for coverage
├── coverage_policy.py          # canonical pytest-cov fragments for flat layout
├── cursor_client.py
├── cycle.py
├── daemon.py
├── dashboard.py
├── diagnoser.py
├── directive_confidence.py
├── evidence.py
├── git_kg_sync.py
├── knowledge_graph.py
├── memory.py
├── multi_project.py
├── product_agent.py
├── proposer.py
├── research.py
├── rollback.py
├── slack_integration.py
├── vision.py
│
├── docs/
│   ├── PROJECT_OVERVIEW.md           # living architecture + dated changelog
│   ├── MULTI_PROJECT.md
│   ├── ANIMAs_META_HARNESS_EXAMPLE.md
│   ├── Meta-Harness_Operations_Reference.md   # ops playbook (some stats may lag)
│   ├── DIRECTIVES/                   # human-authored directive notes (D001–D003)
│   └── TEAM_DIRECTION_BRIEF.md     # this file
│
├── scripts/
│   ├── generate_metrics.py
│   ├── run_cycle.bat
│   ├── run_cycle.sh
│   ├── kg_sync_manual.py
│   ├── fix_memory_duplicates.py
│   └── fix_p7p8_dupes.py
│
├── templates/
│   ├── metaharness.toml
│   ├── metaharness-projects.toml
│   └── git-hook-post-commit.toml
│
├── tests/
│   ├── conftest.py             # registers flat meta_harness for pytest-cov
│   ├── __init__.py
│   └── test_*.py               # 40 test modules
│
└── unused/                     # (if present) scratch / deprecated material
```

---

## 2. Current blocker / objective

### Program goals (from `metaharness.toml`)

- **Product:** portable self-improving loop — diagnose, propose directives, implement inside scope, test, record in KG/memory; never break `run_cycle()` / `load_config()`.
- **Goals:** fix bugs/regressions, harden subprocess/platform paths, robustness; **primary metric** `test_pass_rate` **maximize**.
- **Vision (`[vision]`):** long-term autonomy narrative; north-star metric `coverage_pct`; features wanted/done lists drive product thinking.

### Active engineering focus (2026-04-06)

1. **Trustworthy coverage in the harness loop**  
   Canonical measurement is **`pytest tests/ --cov=. --cov-config=.coveragerc`** (see `coverage_policy.py`, `.coveragerc`, `README.md`).  
   - **`scripts/run_cycle.bat`** and **`scripts/run_cycle.sh`** already use `--cov=. --cov-config=.coveragerc`.  
   - **`[test].command` in root `metaharness.toml` still uses `--cov=meta_harness` only** — so **`metaharness run`** / daemon-invoked tests can produce **different** `coverage.xml` / `metrics.json` than a manual `run_cycle` workflow. Aligning this is the remaining high-leverage fix for honest diagnosis (matches latest directive **M040_auto** intent).

2. **Product layer**  
   **`[product].schedule`** in this repo is **`["08:00"]`** (local time). Latest product directive **P014_auto** proposes Slack notifications when research papers enter the implement queue; verify against current `cli.py` / `slack_integration.py` (some notify paths may already exist — treat directive as spec, verify code).

3. **Documentation drift**  
   `docs/Meta-Harness_Operations_Reference.md` still cites older test counts / product schedule in places; reconcile with **`metaharness.toml`** and this brief when updating ops docs.

### Veto / pause

- **No** `.metaharness/PENDING_VETO` present at time of this brief (no cycle waiting on human veto on disk).

---

## 3. Directive backlog

**55 files** under `.metaharness/directives/*.md` (D001_auto … D024_auto, M024_auto … M040_auto, P001_auto … P014_auto).

**Important:** YAML frontmatter on many files still says `status: pending` even after work ships — **do not** treat frontmatter alone as source of truth. Prefer:

- `metaharness status` / `metaharness product status`
- Cycle JSON under `.metaharness/cycles/`
- SQLite KG: `.metaharness/knowledge_graph.db` (`metaharness graph stats`, `graph search`, …)

**Newest generated directives (by timestamp in file):**

| ID | Layer | Generated (UTC) | Summary |
|----|--------|-------------------|---------|
| **M040_auto** | maintenance | 2026-04-06T12:51:43 | Align `run_cycle` / coverage with canonical `--cov=. --cov-config=.coveragerc`; **scripts already updated — remaining gap is `[test]` in `metaharness.toml`** (see §4). |
| **P014_auto** | product | 2026-04-06T12:50:36 | Slack alerts when research papers enter implement queue; central notifier + CLI parity. |

Older `D*_auto` / `M*_auto` / `P*_auto` files are historical proposals; triage by KG and cycle logs, not file count.

---

## 4. Relevant file contents (areas to direct)

### 4.1 `[test]` command — still mismatched vs canonical coverage

**File:** `metaharness.toml` (root)

```toml
[test]
# Run full suite with junit XML and coverage.
# generate_metrics.py reads these outputs and writes metrics.json.
command = "pytest tests/ --junitxml=.metaharness/test_results.xml --cov=meta_harness --cov-report=xml:coverage.xml --cov-report=term-missing -q"
working_dir = "."
timeout_seconds = 180
junit_xml = false  # cycle.py must NOT append --junitxml again; it's already in the command
```

**Team direction:** Changing this line is a **human / explicit maintenance** decision: it affects every `metaharness run` and must stay consistent with `cycle.py`’s behavior (`junit_xml = false` because the command already embeds junit). Recommended alignment with `coverage_policy.canonical_pytest_coverage_argv()`:

- Use `--cov=. --cov-config=.coveragerc` (and keep junit + XML report paths as today).

**Note:** `pyproject.toml` is protected from autonomous agents; **`metaharness.toml` is also protected from agent scope** in this repo — a person should edit `[test].command` when the team agrees.

### 4.2 Canonical policy (reference)

**File:** `coverage_policy.py` (excerpt)

```python
# Use `--cov=.` (not `--cov=meta_harness` alone) so pytest-cov traces the
# working tree correctly on this layout; see the `.coveragerc` header.

COVERAGE_SOURCE = "."
COVERAGE_FLAG = f"--cov={COVERAGE_SOURCE}"
COVERAGE_CONFIG_FLAG = f"--cov-config={COVERAGE_CONFIG_FILE}"
```

### 4.3 Self-host scripts — already canonical

**File:** `scripts/run_cycle.bat` (pytest line)

```bat
pytest tests/ --junitxml=.metaharness\test_results.xml --cov=. --cov-config=.coveragerc --cov-report=xml:coverage.xml -q
```

**File:** `scripts/run_cycle.sh` — same flags (`--cov=.`, `--cov-config=.coveragerc`).

### 4.4 Latest product directive (excerpt) — P014_auto

**File:** `.metaharness/directives/P014_auto.md` (title + intent)

- **Goal:** Proactive Slack when a paper is queued for implementation; unify CLI (`research eval`) and Slack (`_research_background`) paths; optional `[slack] notify_research_queue` in config template (not root `metaharness.toml` if protected).

Direct implementation work: **`slack_integration.py`**, **`cli.py`**, **`config.py` / `SlackConfig`**, **`templates/metaharness.toml`**.

---

## 5. Latest test output (stability / CI)

**Command:** `py -3 -m pytest tests/ -q --tb=no`  
**Date:** 2026-04-06  
**Result:** **439 passed, 2 skipped** in ~14.5s — **no failures.**

```
........................................................................ [ 16%]
........................................................................ [ 32%]
.............................s....................................s....... [ 48%]
........................................................................ [ 65%]
........................................................................ [ 81%]
........................................................................ [ 97%]
.........                                                                [100%]
439 passed, 2 skipped in 14.54s
```

**Current `metrics.json` (after last local generation)** reports e.g. `test_pass_rate: 1.0`, `test_count: 441`, `coverage_pct: 33.0` — regenerate with `py scripts/generate_metrics.py` after pytest + `coverage.xml` when baselining.

---

## Quick links for operators

| Need | Command / path |
|------|----------------|
| Recent maintenance cycles | `metaharness status` |
| Product cycles | `metaharness product status` |
| Human work → KG | `metaharness sync ...` (see README + Operations Reference) |
| KG query | `metaharness graph stats`, `metaharness graph search "..."` |
| Full CLI | `metaharness --help` |

---

*Refresh this brief after major merges, config changes, or when directive/KM state materially changes.*
