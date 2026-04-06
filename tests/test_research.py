from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

import meta_harness.research as research_mod
import meta_harness.slack_integration as si
from meta_harness.cli import main
from meta_harness.config import load_config
from meta_harness.cursor_client import CursorResponse
from meta_harness.research import (
    PaperContent,
    ResearchEvaluation,
    clear_queue_item,
    evaluate_paper,
    fetch_paper,
    format_slack_verdict,
    get_queue,
    queue_paper,
)


@pytest.fixture
def tmp_proj(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        '[project]\nname = "proj-x"\n[cycle]\nveto_seconds = 120\n',
        encoding="utf-8",
    )
    return tmp_path


def test_fetch_arxiv_extracts_title_and_abstract():
    html = """<!DOCTYPE html>
<html><head><title>arXiv:1234.5678v1 Title</title></head>
<body>
<h1 class="title mathjax">My Paper Title</h1>
<blockquote class="abstract mathjax">
<h2>Abstract</h2>
This is the abstract text.
</blockquote>
</body></html>"""
    pdf_bytes = b"%PDF-1.4 fake"

    class FakeResp:
        def __init__(self, text: str = "", content: bytes = b"", ctype: str = "text/html"):
            self.text = text
            self.content = content
            self.headers = {"content-type": ctype}

        def raise_for_status(self) -> None:
            pass

    calls: list[str] = []

    def fake_get(url: str, **_kwargs):
        calls.append(url)
        if "/abs/" in url:
            return FakeResp(text=html)
        if "/pdf/" in url:
            return FakeResp(content=pdf_bytes, ctype="application/pdf")
        raise AssertionError(url)

    mock_reader = MagicMock()
    mock_reader.pages = [MagicMock(extract_text=lambda: "Body page one.\n\nMore text.")]

    with patch.object(research_mod, "httpx", httpx):
        with patch.object(research_mod.httpx, "get", side_effect=fake_get):
            with patch.object(research_mod, "PdfReader", return_value=mock_reader):
                paper = fetch_paper("https://arxiv.org/abs/1234.5678")
    assert paper.source_type == "arxiv"
    assert "My Paper Title" in paper.title
    assert "abstract text" in paper.abstract.lower()
    assert paper.body_excerpt
    assert not paper.fetch_error
    assert any("/abs/" in c for c in calls) and any("/pdf/" in c for c in calls)


def test_fetch_pdf_url_parses_text():
    class FakeResp:
        def __init__(self):
            self.text = ""
            self.content = b"%PDF-1.4 x"
            self.headers = {"content-type": "application/pdf"}

        def raise_for_status(self) -> None:
            pass

    mock_reader = MagicMock()
    mock_reader.pages = [
        MagicMock(extract_text=lambda: "First paragraph title line.\n\nSecond block."),
    ]

    with patch.object(research_mod, "httpx", httpx):
        with patch.object(research_mod.httpx, "get", return_value=FakeResp()):
            with patch.object(research_mod, "PdfReader", return_value=mock_reader):
                paper = fetch_paper("https://example.com/paper.pdf")
    assert paper.source_type == "pdf"
    assert paper.body_excerpt
    assert not paper.fetch_error


def test_fetch_handles_network_error():
    with patch.object(research_mod, "httpx", httpx):
        with patch.object(
            research_mod.httpx,
            "get",
            side_effect=httpx.TimeoutException("timeout"),
        ):
            paper = fetch_paper("https://example.com/x")
    assert paper.fetch_error
    assert "timeout" in paper.fetch_error.lower() or "Timeout" in paper.fetch_error


def test_evaluate_paper_parses_implement(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    paper = PaperContent(url="https://x", title="T", abstract="A", body_excerpt="B")
    payload = {
        "relevant": True,
        "confidence": 0.9,
        "applicable_to": "mod",
        "implementation_difficulty": "low",
        "expected_impact": "faster",
        "recommendation": "implement",
        "reason": "fits vision",
    }
    with patch.object(
        research_mod.cursor_client,
        "json_call",
        return_value=CursorResponse(success=True, data=payload, raw="{}"),
    ):
        ev = evaluate_paper(cfg, paper)
    assert ev.relevant is True
    assert ev.recommendation == "implement"
    assert ev.applicable_to == "mod"


def test_evaluate_paper_parses_discard(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    paper = PaperContent(url="https://x", title="T", abstract="A", body_excerpt="B")
    payload = {
        "relevant": True,
        "confidence": 0.2,
        "applicable_to": "",
        "implementation_difficulty": "high",
        "expected_impact": "",
        "recommendation": "discard",
        "reason": "no fit",
    }
    with patch.object(
        research_mod.cursor_client,
        "json_call",
        return_value=CursorResponse(success=True, data=payload, raw="{}"),
    ):
        ev = evaluate_paper(cfg, paper)
    assert ev.relevant is False
    assert ev.recommendation == "discard"


def test_evaluate_paper_handles_json_failure(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    paper = PaperContent(url="https://x", title="T", abstract="A", body_excerpt="B")
    with patch.object(
        research_mod.cursor_client,
        "json_call",
        return_value=CursorResponse(success=False, error="bad", raw=""),
    ):
        ev = evaluate_paper(cfg, paper)
    assert ev.relevant is False
    assert ev.recommendation == "discard"
    assert "Evaluation failed" in ev.reason


def test_queue_paper_writes_json(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    ev = ResearchEvaluation(
        url="https://u",
        title="T",
        relevant=True,
        confidence=0.8,
        applicable_to="m",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    assert queue_paper(cfg, ev) is True
    assert cfg.research_queue_path.is_file()
    q = get_queue(cfg)
    assert len(q) == 1
    assert q[0]["url"] == "https://u"
    assert q[0]["title"] == "T"
    assert q[0]["difficulty"] == "low"


def test_queue_paper_appends_to_existing(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    for i in range(2):
        ev = ResearchEvaluation(
            url=f"https://u{i}",
            title=f"T{i}",
            relevant=True,
            confidence=0.5,
            applicable_to="m",
            implementation_difficulty="medium",
            expected_impact="x",
            recommendation="implement",
            reason="r",
        )
        assert queue_paper(cfg, ev) is True
    q = get_queue(cfg)
    assert len(q) == 2
    assert {x["url"] for x in q} == {"https://u0", "https://u1"}


def test_queue_paper_caps_at_10(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    for i in range(10):
        ev = ResearchEvaluation(
            url=f"https://u{i}",
            title=f"T{i}",
            relevant=True,
            confidence=0.5,
            applicable_to="m",
            implementation_difficulty="medium",
            expected_impact="x",
            recommendation="implement",
            reason="r",
        )
        assert queue_paper(cfg, ev) is True
    ev11 = ResearchEvaluation(
        url="https://overflow",
        title="X",
        relevant=True,
        confidence=0.5,
        applicable_to="m",
        implementation_difficulty="medium",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    assert queue_paper(cfg, ev11) is False
    assert len(get_queue(cfg)) == 10


def test_queue_paper_replaces_oldest_monitor_when_full(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    cfg.research_queue_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.research_queue_path.write_text(
        '[{"url":"https://old","title":"old","applicable_to":"x","expected_impact":"e",'
        '"difficulty":"low","reason":"r","recommendation":"monitor","queued_at":"t"}]',
        encoding="utf-8",
    )
    for i in range(9):
        ev = ResearchEvaluation(
            url=f"https://impl{i}",
            title=f"I{i}",
            relevant=True,
            confidence=0.5,
            applicable_to="m",
            implementation_difficulty="medium",
            expected_impact="x",
            recommendation="implement",
            reason="r",
        )
        assert queue_paper(cfg, ev) is True
    assert len(get_queue(cfg)) == 10
    new_ev = ResearchEvaluation(
        url="https://new",
        title="N",
        relevant=True,
        confidence=0.5,
        applicable_to="m",
        implementation_difficulty="medium",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    assert queue_paper(cfg, new_ev) is True
    q = get_queue(cfg)
    assert len(q) == 10
    urls = [x["url"] for x in q]
    assert "https://new" in urls
    assert "https://old" not in urls


def test_get_queue_returns_empty_list_when_missing(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    p = cfg.research_queue_path
    if p.exists():
        p.unlink()
    assert get_queue(cfg) == []


def test_clear_queue_item_removes_by_url(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    ev = ResearchEvaluation(
        url="https://keep",
        title="K",
        relevant=True,
        confidence=0.5,
        applicable_to="m",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    queue_paper(cfg, ev)
    assert clear_queue_item(cfg, "https://keep") is True
    assert get_queue(cfg) == []


def test_clear_queue_item_returns_false_when_not_found(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    assert clear_queue_item(cfg, "https://nope") is False


def test_format_slack_verdict_implement():
    ev = ResearchEvaluation(
        url="https://x",
        title="T",
        relevant=True,
        confidence=0.8,
        applicable_to="mod",
        implementation_difficulty="low",
        expected_impact="big",
        recommendation="implement",
        reason="because",
    )
    s = format_slack_verdict(ev)
    assert "✅" in s
    assert "Queued" in s or "queue" in s.lower()


def test_format_slack_verdict_discard():
    ev = ResearchEvaluation(
        url="https://x",
        title="T",
        relevant=False,
        confidence=0.1,
        applicable_to="",
        implementation_difficulty="high",
        expected_impact="",
        recommendation="discard",
        reason="nope",
    )
    s = format_slack_verdict(ev)
    assert "Not Applicable" in s


def test_slack_research_command_starts_thread(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(tmp_proj)
    started: list[tuple] = []

    class FakeThread:
        def __init__(self, *a, **kwargs):
            self._kwargs = kwargs

        def start(self):
            started.append(self._kwargs)

    monkeypatch.setattr(si.threading, "Thread", FakeThread)
    monkeypatch.setattr(si, "_research_background", lambda *a, **k: None)
    out = si.handle_slash_command(cfg, "research", "https://example.com/p")
    assert "Evaluating" in out or "evaluating" in out.lower()
    assert started
    kwargs = started[0]
    assert kwargs.get("daemon") is True
    assert kwargs.get("name") == "metaharness-research"


def test_slack_research_queue_command(tmp_proj: Path):
    cfg = load_config(tmp_proj)

    def fake_queue(_c):
        return [
            {"title": "Alpha paper title here", "applicable_to": "m1", "difficulty": "low"},
            {"title": "Beta", "applicable_to": "m2", "difficulty": "high"},
        ]

    with patch("meta_harness.research.get_queue", fake_queue):
        out = si.handle_slash_command(cfg, "research", "queue")
    assert "Alpha" in out
    assert "Beta" in out


def test_slack_research_discard_command(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    with patch("meta_harness.research.clear_queue_item", lambda _c, u: True):
        out = si.handle_slash_command(cfg, "research", "discard https://x")
    assert "Removed" in out


def test_notify_research_queue_item_mrkdwn(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(tmp_proj)
    posted: list[str] = []

    def capture(_c, t: str) -> str:
        posted.append(t)
        return "ts"

    monkeypatch.setattr(si, "post_message", capture)
    si.notify_research_queue_item(
        cfg,
        {
            "title": "T1",
            "url": "https://example.com/p",
            "applicable_to": "widgets",
            "difficulty": "low",
        },
    )
    assert len(posted) == 1
    assert "Research — ready to implement" in posted[0]
    assert "T1" in posted[0]
    assert "https://example.com/p" in posted[0]


def test_notify_research_queue_item_respects_config_off(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(tmp_proj)
    cfg.slack.notify_research_queue = False
    posted: list[str] = []
    monkeypatch.setattr(si, "post_message", lambda c, t: posted.append(t))
    si.notify_research_queue_item(cfg, {"title": "T", "url": "https://u"})
    assert posted == []


def test_notify_research_queue_item_slack_disabled_no_raise(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = False
    si.notify_research_queue_item(cfg, {"title": "T", "url": "https://u"})


def test_research_eval_slack_notify_on_implement(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    # `test_conftest_coverage_registration` reloads `meta_harness`; re-bind CLI + research
    # so this test does not use a stale `main` imported at module load time.
    _tpath = Path(__file__).resolve().parent / "conftest.py"
    _spec = importlib.util.spec_from_file_location("_mh_tests_conftest", _tpath)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Cannot load {_tpath}")
    _tmod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tmod)
    _tmod._ensure_meta_harness()

    main_fresh = importlib.import_module("meta_harness.cli").main
    research_live = importlib.import_module("meta_harness.research")

    (tmp_proj / "metaharness.toml").write_text(
        '[project]\nname = "p"\n[cycle]\nveto_seconds = 120\n[slack]\nenabled = true\n',
        encoding="utf-8",
    )
    posted: list[str] = []

    def capture_post(_c, t: str) -> str:
        posted.append(t)
        return "ts"

    si_live = importlib.import_module("meta_harness.slack_integration")
    monkeypatch.setattr(si_live, "post_message", capture_post)
    ev = ResearchEvaluation(
        url="https://example.com/paper",
        title="UniquePaperTitleXYZ",
        relevant=True,
        confidence=0.9,
        applicable_to="widgets",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(research_live, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(research_live, "evaluate_paper", lambda c, p: ev)

    runner = CliRunner()
    result = runner.invoke(main_fresh, ["research", "eval", ev.url, "--dir", str(tmp_proj)])
    assert result.exit_code == 0
    assert posted
    assert any("UniquePaperTitleXYZ" in p for p in posted)
    assert sum(1 for p in posted if "Research — ready to implement" in p) == 1


def test_research_eval_implement_queue_fail_no_slack_queue_ping(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    _tpath = Path(__file__).resolve().parent / "conftest.py"
    _spec = importlib.util.spec_from_file_location("_mh_tests_conftest", _tpath)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Cannot load {_tpath}")
    _tmod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tmod)
    _tmod._ensure_meta_harness()

    main_fresh = importlib.import_module("meta_harness.cli").main
    research_live = importlib.import_module("meta_harness.research")

    (tmp_proj / "metaharness.toml").write_text(
        '[project]\nname = "p"\n[cycle]\nveto_seconds = 120\n[slack]\nenabled = true\n',
        encoding="utf-8",
    )
    posted: list[str] = []

    def capture_post(_c, t: str) -> str:
        posted.append(t)
        return "ts"

    si_live = importlib.import_module("meta_harness.slack_integration")
    monkeypatch.setattr(si_live, "post_message", capture_post)
    ev = ResearchEvaluation(
        url="https://example.com/full",
        title="FullQueue",
        relevant=True,
        confidence=0.9,
        applicable_to="w",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(research_live, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(research_live, "evaluate_paper", lambda c, p: ev)
    monkeypatch.setattr(research_live, "queue_paper", lambda c, e: False)

    runner = CliRunner()
    result = runner.invoke(main_fresh, ["research", "eval", ev.url, "--dir", str(tmp_proj)])
    assert result.exit_code == 0
    assert not any("Research — ready to implement" in p for p in posted)


def test_research_eval_monitor_no_slack_queue_ping(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    _tpath = Path(__file__).resolve().parent / "conftest.py"
    _spec = importlib.util.spec_from_file_location("_mh_tests_conftest", _tpath)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Cannot load {_tpath}")
    _tmod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tmod)
    _tmod._ensure_meta_harness()

    main_fresh = importlib.import_module("meta_harness.cli").main
    research_live = importlib.import_module("meta_harness.research")

    (tmp_proj / "metaharness.toml").write_text(
        '[project]\nname = "p"\n[cycle]\nveto_seconds = 120\n[slack]\nenabled = true\n',
        encoding="utf-8",
    )
    posted: list[str] = []

    def capture_post(_c, t: str) -> str:
        posted.append(t)
        return "ts"

    si_live = importlib.import_module("meta_harness.slack_integration")
    monkeypatch.setattr(si_live, "post_message", capture_post)
    ev = ResearchEvaluation(
        url="https://example.com/mon",
        title="MonitorPaper",
        relevant=True,
        confidence=0.5,
        applicable_to="w",
        implementation_difficulty="medium",
        expected_impact="x",
        recommendation="monitor",
        reason="r",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(research_live, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(research_live, "evaluate_paper", lambda c, p: ev)

    def boom(_c, _e):
        raise AssertionError("queue_paper must not run for monitor")

    monkeypatch.setattr(research_live, "queue_paper", boom)

    runner = CliRunner()
    result = runner.invoke(main_fresh, ["research", "eval", ev.url, "--dir", str(tmp_proj)])
    assert result.exit_code == 0
    assert not any("Research — ready to implement" in p for p in posted)


def test_research_eval_slack_notify_disabled_by_config(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    _tpath = Path(__file__).resolve().parent / "conftest.py"
    _spec = importlib.util.spec_from_file_location("_mh_tests_conftest", _tpath)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Cannot load {_tpath}")
    _tmod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tmod)
    _tmod._ensure_meta_harness()

    main_fresh = importlib.import_module("meta_harness.cli").main
    research_live = importlib.import_module("meta_harness.research")

    (tmp_proj / "metaharness.toml").write_text(
        '[project]\nname = "p"\n[cycle]\nveto_seconds = 120\n[slack]\nenabled = true\n'
        "notify_research_queue = false\n",
        encoding="utf-8",
    )
    posted: list[str] = []

    def capture_post(_c, t: str) -> str:
        posted.append(t)
        return "ts"

    si_live = importlib.import_module("meta_harness.slack_integration")
    monkeypatch.setattr(si_live, "post_message", capture_post)
    ev = ResearchEvaluation(
        url="https://example.com/off",
        title="OffCfg",
        relevant=True,
        confidence=0.9,
        applicable_to="w",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(research_live, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(research_live, "evaluate_paper", lambda c, p: ev)

    runner = CliRunner()
    result = runner.invoke(main_fresh, ["research", "eval", ev.url, "--dir", str(tmp_proj)])
    assert result.exit_code == 0
    assert not any("Research — ready to implement" in p for p in posted)


def test_research_background_implement_queue_ok_posts_ping_and_verdict(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    # Resolve submodules via importlib so patches apply after ``_ensure_meta_harness``
    # reloads ``meta_harness`` (see tests/test_conftest_coverage_registration.py).
    slack = importlib.import_module("meta_harness.slack_integration")
    rmod = importlib.import_module("meta_harness.research")
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    cfg.slack.channel = "C0123"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    ev = ResearchEvaluation(
        url="https://example.com/bg",
        title="BGPaper",
        relevant=True,
        confidence=0.9,
        applicable_to="mod",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(rmod, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(rmod, "evaluate_paper", lambda c, p: ev)
    monkeypatch.setattr(rmod, "queue_paper", lambda c, e: True)

    posts: list[str] = []

    def capture(_c, t: str) -> str:
        posts.append(t)
        return "ts"

    monkeypatch.setattr(slack, "post_message", capture)
    slack._research_background(cfg, ev.url)
    assert sum(1 for p in posts if "ready to implement" in p) == 1
    assert len(posts) == 2
    assert any("Research Review" in p or "Review" in p for p in posts)


def test_research_background_implement_queue_fail_no_queue_ping(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    slack = importlib.import_module("meta_harness.slack_integration")
    rmod = importlib.import_module("meta_harness.research")
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    cfg.slack.channel = "C0123"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    ev = ResearchEvaluation(
        url="https://example.com/nospace",
        title="NoSpace",
        relevant=True,
        confidence=0.9,
        applicable_to="m",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(rmod, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(rmod, "evaluate_paper", lambda c, p: ev)
    monkeypatch.setattr(rmod, "queue_paper", lambda c, e: False)

    posts: list[str] = []

    def capture(_c, t: str) -> str:
        posts.append(t)
        return "ts"

    monkeypatch.setattr(slack, "post_message", capture)
    slack._research_background(cfg, ev.url)
    assert not any("Research — ready to implement" in p for p in posts)
    assert len(posts) == 1


def test_research_background_discard_no_queue_ping(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    slack = importlib.import_module("meta_harness.slack_integration")
    rmod = importlib.import_module("meta_harness.research")
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    cfg.slack.channel = "C0123"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    ev = ResearchEvaluation(
        url="https://example.com/discard",
        title="Disc",
        relevant=False,
        confidence=0.1,
        applicable_to="",
        implementation_difficulty="high",
        expected_impact="",
        recommendation="discard",
        reason="no",
    )
    paper = PaperContent(url=ev.url, title=ev.title, abstract="a", body_excerpt="b")
    monkeypatch.setattr(rmod, "fetch_paper", lambda u: paper)
    monkeypatch.setattr(rmod, "evaluate_paper", lambda c, p: ev)

    def boom(_c, _e):
        raise AssertionError("queue_paper must not run for discard")

    monkeypatch.setattr(rmod, "queue_paper", boom)

    posts: list[str] = []

    def capture(_c, t: str) -> str:
        posts.append(t)
        return "ts"

    monkeypatch.setattr(slack, "post_message", capture)
    slack._research_background(cfg, ev.url)
    assert not any("Research — ready to implement" in p for p in posts)
    assert len(posts) == 1


def test_notify_research_queue_from_evaluation_delegates_to_item(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    slack = importlib.import_module("meta_harness.slack_integration")
    cfg = load_config(tmp_proj)
    calls: list[tuple] = []

    def capture_item(c, item):
        calls.append((c, dict(item)))

    monkeypatch.setattr(slack, "notify_research_queue_item", capture_item)
    ev = ResearchEvaluation(
        url="https://u",
        title="T",
        relevant=True,
        confidence=0.8,
        applicable_to="a",
        implementation_difficulty="low",
        expected_impact="x",
        recommendation="implement",
        reason="r",
    )
    slack.notify_research_queue_from_evaluation(cfg, ev)
    assert len(calls) == 1
    _, it = calls[0]
    assert it["title"] == "T"
    assert it["url"] == "https://u"
    assert it["applicable_to"] == "a"
    assert it["difficulty"] == "low"
