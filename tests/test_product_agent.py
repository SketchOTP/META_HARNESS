from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meta_harness.config import load_config
from meta_harness.cursor_client import CursorResponse
from meta_harness import product_agent as product_agent_mod
from meta_harness.product_agent import (
    ProductDiagnosis,
    _extract_product_directive_title,
    _next_product_directive_id,
    diagnose,
    product_effective_scope,
    propose,
)
from meta_harness.proposer import Directive
from meta_harness.agent import AgentResult, FileChange


@pytest.fixture
def prod_cfg(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n"
        "[product]\nenabled = true\nveto_seconds = 0\n[memory]\nenabled = false\n",
        encoding="utf-8",
    )
    return tmp_path


def test_extract_product_title_rejects_bare_id_like_d1():
    raw = "# DIRECTIVE: D1\n\n# OAuth login flow\n\n" + "word " * 20
    assert _extract_product_directive_title(raw) == "OAuth login flow"
    raw2 = "# DIRECTIVE: D1\n\n" + "word " * 20
    assert "Untitled" in _extract_product_directive_title(raw2)


def test_strip_yaml_frontmatter_before_title():
    raw = "---\nid: D1\n---\n\n# DIRECTIVE: Add export API\n\n" + "word " * 20
    assert _extract_product_directive_title(raw) == "Add export API"


def test_product_effective_scope_merges_protected(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        "[scope]\nprotected = [\"a.toml\"]\n[product]\nprotected = [\"b.toml\"]\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    mod, prot = product_effective_scope(cfg)
    assert "a.toml" in prot and "b.toml" in prot


def test_diagnose_parses_json_response(prod_cfg: Path):
    cfg = load_config(prod_cfg)
    from meta_harness import evidence as ev_mod

    ev = ev_mod.Evidence(log_tail="x", metrics=ev_mod.MetricsBundle(current={"m": 1.0}))
    payload = {
        "summary": "Ship faster",
        "existing_features": ["cli"],
        "missing_features": ["api"],
        "user_value_gaps": ["docs"],
        "next_build_targets": ["auth"],
        "maintenance_blockers": [],
    }
    fake = CursorResponse(success=True, data=payload, raw="")
    with patch.object(product_agent_mod.cursor_client, "json_call", return_value=fake):
        dx = diagnose(cfg, ev, kg=None)
    assert dx.summary == "Ship faster"
    assert "auth" in dx.next_build_targets


def test_next_product_directive_id_ignores_maintenance_and_requires_p_auto_suffix(
    tmp_path: Path,
):
    d = tmp_path / "directives"
    d.mkdir()
    (d / "D099_auto.md").write_text("x", encoding="utf-8")
    (d / "M001_auto.md").write_text("x", encoding="utf-8")
    (d / "P002_auto.md").write_text("x", encoding="utf-8")
    (d / "P005_notes.md").write_text("x", encoding="utf-8")
    assert _next_product_directive_id(d) == "P003_auto"
    (d / "P001_auto.md").write_text("x", encoding="utf-8")
    assert _next_product_directive_id(d) == "P003_auto"


def test_propose_writes_p_directive(prod_cfg: Path):
    cfg = load_config(prod_cfg)
    dx = ProductDiagnosis(
        summary="s",
        next_build_targets=["Add feature X"],
    )
    body = "# DIRECTIVE: Feature X\n\n" + "detail " * 40
    fake = MagicMock(success=True, raw=body, error="")
    with patch.object(product_agent_mod.cursor_client, "agent_call", return_value=fake):
        d = propose(cfg, dx, kg=None)
    assert d.id.startswith("P")
    assert d.path.exists()
    assert "Feature X" in d.title or "feature" in d.title.lower()


def test_propose_strips_markdown_fence_before_title(prod_cfg: Path):
    cfg = load_config(prod_cfg)
    dx = ProductDiagnosis(summary="s", next_build_targets=["t"])
    inner = "# DIRECTIVE: Fenced title\n\n" + "word " * 30
    body = "```markdown\n" + inner + "\n```"
    fake = MagicMock(success=True, raw=body, error="")
    with patch.object(product_agent_mod.cursor_client, "agent_call", return_value=fake):
        d = propose(cfg, dx, kg=None)
    assert d.title == "Fenced title"
    assert d.id.startswith("P")


def test_run_product_cycle_completed(prod_cfg: Path, monkeypatch: pytest.MonkeyPatch):
    from meta_harness import product_agent as pa
    from meta_harness import evidence as ev_mod

    cfg = load_config(prod_cfg)

    monkeypatch.setattr(
        pa.evidence,
        "collect",
        lambda c: ev_mod.Evidence(
            log_tail="log",
            metrics=ev_mod.MetricsBundle(current={"x": 1.0}),
        ),
    )
    monkeypatch.setattr(pa.evidence, "has_sufficient_evidence", lambda ev, n: True)
    monkeypatch.setattr(pa, "_read_primary_metric", lambda c: None)
    monkeypatch.setattr(
        pa,
        "diagnose",
        lambda c, ev, kg=None: ProductDiagnosis(
            summary="s",
            next_build_targets=["t"],
        ),
    )
    dpath = cfg.directives_dir / "P001_auto.md"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dr = Directive(id="P001_auto", path=dpath, title="T", content="c" * 60)
    monkeypatch.setattr(pa, "propose", lambda c, dx, kg=None: dr)
    monkeypatch.setattr(
        pa.agent,
        "run",
        lambda *a, **k: AgentResult(
            success=True,
            changes=[FileChange("write", "n.py", "")],
            phases_completed=1,
        ),
    )
    monkeypatch.setattr(pa, "_run_tests", lambda c: (True, "ok"))
    monkeypatch.setattr(pa, "_restart_project", lambda c: None)
    monkeypatch.setattr(pa.mem_module, "persist_cycle_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(pa.vision_mod, "evolve_vision", lambda *a, **k: {})

    out = pa.run_product_cycle(cfg)
    assert out.status.value == "COMPLETED"
    logs = list(cfg.product_cycles_dir.glob("*.json"))
    assert logs
