from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meta_harness.config import HarnessConfig, ProjectConfig, ScopeConfig
from meta_harness.diagnoser import Diagnosis
from meta_harness import proposer as proposer_mod


def test_directive_titles_similar():
    assert proposer_mod._directive_titles_similar("Fix Slack buttons", "fix slack buttons", 0.5)
    assert proposer_mod._directive_titles_similar("A long title here", "A long title here!", 0.9)
    assert not proposer_mod._directive_titles_similar("Alpha", "Omega", 0.9)


def _cfg(tmp_path: Path) -> HarnessConfig:
    cfg = HarnessConfig(project_root=tmp_path)
    cfg.project = ProjectConfig(name="p", description="d")
    cfg.goals.objectives = ["g"]
    cfg.scope = ScopeConfig(modifiable=["**/*.py"], protected=["secret/**"])
    cfg.directives_dir.mkdir(parents=True, exist_ok=True)
    cfg.memory.enabled = False
    return cfg


def test_run_writes_directive_file(tmp_path: Path):
    cfg = _cfg(tmp_path)
    diagnosis = Diagnosis(
        summary="needs work",
        strengths=["s"],
        weaknesses=["w1"],
        patterns=["p"],
        opportunities=["do X"],
        risk_areas=["r"],
    )
    body = (
        "# DIRECTIVE: Add tests\n\n"
        "Do the thing.\n" + "x" * 60
    )
    fake = MagicMock(success=True, raw=body, error="")
    with patch.object(proposer_mod.cursor_client, "agent_call", return_value=fake):
        d = proposer_mod.run(cfg, diagnosis, kg=None)
    assert d.path.exists()
    text = d.path.read_text(encoding="utf-8")
    assert "id:" in text
    assert "Add tests" in d.title
    assert len(d.content) > 50


def test_run_raises_on_agent_failure(tmp_path: Path):
    cfg = _cfg(tmp_path)
    diagnosis = Diagnosis(summary="x")
    fake = MagicMock(success=False, raw="", error="boom")
    with patch.object(proposer_mod.cursor_client, "agent_call", return_value=fake):
        with pytest.raises(RuntimeError, match="boom"):
            proposer_mod.run(cfg, diagnosis, kg=None)


def test_run_raises_on_short_content(tmp_path: Path):
    cfg = _cfg(tmp_path)
    diagnosis = Diagnosis(summary="x")
    fake = MagicMock(success=True, raw="short", error="")
    with patch.object(proposer_mod.cursor_client, "agent_call", return_value=fake):
        with pytest.raises(ValueError, match="empty"):
            proposer_mod.run(cfg, diagnosis, kg=None)


def test_kg_similar_completed_triggers_second_agent_call(tmp_path: Path):
    cfg = _cfg(tmp_path)
    diagnosis = Diagnosis(summary="x")
    first = "# DIRECTIVE: Add Slack veto tests\n\n" + ("word " * 30)
    second = "# DIRECTIVE: Improve metrics collection\n\n" + ("word " * 30)

    kg = MagicMock()
    kg.search.return_value = ["D001_auto"]
    kg.get_node.return_value = {
        "type": "directive",
        "status": "COMPLETED",
        "name": "Add Slack veto button tests",
    }

    calls: list[str] = []

    def fake_agent_call(c, sys, pr, **kw):
        calls.append(pr)
        if len(calls) == 1:
            return MagicMock(success=True, raw=first, error="")
        return MagicMock(success=True, raw=second, error="")

    with patch.object(proposer_mod.cursor_client, "agent_call", side_effect=fake_agent_call):
        d = proposer_mod.run(cfg, diagnosis, kg=kg)
    assert len(calls) == 2
    assert "NOTE: Similar directive" in calls[1]
    assert "metrics" in d.title.lower()


def test_next_directive_id_increments(tmp_path: Path):
    cfg = _cfg(tmp_path)
    (cfg.directives_dir / "D005_auto.md").write_text("x", encoding="utf-8")
    diagnosis = Diagnosis(summary="x")
    long_body = "# DIRECTIVE: T\n\n" + "word " * 30
    fake = MagicMock(success=True, raw=long_body, error="")
    with patch.object(proposer_mod.cursor_client, "agent_call", return_value=fake):
        d = proposer_mod.run(cfg, diagnosis, kg=None)
    assert d.id.startswith("M")
    assert "006" in d.id or int(d.id[1:4]) >= 6


# --- _normalize_agent_markdown_body ---


def test_normalize_agent_markdown_body_plain_no_fence():
    body = "# DIRECTIVE: X\n\nHello.\n"
    assert proposer_mod._normalize_agent_markdown_body(body) == body.strip()


def test_normalize_agent_markdown_body_single_markdown_fence():
    inner = "# DIRECTIVE: Wrapped\n\nContent here.\n"
    fenced = "```markdown\n" + inner + "```"
    assert proposer_mod._normalize_agent_markdown_body(fenced) == inner.strip()


def test_normalize_agent_markdown_body_unclosed_fence_returns_stripped_original():
    original = "```markdown\n# DIRECTIVE: No close\nstill open"
    assert proposer_mod._normalize_agent_markdown_body(original) == original.strip()


def test_normalize_agent_markdown_body_fence_only_no_newline_returns_stripped():
    # first_nl == -1: opening fence line has no newline after the language tag
    assert proposer_mod._normalize_agent_markdown_body("```") == "```"
    assert proposer_mod._normalize_agent_markdown_body("```markdown") == "```markdown"


# --- _extract_directive_title ---


def test_extract_directive_title_from_first_heading():
    assert proposer_mod._extract_directive_title("# DIRECTIVE: Foo bar\n\nMore.") == "Foo bar"


def test_extract_directive_title_skips_fences_and_blanks():
    content = "\n\n```\nignored\n```\n\n# DIRECTIVE: After fences\n"
    assert proposer_mod._extract_directive_title(content) == "After fences"


def test_extract_directive_title_missing_returns_untitled():
    assert proposer_mod._extract_directive_title("Just prose\n\nno directive.") == "Untitled Directive"


# --- _kg_notes_for_similar_attempts ---


def test_kg_notes_empty_when_kg_none():
    assert proposer_mod._kg_notes_for_similar_attempts(None, "Some title") == ""


def test_kg_notes_empty_when_proposed_title_blank():
    kg = MagicMock()
    assert proposer_mod._kg_notes_for_similar_attempts(kg, "") == ""
    assert proposer_mod._kg_notes_for_similar_attempts(kg, "   ") == ""
    kg.search.assert_not_called()


def test_kg_notes_empty_when_search_raises():
    kg = MagicMock()
    kg.search.side_effect = RuntimeError("kg down")
    assert proposer_mod._kg_notes_for_similar_attempts(kg, "Add tests") == ""


def test_kg_notes_one_similar_completed_directive():
    kg = MagicMock()
    kg.search.return_value = ["D010_auto"]
    kg.get_node.return_value = {
        "type": "directive",
        "status": "COMPLETED",
        "name": "Add tests for proposer",
    }
    out = proposer_mod._kg_notes_for_similar_attempts(kg, "Add tests for proposer")
    assert "NOTE: Similar directive was already attempted" in out
    assert "D010_auto" in out
    assert "COMPLETED" in out


def test_kg_notes_skips_non_terminal_status():
    kg = MagicMock()
    kg.search.return_value = ["D011_auto"]
    kg.get_node.return_value = {
        "type": "directive",
        "status": "PENDING",
        "name": "Add tests for proposer",
    }
    assert proposer_mod._kg_notes_for_similar_attempts(kg, "Add tests for proposer") == ""


def test_kg_notes_dedupes_duplicate_nid_and_status():
    kg = MagicMock()
    kg.search.return_value = ["D012_auto", "D012_auto"]
    node = {
        "type": "directive",
        "status": "AGENT_FAILED",
        "name": "Fix Slack buttons",
    }
    kg.get_node.return_value = node
    out = proposer_mod._kg_notes_for_similar_attempts(kg, "Fix Slack buttons")
    assert out.count("NOTE: Similar directive was already attempted") == 1
    assert "D012_auto" in out
    assert "AGENT_FAILED" in out


def test_kg_notes_no_note_when_name_not_similar_to_proposed():
    kg = MagicMock()
    kg.search.return_value = ["D013_auto"]
    kg.get_node.return_value = {
        "type": "directive",
        "status": "COMPLETED",
        "name": "Omega",
    }
    assert proposer_mod._kg_notes_for_similar_attempts(kg, "Alpha") == ""
