"""
scripts/fix_memory_duplicates.py

Targeted fix for duplicate COMPLETED entries in project_memory.json.
Removes the EARLIER duplicate of P003_auto and research_agent COMPLETED
records — preserves the later (metaharness sync) entry and all real cycles
including P006_auto.

Safe approach: removes by matching exact ID + status, not by position.

Run from repo root:
    py scripts\\fix_memory_duplicates.py
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PATH = ROOT / ".metaharness" / "memory" / "project_memory.json"
BACKUP_PATH = MEMORY_PATH.with_suffix(".json.bak2")

DUPLICATE_IDS = {"P003_auto", "research_agent"}

OVERCOUNTED_FILES = [
    "dashboard.py",
    "tests/test_dashboard.py",
    "research.py",
    "config.py",
    "slack_integration.py",
    "product_agent.py",
    "cli.py",
    "pyproject.toml",
    "tests/test_research.py",
]


def main() -> None:
    if not MEMORY_PATH.exists():
        print(f"ERROR: {MEMORY_PATH} not found.")
        return

    shutil.copy2(MEMORY_PATH, BACKUP_PATH)
    print(f"Backup written to {BACKUP_PATH}")

    mem = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    directives: list[dict] = mem["directives"]

    # For each duplicate ID, find all COMPLETED entries and remove the FIRST one
    removed_count = 0
    for dup_id in DUPLICATE_IDS:
        completed_indices = [
            i for i, d in enumerate(directives)
            if d["id"] == dup_id and d["status"] == "COMPLETED"
        ]
        print(f"{dup_id}: found {len(completed_indices)} COMPLETED entries at indices {completed_indices}")
        if len(completed_indices) >= 2:
            idx_to_remove = completed_indices[0]
            removed = directives.pop(idx_to_remove)
            print(f"  Removed earlier duplicate: id={removed['id']} status={removed['status']} ts={removed['ts']}")
            removed_count += 1

    if removed_count != 2:
        print(f"WARNING: expected to remove 2 duplicates, removed {removed_count}. Aborting.")
        return

    mem["directives"] = directives

    # Fix metric_trajectory — remove first occurrence of each duplicate ID
    traj: list[list] = mem["metric_trajectory"]
    for dup_id in DUPLICATE_IDS:
        dup_indices = [i for i, t in enumerate(traj) if t[0] == dup_id]
        print(f"metric_trajectory {dup_id}: {len(dup_indices)} entries at {dup_indices}")
        if len(dup_indices) >= 2:
            traj.pop(dup_indices[0])
            print(f"  Removed earlier trajectory entry for {dup_id}")
    mem["metric_trajectory"] = traj

    # Fix counters
    before_total = mem["total_cycles"]
    before_completed = mem["completed"]
    mem["total_cycles"] -= 2
    mem["completed"] -= 2
    print(f"total_cycles: {before_total} → {mem['total_cycles']}")
    print(f"completed:    {before_completed} → {mem['completed']}")

    # Fix file_touches
    for f in OVERCOUNTED_FILES:
        if f in mem["file_touches"]:
            before = mem["file_touches"][f]
            mem["file_touches"][f] -= 1
            if mem["file_touches"][f] <= 0:
                del mem["file_touches"][f]
            print(f"  file_touches[{f}]: {before} → {mem['file_touches'].get(f, 'deleted')}")

    # Fix file_successes
    for f in OVERCOUNTED_FILES:
        if f in mem["file_successes"]:
            before = mem["file_successes"][f]
            mem["file_successes"][f] -= 1
            if mem["file_successes"][f] <= 0:
                del mem["file_successes"][f]
            print(f"  file_successes[{f}]: {before} → {mem['file_successes'].get(f, 'deleted')}")

    mem["last_updated"] = datetime.utcnow().isoformat() + "Z"
    MEMORY_PATH.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nDone. Written to {MEMORY_PATH}")
    print(f"Final: total_cycles={mem['total_cycles']}, completed={mem['completed']}, "
          f"failed={mem['failed']}, vetoed={mem['vetoed']}")
    print(f"Directive count: {len(mem['directives'])}")


if __name__ == "__main__":
    main()