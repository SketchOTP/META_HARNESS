from __future__ import annotations

import importlib
import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import meta_harness.cycle as cycle_mod
import meta_harness.slack_integration as si
from meta_harness.config import load_config
from meta_harness.cycle import CycleOutcome, CycleStatus, _veto_window
from meta_harness.proposer import Directive


@pytest.fixture
def tmp_proj(tmp_path: Path) -> Path:
    (tmp_path / "metaharness.toml").write_text(
        "[project]\nname = \"proj-x\"\n[cycle]\nveto_seconds = 120\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_slack_socket_listener_state():
    """Avoid global listener leakage across tests (same process shares slack_integration)."""
    yield
    importlib.import_module("meta_harness.slack_integration").reset_slack_socket_listener_state()


def test_slack_disabled_no_import_error(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", False)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = False

    d = Directive(id="D1", path=tmp_proj / "d.md", title="T", content="c")
    assert si.post_veto_window(cfg, "D1", "T", "s", 10) is None
    si.update_veto_result(cfg, True)
    si.post_message(cfg, "x")
    out = si.handle_slash_command(cfg, "help", "")
    assert "status" in out.lower()
    assert si.start_socket_mode(cfg) is None
    si.slack_test(cfg)
    o = CycleOutcome(
        cycle_id="c1",
        timestamp="t",
        directive_id="D1",
        directive_title="T",
        status=CycleStatus.COMPLETED,
        phases_completed=1,
    )
    si.post_cycle_outcome(cfg, o)


def test_post_veto_window_builds_correct_blocks(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", True)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    cfg.slack.channel = "C111"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    captured: dict = {}

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "ts": "111.222"}

    monkeypatch.setattr(si, "WebClient", FakeClient)

    ts = si.post_veto_window(cfg, "D009", "My Title", "summary text", 60)
    assert ts == "111.222"
    blocks = captured.get("blocks") or []
    flat = json.dumps(blocks)
    assert "mh_approve" in flat and "mh_veto" in flat
    assert "My Title" in flat


def test_post_veto_window_saves_ts(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", True)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, **kwargs):
            return {"ok": True, "ts": "99.88"}

    monkeypatch.setattr(si, "WebClient", FakeClient)
    si.post_veto_window(cfg, "D1", "T", "s", 10)
    p = cfg.harness_dir / "slack_veto_ts.txt"
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert lines[-1] == "99.88"


def test_update_veto_result_approved(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", True)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    ts_path = cfg.harness_dir / "slack_veto_ts.txt"
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    ts_path.write_text("1.23", encoding="utf-8")

    updates: list = []

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_update(self, **kwargs):
            updates.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(si, "WebClient", FakeClient)
    si.update_veto_result(cfg, approved=True)
    assert not ts_path.exists()
    assert updates and "Approved" in json.dumps(updates[0])


def test_update_veto_result_vetoed(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", True)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    ts_path = cfg.harness_dir / "slack_veto_ts.txt"
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    ts_path.write_text("1.23", encoding="utf-8")

    updates: list = []

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_update(self, **kwargs):
            updates.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(si, "WebClient", FakeClient)
    si.update_veto_result(cfg, approved=False)
    assert "Vetoed" in json.dumps(updates[0])


def test_post_cycle_outcome_completed(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", True)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    posted: list = []

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, **kwargs):
            posted.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(si, "WebClient", FakeClient)
    o = CycleOutcome(
        cycle_id="c",
        timestamp="t",
        directive_id="D1",
        directive_title="T",
        status=CycleStatus.COMPLETED,
        phases_completed=1,
    )
    si.post_cycle_outcome(cfg, o)
    assert posted and "✅" in posted[0].get("text", "")


def test_post_cycle_outcome_test_failed(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", True)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    posted: list = []

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, **kwargs):
            posted.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(si, "WebClient", FakeClient)
    o = CycleOutcome(
        cycle_id="c",
        timestamp="t",
        directive_id="D1",
        directive_title="T",
        status=CycleStatus.TEST_FAILED,
        phases_completed=1,
    )
    si.post_cycle_outcome(cfg, o)
    assert "🔴" in posted[0].get("text", "")


def test_handle_slash_memory_empty_mrkdwn(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    out = si.handle_slash_command(cfg, "memory", "")
    assert "Harness Memory" in out
    assert "No harness cycles" in out or "recorded" in out


def test_handle_slash_product_status_and_help(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    cfg.product_cycles_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.product_cycles_dir / "product_x.json"
    p.write_text(
        json.dumps(
            {
                "directive": "P001_auto",
                "status": "COMPLETED",
                "directive_title": "Feature",
            }
        ),
        encoding="utf-8",
    )
    out = si.handle_slash_command(cfg, "product", "status")
    assert "Product cycles" in out and "P001" in out
    h = si.handle_slash_command(cfg, "product", "help")
    assert "roadmap" in h.lower()


def test_handle_slash_status(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    cfg.cycles_dir.mkdir(parents=True, exist_ok=True)
    for i, st in enumerate(["COMPLETED", "VETOED", "ERROR"]):
        p = cfg.cycles_dir / f"cycle_{i}.json"
        p.write_text(
            json.dumps({"directive": f"D{i}", "status": st, "delta": None}),
            encoding="utf-8",
        )
        time.sleep(0.01)
    out = si.handle_slash_command(cfg, "status", "")
    assert "Recent Cycles" in out
    assert "D0" in out or "D1" in out or "D2" in out
    assert "✅" in out or "⊘" in out or "🔴" in out


def test_handle_slash_pause(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    si.handle_slash_command(cfg, "pause", "")
    assert cfg.daemon_pause_path.exists()


def test_handle_slash_resume(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    cfg.daemon_pause_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_pause_path.write_text("", encoding="utf-8")
    si.handle_slash_command(cfg, "resume", "")
    assert not cfg.daemon_pause_path.exists()


def test_handle_slash_proceed(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    si.handle_slash_command(cfg, "proceed", "")
    assert cfg.slack_early_approve_path.exists()


def test_handle_slash_veto(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    cfg.pending_veto_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.pending_veto_path.write_text("x", encoding="utf-8")
    si.handle_slash_command(cfg, "veto", "")
    assert not cfg.pending_veto_path.exists()


def test_handle_slash_help(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    out = si.handle_slash_command(cfg, "help", "")
    for word in (
        "status",
        "memory",
        "memmap",
        "pause",
        "resume",
        "proceed",
        "veto",
        "research",
    ):
        assert word in out.lower()


def test_handle_slash_memmap(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    out = si.handle_slash_command(cfg, "memmap", "")
    assert "━" in out
    assert "*📊 Memory Map" in out
    assert "No harness memory yet" in out or "🔥" in out


def test_handle_slash_memmap_truncates_long_map(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(tmp_proj)

    def huge(_m, _c):
        return "M" * 5000

    monkeypatch.setattr(si, "_slack_memmap", huge)
    out = si.handle_slash_command(cfg, "memmap", "")
    assert len(out) <= si._SLACK_EPHEMERAL_MAX
    assert "truncated" in out.lower() or "…" in out


def test_handle_slash_unknown(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    out = si.handle_slash_command(cfg, "notacommand", "")
    assert "help" in out.lower()


def test_slack_test_posts_message(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", True)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    posted: list = []

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, **kwargs):
            posted.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(si, "WebClient", FakeClient)
    si.slack_test(cfg)
    assert posted and "proj-x" in posted[0].get("text", "")


def test_slack_test_raises_on_failure(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(si, "_SLACK_AVAILABLE", True)
    cfg = load_config(tmp_proj)
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, **kwargs):
            return {"ok": False, "error": "channel_not_found"}

    monkeypatch.setattr(si, "WebClient", FakeClient)
    with pytest.raises(RuntimeError, match="channel_not_found|failed"):
        si.slack_test(cfg)


def test_veto_window_early_approve_via_slack(tmp_proj: Path):
    cfg = load_config(tmp_proj)
    cfg.cycle.veto_seconds = 5
    d = Directive(id="D1", path=tmp_proj / "d.md", title="T", content="c")

    def touch_later():
        time.sleep(0.4)
        cfg.slack_early_approve_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.slack_early_approve_path.write_text("", encoding="utf-8")

    threading.Thread(target=touch_later, daemon=True).start()
    assert _veto_window(cfg, d, ["op"]) is True


def test_veto_window_starts_socket_when_tokens_ready(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = load_config(tmp_proj)
    cfg.cycle.veto_seconds = 2
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    d = Directive(id="D1", path=tmp_proj / "d.md", title="T", content="c")
    mock_h = MagicMock()

    monkeypatch.setattr("meta_harness.slack_integration.post_veto_window", lambda *a, **k: None)
    monkeypatch.setattr("meta_harness.slack_integration.update_veto_result", lambda *a, **k: None)
    monkeypatch.setattr("meta_harness.slack_integration.socket_tokens_ready", lambda c: True)
    monkeypatch.setattr("meta_harness.slack_integration.start_socket_mode", lambda c: mock_h)

    class T:
        v = 0.0

        @classmethod
        def time(cls):
            return cls.v

        @classmethod
        def sleep(cls, _):
            cls.v += 5.0

    monkeypatch.setattr(cycle_mod.time, "time", T.time)
    monkeypatch.setattr(cycle_mod.time, "sleep", T.sleep)

    assert _veto_window(cfg, d, None) is True
    mock_h.connect.assert_called()
    mock_h.close.assert_called()
    assert not si.slack_socket_listener_active()


def test_veto_window_skips_socket_when_listener_already_active(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    # Use canonical slack_integration from sys.modules so registration matches
    # ``from . import slack_integration`` inside ``_veto_window`` after conftest purges.
    si = importlib.import_module("meta_harness.slack_integration")
    cfg = load_config(tmp_proj)
    cfg.cycle.veto_seconds = 2
    cfg.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    d = Directive(id="D1", path=tmp_proj / "d.md", title="T", content="c")
    existing = MagicMock()
    si.register_slack_socket_listener(existing)
    started: list[Any] = []

    def capture_start(c):
        started.append(1)
        return MagicMock()

    monkeypatch.setattr("meta_harness.slack_integration.post_veto_window", lambda *a, **k: None)
    monkeypatch.setattr("meta_harness.slack_integration.update_veto_result", lambda *a, **k: None)
    monkeypatch.setattr("meta_harness.slack_integration.socket_tokens_ready", lambda c: True)
    monkeypatch.setattr("meta_harness.slack_integration.start_socket_mode", capture_start)

    class T:
        v = 0.0

        @classmethod
        def time(cls):
            return cls.v

        @classmethod
        def sleep(cls, _):
            cls.v += 5.0

    monkeypatch.setattr(cycle_mod.time, "time", T.time)
    monkeypatch.setattr(cycle_mod.time, "sleep", T.sleep)

    try:
        assert _veto_window(cfg, d, None) is True
        assert started == []
    finally:
        si.unregister_slack_socket_listener(existing)


def test_veto_window_proceeds_to_slack_update_on_approve(
    tmp_proj: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = load_config(tmp_proj)
    cfg.cycle.veto_seconds = 10
    cfg.slack.enabled = True
    d = Directive(id="D1", path=tmp_proj / "d.md", title="T", content="c")

    monkeypatch.setattr("meta_harness.slack_integration.post_veto_window", lambda *a, **k: None)
    monkeypatch.setattr("meta_harness.slack_integration.socket_tokens_ready", lambda c: False)

    calls: list[bool] = []

    def track_update(c, approved):
        calls.append(approved)

    monkeypatch.setattr("meta_harness.slack_integration.update_veto_result", track_update)

    class T:
        v = 0.0

        @classmethod
        def time(cls):
            return cls.v

        @classmethod
        def sleep(cls, _):
            cls.v += 2.0

    monkeypatch.setattr(cycle_mod.time, "time", T.time)
    monkeypatch.setattr(cycle_mod.time, "sleep", T.sleep)

    assert _veto_window(cfg, d, None) is True
    assert True in calls


def test_get_tokens_rejects_non_xapp(tmp_proj: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(tmp_proj)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xoxp-bad")
    with pytest.raises(RuntimeError, match="xapp-"):
        si._get_tokens(cfg)
