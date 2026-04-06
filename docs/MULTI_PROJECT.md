# Multi-project daemon

One Meta-Harness process can run **maintenance cycles sequentially** for several registered repositories, each with its own `metaharness.toml` and **isolated** `.metaharness/` state (including `knowledge_graph.db`).

## When to use it

- You keep a **control-plane** directory (often a small meta-repo or your home config folder) that lists project roots in `metaharness-projects.toml`.
- You run `metaharness daemon` from that tree (or pass `--projects-file`). The daemon loads each project’s config with `load_config(project_root)` as today, so paths and KG stay per project.

## Single-project mode (unchanged)

If there is **no** `metaharness-projects.toml` in the walk-up path, the daemon behaves as before: one `metaharness.toml` from `--dir` / walk-up, one project.

## Registry file

- **Name:** `metaharness-projects.toml`
- **Location:** Typically next to a control-plane `metaharness.toml`, or any directory you use as the anchor when starting the daemon. The CLI walks **up** from `--dir` (default `.`) until it finds this file.
- **Explicit path:** `metaharness daemon --projects-file /path/to/metaharness-projects.toml` (and `metaharness status --projects-file ...` to show the registry without discovery).

Copy `templates/metaharness-projects.toml` as a starting point; you do not need to edit `pyproject.toml`.

### Schema (minimal)

- **`[[projects]]`** (repeatable)
  - **`id`** — Short slug (unique).
  - **`root`** — Path to a directory that contains `metaharness.toml` (relative to the registry file or absolute).
  - **`enabled`** — Optional, default `true`.
  - **`label`** — Optional; shown in logs.

- **`product_project_id`** (optional, top-level) — If set, the daemon runs the **product agent** background loop only for that project id. If omitted, the product thread is not started in multi-project mode (use `metaharness product run --dir ...` per repo as needed).

## Pause behavior

Two layers:

1. **Control plane (whole daemon):** Create an empty file `DAEMON_PAUSE` in the **same directory as** `metaharness-projects.toml`. While it exists, the multi-project daemon waits before starting the next **full round** (all projects). Remove the file to resume.

2. **Per project:** Each repo’s existing `.metaharness/DAEMON_PAUSE` is honored **before that project’s cycle**, same as single-project mode.

## Timing between rounds

After one full pass over all enabled projects, the daemon waits using the **`[cycle]`** settings from the **first enabled** project’s `metaharness.toml` (`interval_seconds` or `schedule`). Configure that project accordingly so the between-round delay matches what you want.

## Slack

Slack Socket Mode **autostart is disabled** in multi-project mode (one OS process, multiple Slack app configurations). Use per-project `metaharness slack listen` or your own process model if you need Socket Mode per app.

## Isolation

Each `root` must resolve to its own project tree; each `load_config` keeps separate `.metaharness/` dirs and `knowledge_graph.db` under that tree. There is **no** shared memory or cross-project KG in this slice.
