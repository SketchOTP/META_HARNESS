#!/usr/bin/env python3
"""
scripts/generate_metrics.py

Read pytest junit XML and coverage XML, write metrics.json.

Usage:
    python scripts/generate_metrics.py

Reads:
    .metaharness/test_results.xml   (pytest --junitxml)
    coverage.xml                    (pytest --cov-report=xml)

Writes:
    metrics.json
"""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_test_results() -> dict:
    xml_path = ROOT / ".metaharness" / "test_results.xml"
    if not xml_path.exists():
        print(f"[metrics] No test results at {xml_path} — run pytest first", file=sys.stderr)
        return {}

    try:
        tree = ET.parse(xml_path)
        suite = tree.getroot()
        if suite.tag != "testsuite":
            suite = suite.find("testsuite") or suite

        total = int(suite.get("tests", 0))
        failed = int(suite.get("failures", 0))
        errored = int(suite.get("errors", 0))
        skipped = int(suite.get("skipped", 0))
        duration = float(suite.get("time", 0.0))

        # Skipped tests are not failures; rate = passed / executed (non-skipped).
        passing = total - failed - errored - skipped
        denom = total - skipped
        rate = round(passing / denom, 4) if denom > 0 else 0.0

        return {
            "test_pass_rate": rate,
            "test_count": total,
            "test_passed": passing,
            "test_failed": failed + errored,
            "test_skipped": skipped,
            "test_duration_s": round(duration, 2),
        }
    except Exception as e:
        print(f"[metrics] Failed to parse test results: {e}", file=sys.stderr)
        return {}


def read_coverage() -> dict:
    xml_path = ROOT / "coverage.xml"
    if not xml_path.exists():
        return {}

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        line_rate = float(root.get("line-rate", 0))
        branch_rate = float(root.get("branch-rate", 0))

        # Per-file coverage for the lowest-covered files
        low_coverage = []
        for cls in root.findall(".//class"):
            fname = cls.get("filename", "")
            rate = float(cls.get("line-rate", 1.0))
            if "meta_harness" in fname and rate < 0.8:
                low_coverage.append({"file": fname, "pct": round(rate * 100, 1)})
        low_coverage.sort(key=lambda x: x["pct"])

        return {
            "coverage_pct": round(line_rate * 100, 2),
            "branch_coverage_pct": round(branch_rate * 100, 2),
            "low_coverage_files": low_coverage[:5],
        }
    except Exception as e:
        print(f"[metrics] Failed to parse coverage: {e}", file=sys.stderr)
        return {}


def main() -> None:
    metrics: dict = {}
    metrics.update(read_test_results())
    metrics.update(read_coverage())

    if not metrics:
        print("[metrics] No data collected — nothing written.", file=sys.stderr)
        sys.exit(1)

    out = ROOT / "metrics.json"
    out.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"[metrics] Written to {out}")
    for k, v in metrics.items():
        if isinstance(v, (int, float)):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
