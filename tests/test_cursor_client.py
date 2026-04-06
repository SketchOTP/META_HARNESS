from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meta_harness.config import HarnessConfig
from meta_harness import cursor_client
from meta_harness.platform_runtime import CursorAgentBinaryNotFound


def _cfg(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(project_root=tmp_path)


def test_extract_json_bare():
    assert cursor_client.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_utf8_bom_before_bare_json():
    assert cursor_client.extract_json("\ufeff" + '{"a": 1}') == {"a": 1}


def test_extract_json_raw_decode_ignores_trailing_prose_after_object():
    raw = '{"ok": true, "n": 1}\n\nHere is extra explanation.'
    assert cursor_client.extract_json(raw) == {"ok": True, "n": 1}


def test_extract_json_finds_json_after_many_stray_braces():
    junk = "{" * 150
    raw = junk + '{"payload": true}'
    assert cursor_client.extract_json(raw) == {"payload": True}


def test_extract_json_fenced():
    raw = 'Here:\n```json\n{"x": true}\n```\n'
    assert cursor_client.extract_json(raw) == {"x": True}


def test_extract_json_fenced_blank_lines_after_json_opener():
    raw = 'Here:\n```json\n\n\n{"x": true}\n```\n'
    assert cursor_client.extract_json(raw) == {"x": True}


def test_extract_json_unclosed_json_fence():
    """Truncated stdout: opening ```json but no closing fence; still parse the object."""
    raw = 'Intro.\n\n```json\n{"ok": true, "n": 42}\n'
    assert cursor_client.extract_json(raw) == {"ok": True, "n": 42}


def test_extract_json_unclosed_json_fence_prefers_last_payload():
    raw = """First block (closed).

```json
{"old": true}
```

```json
{"ok": true, "winner": true}
"""
    assert cursor_client.extract_json(raw) == {"ok": True, "winner": True}


def test_json_call_unclosed_json_fence(tmp_path):
    cfg = _cfg(tmp_path)
    stdout = 'Here is analysis.\n\n```json\n{"phase": "diagnose", "items": []}\n'
    fake = MagicMock(returncode=0, stdout=stdout, stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", label="unclosed_fence", max_retries=0)
    assert r.success is True
    assert r.data == {"phase": "diagnose", "items": []}


def test_extract_json_embedded():
    raw = 'Analysis follows.\n\n{"items": [1, 2]}\n\nThanks.'
    assert cursor_client.extract_json(raw) == {"items": [1, 2]}


def test_extract_json_top_level_array():
    raw = "Prefix\n[1, 2, 3]\n"
    assert cursor_client.extract_json(raw) == [1, 2, 3]


def test_extract_json_nested_braces_in_string():
    raw = r'{"a": "{\"x\": 1}"}'
    assert cursor_client.extract_json(raw) == {"a": '{"x": 1}'}


def test_extract_json_fenced_json_string_contains_triple_backticks():
    """Legacy non-greedy fence regex splits on the first ``` inside a JSON string; raw_decode does not."""
    payload = {"hint": "Use ```python\ncode\n``` in markdown", "ok": True}
    raw = "Some prose before the block.\n\n```json\n" + json.dumps(payload) + "\n```\n"
    assert cursor_client.extract_json(raw) == payload


def test_extract_json_none():
    assert cursor_client.extract_json("no json here at all") is None


def test_unwrap_cursor_envelope_string_result():
    inner = '{"summary": "ok", "architecture_notes": "n", "changes": []}'
    wrapped = json.dumps(
        {"type": "result", "subtype": "success", "result": inner}
    )
    out = cursor_client._unwrap_cursor_envelope(wrapped)
    assert out == inner


def test_unwrap_cursor_envelope_dict_result():
    inner_obj = {"a": 1}
    wrapped = json.dumps({"type": "result", "result": inner_obj})
    out = cursor_client._unwrap_cursor_envelope(wrapped)
    assert json.loads(out) == inner_obj


def test_unwrap_cursor_envelope_passthrough_non_envelope():
    bare = '{"x": 1}'
    assert cursor_client._unwrap_cursor_envelope(bare) == bare


def test_json_call_unwraps_cursor_result_envelope(tmp_path):
    cfg = _cfg(tmp_path)
    inner = '{"ok": true}'
    wrapped = json.dumps({"type": "result", "subtype": "success", "result": inner})
    fake = MagicMock(returncode=0, stdout=wrapped, stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert r.success is True
    assert r.data == {"ok": True}
    assert r.raw == inner


def test_extract_json_skips_invalid_first_fence():
    raw = """Here is some explanation.

```
not json at all
```

```json
{"summary": "ok", "architecture_notes": "n", "changes": []}
```
"""
    assert cursor_client.extract_json(raw) == {
        "summary": "ok",
        "architecture_notes": "n",
        "changes": [],
    }


def test_extract_json_prefers_json_fence_after_generic():
    raw = """
```
not valid json
```

```json
{"a": 1}
```
"""
    assert cursor_client.extract_json(raw) == {"a": 1}


def test_extract_json_invalid_first_object_valid_second():
    """First balanced `{...}` is not valid JSON; a later object is (markdown-heavy CLI output).

    Avoid a bare `[` before the good object (e.g. no `[]` in the payload) so the legacy
    first-`[` path cannot succeed with a tiny valid `[]` slice.
    """
    raw = """Here is analysis.

{ this is not valid json }

The answer is:

{"summary": "ok", "architecture_notes": "n", "changes": {}}
"""
    assert cursor_client.extract_json(raw) == {
        "summary": "ok",
        "architecture_notes": "n",
        "changes": {},
    }


def test_extract_json_second_top_level_brace_without_fence():
    """Only the second `{` begins a valid JSON object (no code fences)."""
    raw = 'Prose with { stray brace } then real payload {"n": 42}'
    assert cursor_client.extract_json(raw) == {"n": 42}


def test_extract_json_multi_candidate_inside_single_fence():
    """Whole fence body is not JSON; first balanced segment invalid, second valid."""
    raw = """```text
{ broken }
{"k": "v"}
```"""
    assert cursor_client.extract_json(raw) == {"k": "v"}


def test_json_call_success(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = '{"ok": true, "n": 42}'
    fake.stderr = ""
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "sys", "user", label="t", max_retries=0)
    assert r.success is True
    assert r.data == {"ok": True, "n": 42}


def test_json_call_bom_json_and_trailing_prose(tmp_path):
    cfg = _cfg(tmp_path)
    stdout = (
        "\ufeff"
        + '{"phase": "analyze", "items": []}'
        + "\n\n---\n\nAdditional commentary from the model."
    )
    fake = MagicMock(returncode=0, stdout=stdout, stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", label="bom_trail", max_retries=0)
    assert r.success is True
    assert r.data == {"phase": "analyze", "items": []}


def test_json_call_prose_then_single_json_fence(tmp_path):
    cfg = _cfg(tmp_path)
    stdout = """Here is a short introduction before the structured result.

```json
{"ok": true, "answer": 7}
```
"""
    fake = MagicMock(returncode=0, stdout=stdout, stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", label="prose_fence", max_retries=0)
    assert r.success is True
    assert r.data == {"ok": True, "answer": 7}


def test_json_call_two_json_fences_prefers_last_payload(tmp_path):
    cfg = _cfg(tmp_path)
    stdout = """Analysis.

```json
{"schema_only": true}
```

```json
{"ok": true, "real": "payload"}
```
"""
    fake = MagicMock(returncode=0, stdout=stdout, stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", label="two_fences", max_retries=0)
    assert r.success is True
    assert r.data == {"ok": True, "real": "payload"}


def test_extract_json_two_json_fences_prefers_last():
    raw = """Intro.

```json
{"decoy": 1}
```

```json
{"decoy": 2, "winner": true}
```
"""
    assert cursor_client.extract_json(raw) == {"decoy": 2, "winner": True}


def test_extract_json_suffix_after_long_preamble_beyond_multi_scan_window():
    pad = "p" * (cursor_client._EXTRACT_JSON_MULTI_MAX_OFFSET + 5000)
    raw = pad + '\n```json\n{"only": "here"}\n```\n'
    assert cursor_client.extract_json(raw) == {"only": "here"}


def test_extract_json_oversized_fence_inner_finds_trailing_json():
    # Inner body alone exceeds _EXTRACT_JSON_MULTI_MAX_INNER; valid JSON only at end of fence
    filler = "x\n" * (cursor_client._EXTRACT_JSON_MULTI_MAX_INNER // 2 + 50)
    assert len(filler) > cursor_client._EXTRACT_JSON_MULTI_MAX_INNER
    expected = {"ok": True, "tail": "payload"}
    inner = filler + "\n" + json.dumps(expected)
    raw = f"```json\n{inner}\n```\n"
    assert cursor_client.extract_json(raw) == expected


def test_json_call_plan_uses_main_model_not_model_fast(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.cursor.model = "composer-2-main"
    cfg.cursor.model_fast = "fast-xyz"
    fake = MagicMock(returncode=0, stdout="{}", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake) as run_mock:
        cursor_client.json_call(cfg, "s", "u", max_retries=0)
    cmd = run_mock.call_args[0][0]
    assert "--mode=plan" in cmd
    assert "--trust" in cmd
    assert "--output-format=json" not in cmd
    assert "--model=composer-2-main" in cmd
    assert "--yolo" not in cmd


def test_json_call_cursor_mode_agent_uses_yolo_and_main_model(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.cursor.model = "big-m"
    fake = MagicMock(returncode=0, stdout="{}", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake) as run_mock:
        cursor_client.json_call(cfg, "s", "u", max_retries=0, cursor_mode="agent")
    cmd = run_mock.call_args[0][0]
    assert "--yolo" in cmd
    assert "--trust" in cmd
    assert "--model=big-m" in cmd
    assert "--mode=plan" not in cmd


def test_json_call_retry_on_empty(tmp_path):
    cfg = _cfg(tmp_path)
    empty = MagicMock(returncode=0, stdout="", stderr="")
    good = MagicMock(returncode=0, stdout='{"r": 1}', stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", side_effect=[empty, good]):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=1)
    assert r.success is True
    assert r.data == {"r": 1}


def test_json_call_parse_retry_succeeds_after_prose_first_attempt(tmp_path):
    """Parse-failure retry appends suffix; second subprocess result yields JSON."""
    cfg = _cfg(tmp_path)
    marker = "\n---PARSE_RETRY_MARKER_XYZ---\n"
    marker_key = "PARSE_RETRY_MARKER_XYZ"
    prose = MagicMock(
        returncode=0,
        stdout="Here is a long status narrative with **markdown** and no JSON.",
        stderr="",
    )
    payload = {"a": 1, "b": "two"}
    fenced = "```json\n" + json.dumps(payload) + "\n```\n"
    good = MagicMock(returncode=0, stdout=fenced, stderr="")
    written: list[Path] = []

    def _capture_run(cmd, *args, **kwargs):
        written.append(Path(cmd[-1]))
        if len(written) == 1:
            return prose
        return good

    with patch("meta_harness.cursor_client.subprocess.run", side_effect=_capture_run):
        r = cursor_client.json_call(
            cfg,
            "sys",
            "user body",
            label="parse_retry",
            max_retries=1,
            parse_retry_user_suffix=marker,
        )
    assert r.success is True
    assert r.data == payload
    assert len(written) == 2
    first = written[0].read_text(encoding="utf-8")
    second = written[1].read_text(encoding="utf-8")
    assert marker_key not in first
    assert marker_key in second


def test_json_call_parse_retry_succeeds_on_third_attempt_default_escalating_suffix(tmp_path):
    """max_retries=2: prose on first two attempts, valid JSON on third; default suffix escalates."""
    cfg = _cfg(tmp_path)
    p1 = MagicMock(
        returncode=0,
        stdout="First reply is narrative markdown **only**, no structured JSON.",
        stderr="",
    )
    p2 = MagicMock(
        returncode=0,
        stdout="Second reply still ignores the request and writes prose here.",
        stderr="",
    )
    payload = {"ok": True, "n": 3}
    fenced = "```json\n" + json.dumps(payload) + "\n```\n"
    good = MagicMock(returncode=0, stdout=fenced, stderr="")
    written: list[Path] = []

    def _capture_run(cmd, *args, **kwargs):
        written.append(Path(cmd[-1]))
        if len(written) == 1:
            return p1
        if len(written) == 2:
            return p2
        return good

    with patch("meta_harness.cursor_client.subprocess.run", side_effect=_capture_run):
        r = cursor_client.json_call(cfg, "s", "user body", label="three_try", max_retries=2)
    assert r.success is True
    assert r.data == payload
    assert len(written) == 3
    first = written[0].read_text(encoding="utf-8")
    second = written[1].read_text(encoding="utf-8")
    third = written[2].read_text(encoding="utf-8")
    assert "The previous reply was not valid JSON" not in first
    assert "The previous reply was not valid JSON" in second
    assert "CRITICAL: Output **only** one markdown code fence" in third


def test_json_call_parse_retry_custom_suffix_escalates_on_later_retries(tmp_path):
    """max_retries=2 with parse_retry_user_suffix: first retry is suffix-only; second adds strict default."""
    cfg = _cfg(tmp_path)
    custom = "CUSTOM"
    p1 = MagicMock(
        returncode=0,
        stdout="First reply is narrative markdown **only**, no structured JSON.",
        stderr="",
    )
    p2 = MagicMock(
        returncode=0,
        stdout="Second reply still prose only.",
        stderr="",
    )
    payload = {"ok": True, "n": 3}
    fenced = "```json\n" + json.dumps(payload) + "\n```\n"
    good = MagicMock(returncode=0, stdout=fenced, stderr="")
    written: list[Path] = []

    def _capture_run(cmd, *args, **kwargs):
        written.append(Path(cmd[-1]))
        if len(written) == 1:
            return p1
        if len(written) == 2:
            return p2
        return good

    with patch("meta_harness.cursor_client.subprocess.run", side_effect=_capture_run):
        r = cursor_client.json_call(
            cfg,
            "s",
            "user body",
            label="custom_escalate",
            max_retries=2,
            parse_retry_user_suffix=custom,
        )
    assert r.success is True
    assert r.data == payload
    assert len(written) == 3
    second = written[1].read_text(encoding="utf-8")
    third = written[2].read_text(encoding="utf-8")
    assert second.endswith("user body" + custom)
    assert custom in third
    assert "CRITICAL: Output **only** one markdown code fence" in third
    assert "The previous reply was not valid JSON" not in second


def test_json_call_empty_retry_second_prompt_omits_parse_suffix(tmp_path):
    """Empty-stdout retries use the base user prompt only (no parse-repair suffix)."""
    cfg = _cfg(tmp_path)
    marker = "\n---NO_EMPTY_RETRY_MARKER---\n"
    marker_key = "NO_EMPTY_RETRY_MARKER"
    empty = MagicMock(returncode=0, stdout="", stderr="")
    good = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
    written: list[Path] = []

    def _capture_run(cmd, *args, **kwargs):
        written.append(Path(cmd[-1]))
        if len(written) == 1:
            return empty
        return good

    with patch("meta_harness.cursor_client.subprocess.run", side_effect=_capture_run):
        r = cursor_client.json_call(
            cfg,
            "s",
            "base user",
            max_retries=1,
            parse_retry_user_suffix=marker,
        )
    assert r.success is True
    assert r.data == {"ok": True}
    assert len(written) == 2
    second = written[1].read_text(encoding="utf-8")
    assert marker_key not in second


def test_json_call_cursor_not_found(tmp_path):
    cfg = _cfg(tmp_path)
    with patch(
        "meta_harness.cursor_client.subprocess.run",
        side_effect=FileNotFoundError("agent missing"),
    ):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_AGENT_BINARY_MISSING
    assert r.error.startswith("[agent_fail:agent_binary_missing]")
    assert "agent missing" in r.error


def test_json_call_timeout(tmp_path):
    cfg = _cfg(tmp_path)
    with patch(
        "meta_harness.cursor_client.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="agent", timeout=1),
    ):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_TIMEOUT
    assert r.error.startswith("[agent_fail:timeout]")
    assert "TimeoutExpired" in r.error or "timed out" in r.error.lower()


def test_json_call_nonzero_uses_stderr(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=2, stdout="", stderr="only err")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_CLI_NONZERO
    assert r.error.startswith("[agent_fail:cli_nonzero]")
    assert "2" in r.error
    assert "only err" in r.error
    assert "exited with code" in r.error


def test_json_call_windows_interrupt_exit_unsigned(tmp_path):
    cfg = _cfg(tmp_path)
    code = cursor_client.STATUS_CONTROL_C_EXIT
    fake = MagicMock(returncode=code, stdout="", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_CLI_INTERRUPTED
    assert r.error.startswith("[agent_fail:cli_interrupted]")
    assert "interrupt" in r.error.lower()
    assert "batch" in r.error.lower() or "Windows" in r.error


def test_json_call_windows_interrupt_exit_signed(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=-1073741510, stdout="", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_CLI_INTERRUPTED
    assert r.error.startswith("[agent_fail:cli_interrupted]")


def test_agent_call_windows_interrupt_exit(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=cursor_client.STATUS_CONTROL_C_EXIT, stdout="", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.agent_call(cfg, "s", "u")
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_CLI_INTERRUPTED
    assert r.error.startswith("[agent_fail:cli_interrupted]")


def test_run_cursor_passes_create_no_window_when_os_name_is_nt(tmp_path):
    """Windows path: subprocess.run gets stable CREATE_NO_WINDOW (patched is_windows)."""
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=0, stdout="{}", stderr="")
    expected_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    with patch("meta_harness.platform_runtime.is_windows", return_value=True):
        with patch("meta_harness.cursor_client.subprocess.run", return_value=fake) as m:
            cursor_client.json_call(cfg, "s", "{}", max_retries=0)
    assert m.call_args.kwargs.get("creationflags") == expected_flag


def test_cursor_windows_creationflags_matches_getattr_fallback():
    """Helper returns the same flag value used for subprocess.run on Windows."""
    expected = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    with patch("meta_harness.platform_runtime.is_windows", return_value=True):
        assert cursor_client._cursor_windows_creationflags() == expected


def test_cursor_windows_creationflags_none_when_not_nt():
    with patch("meta_harness.platform_runtime.is_windows", return_value=False):
        assert cursor_client._cursor_windows_creationflags() is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="On Windows, pathlib.Path requires os.name=='nt'; the non-nt branch runs on POSIX CI.",
)
def test_run_cursor_omits_create_no_window_when_os_name_not_nt(tmp_path):
    assert os.name != "nt"
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=0, stdout="{}", stderr="")
    with patch("meta_harness.platform_runtime.is_windows", return_value=False):
        with patch("meta_harness.cursor_client.subprocess.run", return_value=fake) as m:
            cursor_client.json_call(cfg, "s", "{}", max_retries=0)
    assert "creationflags" not in m.call_args.kwargs


def test_run_cursor_passes_devnull_stdin(tmp_path):
    """Cursor subprocess must not inherit console stdin (non-interactive / Windows batch hardening)."""
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=0, stdout='{"a":1}', stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake) as m:
        cursor_client.json_call(cfg, "s", "{}", max_retries=0)
    assert m.call_args.kwargs.get("stdin") == subprocess.DEVNULL

    fake2 = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake2) as m2:
        cursor_client.agent_call(cfg, "s", "u")
    assert m2.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_json_call_nonzero_persists_last_cursor_failure(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=3, stdout="stdout peek", stderr="stderr detail")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", label="plan", max_retries=0)
    assert r.success is False
    path = cfg.harness_dir / "last_cursor_failure.txt"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "exit_code: 3" in text
    assert "stderr detail" in text
    assert "json_call:plan" in text


def test_json_call_parse_failure_persists_last_cursor_failure(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=0, stdout="not json {]", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", label="x", max_retries=0)
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_JSON_PARSE
    assert r.error.startswith("[agent_fail:json_parse]")
    path = cfg.harness_dir / "last_cursor_failure.txt"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "json_call:parse" in text
    assert "not json" in text


def test_json_call_empty_stdout_after_retries(tmp_path):
    cfg = _cfg(tmp_path)
    empty = MagicMock(returncode=0, stdout="", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=empty):
        r = cursor_client.json_call(cfg, "s", "u", label="empty", max_retries=1)
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_EMPTY_STDOUT
    assert r.error.startswith("[agent_fail:empty_stdout]")
    path = cfg.harness_dir / "last_cursor_failure.txt"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "json_call:empty" in text


def test_json_call_file_not_found_persists_last_cursor_failure(tmp_path):
    cfg = _cfg(tmp_path)
    with patch(
        "meta_harness.cursor_client.subprocess.run",
        side_effect=FileNotFoundError("agent missing"),
    ):
        r = cursor_client.json_call(cfg, "s", "u", label="t", max_retries=0)
    assert r.success is False
    path = cfg.harness_dir / "last_cursor_failure.txt"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "json_call:t" in text
    assert "agent missing" in text


def test_agent_call_nonzero_persists_last_cursor_failure(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=1, stdout="", stderr="agent stderr")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.agent_call(cfg, "s", "u", label="exec")
    assert r.success is False
    path = cfg.harness_dir / "last_cursor_failure.txt"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "agent_call:exec" in text
    assert "agent stderr" in text


def test_json_call_nonzero_stdout_when_no_stderr(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=1, stdout="out msg", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert r.success is False
    assert "1" in r.error
    assert "out msg" in r.error
    assert "exited with code" in r.error


def test_json_call_nonzero_empty_stdout_stderr(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=7, stdout="", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.json_call(cfg, "s", "u", max_retries=0)
    assert r.success is False
    assert "7" in r.error
    assert "(no stdout/stderr)" in r.error
    assert "exited with code" in r.error


def test_complete_raises_on_agent_failure(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=1, stdout="", stderr="nope")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        with pytest.raises(RuntimeError) as excinfo:
            cursor_client.complete(cfg, "s", "u")
    err = str(excinfo.value)
    assert err.startswith("[agent_fail:cli_nonzero]")
    assert "1" in err
    assert "nope" in err
    assert cursor_client._nonzero_exit_message(1, "", "nope") in err


def test_agent_call_success(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=0, stdout="did the thing", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.agent_call(cfg, "s", "u", label="x")
    assert r.success is True
    assert r.raw == "did the thing"


def test_agent_call_failure(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        r = cursor_client.agent_call(cfg, "s", "u")
    assert r.success is False
    assert r.failure_kind == cursor_client.FAILURE_KIND_CLI_NONZERO
    assert r.error.startswith("[agent_fail:cli_nonzero]")
    assert "1" in r.error
    assert "boom" in r.error
    assert cursor_client._nonzero_exit_message(1, "", "boom") in r.error


def test_prompt_file_written(tmp_path):
    cfg = _cfg(tmp_path)
    fake = MagicMock(returncode=0, stdout="{}", stderr="")
    with patch("meta_harness.cursor_client.subprocess.run", return_value=fake):
        cursor_client.json_call(cfg, "s", "u", max_retries=0)
    md = list(cfg.prompts_dir.glob("*.md"))
    assert len(md) >= 1


def test_resolve_agent_executable_existing_path(tmp_path):
    exe = tmp_path / "my-agent.exe"
    exe.write_text("", encoding="utf-8")
    out = cursor_client._resolve_agent_executable(str(exe))
    assert out == str(exe.resolve())


def test_resolve_agent_executable_ps1_prefers_sibling_cmd(tmp_path):
    (tmp_path / "shim.ps1").write_text("", encoding="utf-8")
    (tmp_path / "shim.cmd").write_text("", encoding="utf-8")
    ps1 = str(tmp_path / "shim.ps1")
    expected_cmd = str((tmp_path / "shim.cmd").resolve())

    def _which(name: str):
        return ps1 if name == "agent" else None

    with (
        patch("meta_harness.platform_runtime.is_windows", return_value=True),
        patch("meta_harness.platform_runtime.shutil.which", side_effect=_which),
    ):
        out = cursor_client._resolve_agent_executable("agent")
    assert out == expected_cmd


def test_resolve_agent_executable_ps1_prefers_exe_over_cmd_when_both_exist(tmp_path):
    (tmp_path / "shim.ps1").write_text("", encoding="utf-8")
    (tmp_path / "shim.cmd").write_text("", encoding="utf-8")
    exe = tmp_path / "shim.exe"
    exe.write_text("", encoding="utf-8")
    ps1 = str(tmp_path / "shim.ps1")
    expected_exe = str(exe.resolve())

    def _which(name: str):
        if name == "agent":
            return ps1
        return None

    with (
        patch("meta_harness.platform_runtime.is_windows", return_value=True),
        patch("meta_harness.platform_runtime.shutil.which", side_effect=_which),
    ):
        out = cursor_client._resolve_agent_executable("agent")
    assert out == expected_exe


def test_resolve_agent_executable_ps1_prefers_agent_exe_on_path_over_cmd_shim(tmp_path):
    (tmp_path / "shim.ps1").write_text("", encoding="utf-8")
    (tmp_path / "shim.cmd").write_text("", encoding="utf-8")
    path_exe = tmp_path / "bin" / "agent.exe"
    path_exe.parent.mkdir(parents=True, exist_ok=True)
    path_exe.write_text("", encoding="utf-8")
    ps1 = str(tmp_path / "shim.ps1")
    expected = str(path_exe.resolve())

    def _which(name: str):
        if name == "agent":
            return ps1
        if name == "agent.exe":
            return str(path_exe)
        return None

    with (
        patch("meta_harness.platform_runtime.is_windows", return_value=True),
        patch("meta_harness.platform_runtime.shutil.which", side_effect=_which),
    ):
        out = cursor_client._resolve_agent_executable("agent")
    assert out == expected


def test_resolve_agent_executable_which_non_ps1_returns_found(tmp_path):
    found = str(tmp_path / "bin" / "agent")
    Path(found).parent.mkdir(parents=True, exist_ok=True)
    Path(found).write_text("", encoding="utf-8")

    def _which(name: str):
        return found if name == "agent" else None

    with patch("meta_harness.platform_runtime.shutil.which", side_effect=_which):
        out = cursor_client._resolve_agent_executable("agent")
    assert out == found


def test_resolve_agent_executable_ps1_without_sibling_cmd_returns_ps1(tmp_path):
    (tmp_path / "only.ps1").write_text("", encoding="utf-8")
    ps1 = str(tmp_path / "only.ps1")

    def _which(name: str):
        return ps1 if name == "agent" else None

    with patch("meta_harness.platform_runtime.shutil.which", side_effect=_which):
        out = cursor_client._resolve_agent_executable("agent")
    assert out == ps1


def test_resolve_agent_executable_suffix_fallback_when_agent_not_on_path(tmp_path):
    cmd_path = str(tmp_path / "tools" / "agent.cmd")
    Path(cmd_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cmd_path).write_text("", encoding="utf-8")

    def _which(name: str):
        if name == "agent":
            return None
        if name == "agent.cmd":
            return cmd_path
        return None

    with (
        patch("meta_harness.platform_runtime.is_windows", return_value=True),
        patch("meta_harness.platform_runtime.shutil.which", side_effect=_which),
    ):
        out = cursor_client._resolve_agent_executable("agent")
    assert out == cmd_path


def test_resolve_agent_executable_raises_when_not_found():
    def _which(_name: str):
        return None

    with patch("meta_harness.platform_runtime.shutil.which", side_effect=_which):
        with pytest.raises(CursorAgentBinaryNotFound):
            cursor_client._resolve_agent_executable("agent")


def test_cursor_cmd_plan_argv(tmp_path):
    cfg = HarnessConfig(project_root=tmp_path)
    cfg.cursor.agent_bin = "agent"
    cfg.cursor.model_fast = "fast-m"
    prompt_path = tmp_path / "nested" / "prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("x", encoding="utf-8")

    with patch.object(
        cursor_client,
        "_resolve_agent_executable",
        return_value="resolved-agent-exe",
    ):
        cmd = cursor_client._cursor_cmd(cfg, prompt_path, "plan", "fast-m")

    assert cmd == [
        "resolved-agent-exe",
        "-p",
        "--mode=plan",
        "--trust",
        "--model=fast-m",
        str(prompt_path.resolve()),
    ]


def test_cursor_cmd_agent_argv(tmp_path):
    cfg = HarnessConfig(project_root=tmp_path)
    cfg.cursor.agent_bin = "agent"
    cfg.cursor.model = "test-model-xyz"
    prompt_path = tmp_path / "nested" / "prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("x", encoding="utf-8")

    with patch.object(
        cursor_client,
        "_resolve_agent_executable",
        return_value="resolved-agent-exe",
    ):
        cmd = cursor_client._cursor_cmd(cfg, prompt_path, "agent", "test-model-xyz")

    assert cmd == [
        "resolved-agent-exe",
        "-p",
        "--yolo",
        "--trust",
        "--model=test-model-xyz",
        str(prompt_path.resolve()),
    ]


def test_write_prompt_md_creates_hex_named_file_under_prompts(tmp_path):
    cfg = HarnessConfig(project_root=tmp_path)
    fixed = uuid.UUID("06c5600d18394efba21cbf1e908a0fc4")
    with patch("meta_harness.cursor_client.uuid.uuid4", return_value=fixed):
        path = cursor_client._write_prompt_md(cfg, "hello\n")

    assert path == tmp_path / ".metaharness" / "prompts" / "prompt_06c5600d18394efba21cbf1e908a0fc4.md"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "hello\n"
