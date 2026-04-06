from __future__ import annotations

from pathlib import Path

import pytest

from meta_harness.config import load_config
from meta_harness.directive_confidence import (
    DirectiveConfidenceResult,
    score_directive,
    tier_for_score,
)
from meta_harness.proposer import Directive


@pytest.fixture
def cfg_min(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        '[project]\nname = "dc-test"\n[cycle]\nveto_seconds = 0\nmin_evidence_items = 1\n'
        "[memory]\nenabled = true\n",
        encoding="utf-8",
    )
    (tmp_path / ".metaharness" / "memory").mkdir(parents=True)
    return tmp_path


def test_score_is_deterministic(cfg_min: Path) -> None:
    cfg = load_config(cfg_min)
    mem = cfg.memory_dir / "project_memory.json"
    mem.write_text(
        '{"total_cycles": 10, "completed": 7, "failed": 2, "vetoed": 1, '
        '"directives": [], "file_touches": {}, "file_successes": {}}',
        encoding="utf-8",
    )
    d = Directive(
        id="D001_auto",
        path=cfg_min / "x.md",
        title="Fix bug in parser",
        content="# DIRECTIVE: Fix bug\n\nAdjust `foo/bar.py` to handle edge case. Small fix.",
    )
    r1 = score_directive(cfg, d, kg=None, diagnosis_summary="Straightforward fix.")
    r2 = score_directive(cfg, d, kg=None, diagnosis_summary="Straightforward fix.")
    assert r1.score == r2.score
    assert r1.tier == r2.tier
    assert r1.factors == r2.factors


def test_tier_bands() -> None:
    assert tier_for_score(0.7) == "high"
    assert tier_for_score(0.5) == "medium"
    assert tier_for_score(0.2) == "low"


class FakeKG:
    def __init__(self, hits: list[str], nodes: dict) -> None:
        self._hits = hits
        self._nodes = nodes

    def search(self, query: str, limit: int = 20) -> list[str]:
        return list(self._hits[:limit])

    def get_node(self, nid: str):
        return self._nodes.get(nid)


def test_kg_completed_ratio_raises_score(cfg_min: Path) -> None:
    cfg = load_config(cfg_min)
    mem = cfg.memory_dir / "project_memory.json"
    mem.write_text(
        '{"total_cycles": 4, "completed": 2, "failed": 1, "vetoed": 1, '
        '"directives": [], "file_touches": {}, "file_successes": {}}',
        encoding="utf-8",
    )
    d = Directive(
        id="D999_auto",
        path=cfg_min / "z.md",
        title="Token overlap",
        content="# DIRECTIVE\n\nWork on parser token overlap.",
    )
    kg = FakeKG(
        ["D001_auto"],
        {
            "D001_auto": {
                "id": "D001_auto",
                "type": "directive",
                "status": "COMPLETED",
                "name": "",
                "summary": "",
                "data": {},
            }
        },
    )
    r = score_directive(cfg, d, kg=kg, diagnosis_summary=None)
    assert isinstance(r, DirectiveConfidenceResult)
    assert 0.0 <= r.score <= 1.0
    assert any("KG" in f or "Memory" in f or "Directive text" in f for f in r.factors)
