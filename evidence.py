"""
meta_harness/evidence.py
Collects runtime evidence from the target project for diagnosis.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .config import HarnessConfig


@dataclass
class MetricsBundle:
    current: dict[str, float] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


@dataclass
class ASTEvidence:
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    syntax_errors: list[str] = field(default_factory=list)
    long_functions: list[str] = field(default_factory=list)


@dataclass
class TestEvidence:
    passed: int = 0
    failed: int = 0
    failed_names: list[str] = field(default_factory=list)


@dataclass
class DepsEvidence:
    packages: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    collected_at: str = ""
    log_tail: str = ""
    metrics: MetricsBundle = field(default_factory=MetricsBundle)
    # Snippet from .metaharness/last_cursor_failure.txt (Cursor CLI / agent failures)
    cursor_cli_failure_excerpt: str = ""
    git_diff: str = ""
    # Paths touched in recent commits (newest-first, deduped) for agent ANALYZE prioritization
    git_recent_paths: list[str] = field(default_factory=list)
    file_tree: str = ""
    test_results: str = ""
    cycle_history: str = ""
    error_patterns: list[dict[str, Any]] = field(default_factory=list)
    ast: ASTEvidence = field(default_factory=ASTEvidence)
    tests: TestEvidence = field(default_factory=TestEvidence)
    deps: DepsEvidence = field(default_factory=DepsEvidence)


def _glob_files(root: Path, patterns: list[str], max_age_hours: int) -> list[Path]:
    cutoff = time.time() - max_age_hours * 3600
    found = []
    for pattern in patterns:
        for p in root.glob(pattern):
            if p.is_file() and p.stat().st_mtime >= cutoff:
                found.append(p)
    return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)


def _extract_error_patterns(log_text: str) -> list[dict[str, Any]]:
    lines = log_text.splitlines()
    pat = re.compile(
        r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\s*:\s*(.+)$"
    )
    counts: Counter[str] = Counter()
    samples: dict[str, str] = {}
    for line in lines:
        m = pat.search(line.strip())
        if m:
            key = f"{m.group(1)}: {m.group(2).strip()}"
            counts[key] += 1
            samples.setdefault(key, line.strip())
    return [{"pattern": k, "count": c, "sample": samples[k]} for k, c in counts.most_common()]


def _read_log_tail(files: list[Path], max_lines: int) -> str:
    lines = []
    for f in files:
        try:
            text = f.read_text(errors="replace")
            file_lines = text.splitlines()
            lines.append(f"=== {f.name} (last {min(len(file_lines), max_lines)} lines) ===")
            lines.extend(file_lines[-max_lines:])
        except OSError:
            pass
    combined = "\n".join(lines)
    if len(combined) > 30_000:
        combined = combined[-30_000:]
    return combined


def _read_metrics_with_history(
    files: list[Path],
    harness_dir: Path,
) -> MetricsBundle:
    bundle = MetricsBundle()
    hist_path = harness_dir / "metrics_history.jsonl"
    if hist_path.exists():
        try:
            for line in hist_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    bundle.history.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass

    merged: dict[str, float] = {}
    for f in sorted(files, key=lambda p: p.stat().st_mtime):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, (int, float)):
                        merged[k] = float(v)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    bundle.current = merged

    for i in range(1, len(bundle.history)):
        prev = bundle.history[i - 1]
        cur = bundle.history[i]
        for key in set(prev) | set(cur):
            if key == "ts":
                continue
            try:
                a = float(prev.get(key, 0))
                b = float(cur.get(key, 0))
            except (TypeError, ValueError):
                continue
            if a == 0:
                continue
            if abs(b - a) / abs(a) > 0.20:
                bundle.anomalies.append(f"{key} jumped {a:.4f} → {b:.4f}")
    return bundle


def _git_diff(root: Path) -> str:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~3", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        diff = result.stdout.strip()
        log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        parts = []
        if log.stdout.strip():
            parts.append("Recent commits:\n" + log.stdout.strip())
        if diff:
            parts.append("Recent diff stat:\n" + diff)
        return "\n\n".join(parts)
    except Exception:
        return "(git not available)"


def _git_recent_paths(root: Path, max_commits: int = 20) -> list[str]:
    """Paths appearing in recent commits, newest commits first (deduped)."""
    try:
        import subprocess

        r = subprocess.run(
            ["git", "log", f"-{max_commits}", "--name-only", "--pretty=format:"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        order: list[str] = []
        seen: set[str] = set()
        for line in r.stdout.splitlines():
            line = line.strip().replace("\\", "/")
            if not line or line in seen:
                continue
            seen.add(line)
            order.append(line)
        return order
    except Exception:
        return []


def _file_tree(root: Path, modifiable_patterns: list[str], protected: list[str]) -> str:
    lines = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if any(part.startswith(".") for part in p.parts):
            continue
        if "__pycache__" in rel or ".pyc" in rel:
            continue
        protected_match = any(fnmatch.fnmatch(rel, pat) for pat in protected)
        if protected_match:
            continue
        in_scope = any(fnmatch.fnmatch(rel, pat) for pat in modifiable_patterns)
        tag = " [in scope]" if in_scope else ""
        lines.append(f"  {rel}{tag}")
    return "\n".join(lines[:200])


def _cycle_history(cfg: HarnessConfig, n: int = 5) -> str:
    logs: list[Path] = []
    for d in (cfg.maintenance_cycles_dir, cfg.cycles_dir):
        if d.is_dir():
            logs.extend(d.glob("*.json"))
    logs = sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True)
    lines = []
    for log in logs[:n]:
        try:
            data = json.loads(log.read_text(encoding="utf-8"))
            ts = data.get("timestamp", "?")
            directive = data.get("directive", "?")
            status = data.get("status", "?")
            delta = data.get("delta", None)
            delta_str = f" | delta={delta:+.4f}" if delta is not None else ""
            lines.append(f"  {ts} | {directive} | {status}{delta_str}")
        except Exception:
            pass
    return "\n".join(lines) if lines else "(no previous cycles)"


def _collect_ast_for_path(path: Path, ev: Evidence) -> None:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        ev.ast.syntax_errors.append(f"{path}: {e.msg}")
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            ev.ast.functions.append(node.name)
            end = getattr(node, "end_lineno", None) or node.lineno
            if end - node.lineno + 1 > 50:
                ev.ast.long_functions.append(f"{path.name}:{node.name}")
        elif isinstance(node, ast.ClassDef):
            ev.ast.classes.append(node.name)


def _collect_ast(cfg: HarnessConfig, ev: Evidence) -> None:
    root = cfg.project_root
    seen: set[Path] = set()
    for pat in cfg.scope.modifiable:
        if ".py" not in pat:
            continue
        try:
            for p in root.glob(pat):
                if p.is_file() and p.suffix.lower() == ".py" and p not in seen:
                    seen.add(p)
                    _collect_ast_for_path(p, ev)
        except OSError:
            pass


def _parse_junit_xml(xml_text: str) -> TestEvidence:
    te = TestEvidence()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return te
    if root.tag == "testsuites":
        for ts in root.findall("testsuite"):
            _accumulate_testsuite(ts, te)
    elif root.tag == "testsuite":
        _accumulate_testsuite(root, te)
    return te


def _accumulate_testsuite(suite: ET.Element, te: TestEvidence) -> None:
    for case in suite.findall("testcase"):
        failure = case.find("failure") is not None or case.find("error") is not None
        if failure:
            te.failed += 1
            name = case.get("name") or "unknown"
            te.failed_names.append(name)
        else:
            te.passed += 1


def _parse_pytest_summary(text: str) -> TestEvidence:
    te = TestEvidence()
    m = re.search(r"(\d+)\s+passed", text)
    if m:
        te.passed = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", text)
    if m:
        te.failed = int(m.group(1))
    return te


def _pytest_log_indicates_incomplete_run(text: str) -> bool:
    """True when pytest did not complete a normal test run (collection/import errors).

    Conservative: avoid false positives on skipped-only runs or ModuleNotFoundError
    raised inside a passing test body. Prefer explicit pytest collection/session signals.
    """
    if not text.strip():
        return False
    if "ERROR collecting" in text:
        return True
    if "ImportError while importing test module" in text:
        return True
    return False


def _reconcile_metrics_with_pytest_tail(bundle: MetricsBundle, test_results_tail: str) -> None:
    """If junit-backed metrics claim success but the live pytest tail shows failure, clamp metrics.

    Stale metrics.json from an older green run can disagree with last_test_output.txt; the
    diagnoser should not see a perfect test_pass_rate when the latest run never executed tests.
    """
    if not _pytest_log_indicates_incomplete_run(test_results_tail):
        return
    bundle.current["test_pass_rate"] = 0.0
    # Align numeric hints if present so counts do not imply all green.
    if "test_failed" in bundle.current:
        bundle.current["test_failed"] = max(float(bundle.current["test_failed"]), 1.0)
    if "test_count" in bundle.current:
        bundle.current["test_count"] = max(float(bundle.current["test_count"]), 1.0)
    bundle.anomalies.append(
        "Metrics reconciled against last_test_output.txt: incomplete pytest run "
        "(e.g. collection/import error); test_pass_rate set to 0.0 (stale junit/metrics may disagree)."
    )


def _collect_tests(cfg: HarnessConfig, ev: Evidence) -> None:
    junit = cfg.harness_dir / "test_results.xml"
    if junit.exists():
        try:
            te = _parse_junit_xml(junit.read_text(encoding="utf-8", errors="replace"))
            ev.tests = te
        except OSError:
            pass
    out_path = cfg.harness_dir / "last_test_output.txt"
    if out_path.exists():
        try:
            raw = out_path.read_text(encoding="utf-8", errors="replace")
            ev.test_results = raw[-5000:]
            if _pytest_log_indicates_incomplete_run(raw):
                ev.tests = TestEvidence(passed=0, failed=1, failed_names=["collection"])
            elif ev.tests.passed == 0 and ev.tests.failed == 0:
                fallback = _parse_pytest_summary(raw)
                if fallback.passed or fallback.failed:
                    ev.tests = fallback
        except OSError:
            pass


def _collect_deps(root: Path, ev: Evidence) -> None:
    req = root / "requirements.txt"
    if not req.is_file():
        return
    try:
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[<>=\[]", line, 1)[0].strip()
            if pkg:
                ev.deps.packages.append(pkg)
    except OSError:
        pass


def collect(cfg: HarnessConfig) -> Evidence:
    root = cfg.project_root

    log_files = _glob_files(root, cfg.evidence.log_patterns, cfg.evidence.max_age_hours)
    metrics_files = _glob_files(root, cfg.evidence.metrics_patterns, cfg.evidence.max_age_hours)

    test_result_path = cfg.harness_dir / "last_test_output.txt"
    test_results = ""
    if test_result_path.exists():
        try:
            test_results = test_result_path.read_text(errors="replace")[-5000:]
        except OSError:
            pass

    log_tail = _read_log_tail(log_files, cfg.evidence.max_log_lines)
    metrics = _read_metrics_with_history(metrics_files, cfg.harness_dir)

    ev = Evidence(
        collected_at=datetime.utcnow().isoformat() + "Z",
        log_tail=log_tail,
        metrics=metrics,
        git_diff=_git_diff(root),
        git_recent_paths=_git_recent_paths(root),
        file_tree=_file_tree(root, cfg.scope.modifiable, cfg.scope.protected),
        test_results=test_results,
        cycle_history=_cycle_history(cfg),
    )
    _reconcile_metrics_with_pytest_tail(ev.metrics, test_results)
    ev.error_patterns = _extract_error_patterns(log_tail)
    _collect_ast(cfg, ev)
    _collect_tests(cfg, ev)
    _collect_deps(root, ev)

    fail_path = cfg.harness_dir / "last_cursor_failure.txt"
    if fail_path.exists():
        try:
            raw_fail = fail_path.read_text(encoding="utf-8", errors="replace")
            if len(raw_fail) > 4000:
                ev.cursor_cli_failure_excerpt = raw_fail[:4000] + "\n...(truncated)"
            else:
                ev.cursor_cli_failure_excerpt = raw_fail
        except OSError:
            pass

    return ev


def has_sufficient_evidence(ev: Evidence, min_items: int) -> bool:
    items = 0
    if ev.log_tail.strip():
        items += 1
    if ev.metrics.current:
        items += 1
    # Raw pytest output or junit-derived counts (e.g. after run_cycle / pytest --junitxml)
    if ev.test_results.strip() or ev.tests.passed or ev.tests.failed:
        items += 1
    return items >= min_items


def to_prompt_sections(ev: Evidence, *, max_chars: int = 8000) -> str:
    parts: list[tuple[int, str]] = []

    def add(priority: int, title: str, body: str) -> None:
        if body.strip():
            parts.append((priority, f"### {title}\n{body.strip()}"))

    fail_lines = ""
    if ev.tests.failed_names:
        fail_lines = "Failed: " + ", ".join(ev.tests.failed_names[:20])
    elif ev.tests.failed:
        fail_lines = f"Failed count: {ev.tests.failed}"
    if ev.tests.passed or ev.tests.failed:
        add(0, "Test results", f"passed={ev.tests.passed} failed={ev.tests.failed}\n{fail_lines}")

    add(1, "Test output (tail)", ev.test_results[-2000:] if ev.test_results else "")

    if ev.error_patterns:
        lines = "\n".join(f"- {p['pattern']} (×{p['count']})" for p in ev.error_patterns[:15])
        add(2, "Error patterns", lines)

    if ev.metrics.current:
        add(3, "Metrics", json.dumps(ev.metrics.current, indent=2))
    if ev.metrics.anomalies:
        add(4, "Metric anomalies", "\n".join(ev.metrics.anomalies))

    if ev.ast.syntax_errors:
        add(5, "AST syntax errors", "\n".join(ev.ast.syntax_errors[:20]))
    if ev.ast.long_functions:
        add(6, "Long functions", "\n".join(ev.ast.long_functions[:20]))
    fn_preview = ", ".join(ev.ast.functions[:30])
    if fn_preview:
        add(7, "Functions (sample)", fn_preview)
    cls_preview = ", ".join(ev.ast.classes[:30])
    if cls_preview:
        add(8, "Classes (sample)", cls_preview)

    if ev.deps.packages:
        add(9, "Dependencies", "\n".join(ev.deps.packages[:50]))

    add(10, "Runtime logs", ev.log_tail[-2500:] if ev.log_tail else "")

    parts.sort(key=lambda x: x[0])
    out = "\n\n".join(p[1] for p in parts)
    if len(out) <= max_chars:
        return out
    return out[: max_chars - 20] + "\n…(truncated)"
