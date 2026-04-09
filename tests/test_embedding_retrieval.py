from __future__ import annotations

from pathlib import Path

import pytest

from meta_harness.config import EmbeddingConfig, HarnessConfig
from meta_harness import embedding_retrieval as er


def test_retrieve_compact_lines_disabled(tmp_path: Path):
    cfg = HarnessConfig(project_root=tmp_path)
    cfg.embedding = EmbeddingConfig(enabled=False)
    from meta_harness.evidence import Evidence

    assert er.retrieve_compact_lines(cfg, Evidence()) == []


def test_retrieve_compact_lines_empty_index(tmp_path: Path):
    cfg = HarnessConfig(project_root=tmp_path)
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    cfg.embedding = EmbeddingConfig(enabled=True)
    from meta_harness.evidence import Evidence

    ev = Evidence()
    ev.tests.failed_names = ["test_foo"]
    ev.test_results = "AssertionError: x" * 5
    assert er.retrieve_compact_lines(cfg, ev) == []


def test_index_directive_body_skips_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = HarnessConfig(project_root=tmp_path)
    cfg.embedding = EmbeddingConfig(enabled=False)
    called = []

    monkeypatch.setattr(er, "embed_batch", lambda c, t: called.append(t) or [])
    er.index_directive_body(cfg, "D1", "body text")
    assert len(called) == 0
