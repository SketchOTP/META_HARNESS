"""
meta_harness/cursor_client.py

Cursor Agent CLI invocations:
  - Plan / JSON phases (ANALYZE, diagnose): `agent -p --mode=plan --trust --model=...` (stdout parsed via extract_json)
  - Agent / write phases (EXECUTE JSON body, proposer): `agent -p --yolo --trust --model=...`
  ``--trust`` avoids interactive workspace-trust prompts (notably on Windows) that hang subprocess runs.
  Subprocess stdin is closed (``DEVNULL``) so unattended runs do not inherit a console stdin (avoids batch/cmd interactive prompts and stuck ``Terminate batch job``-class failures when a shim touches the console).

Prompts are written as `.md` under `.metaharness/prompts/`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from rich.console import Console

from .config import HarnessConfig
from .platform_runtime import (
    merge_subprocess_no_window_kwargs,
    resolve_cursor_agent_executable,
)

_console = Console()

def _default_parse_retry_suffix(parse_fail_index: int) -> str:
    """Escalating repair text for parse-only retries when ``parse_retry_user_suffix`` is not set.

    ``parse_fail_index`` is 0 for the first parse retry, 1 for the second, and so on.
    """
    mild = (
        "\n\n---\n"
        "The previous reply was not valid JSON for this harness step. "
        "Reply with exactly one markdown fenced code block using the language tag `json`; "
        "inside that fence, output only a JSON value (no other text before or after the fence).\n"
    )
    if parse_fail_index <= 0:
        return mild
    strict = (
        "\n\n---\n"
        "CRITICAL: Output **only** one markdown code fence labeled ```json containing a single JSON value. "
        "No preamble, no explanation, no other markdown outside that fence.\n"
    )
    return strict

# Stable failure kinds for Cursor CLI / JSON / subprocess outcomes (see AgentFailureKind).
FAILURE_KIND_CLI_NONZERO = "CLI_NONZERO"
FAILURE_KIND_CLI_INTERRUPTED = "CLI_INTERRUPTED"
FAILURE_KIND_JSON_PARSE = "JSON_PARSE"
FAILURE_KIND_EMPTY_STDOUT = "EMPTY_STDOUT"
FAILURE_KIND_TIMEOUT = "TIMEOUT"
FAILURE_KIND_AGENT_BINARY_MISSING = "AGENT_BINARY_MISSING"

# Windows NTSTATUS STATUS_CONTROL_C_EXIT (0xC000013A). subprocess may report 3221225786
# (unsigned) or -1073741510 (signed 32-bit) depending on the caller.
STATUS_CONTROL_C_EXIT = 3221225786

AgentFailureKind = Literal[
    "CLI_NONZERO",
    "CLI_INTERRUPTED",
    "JSON_PARSE",
    "EMPTY_STDOUT",
    "TIMEOUT",
    "AGENT_BINARY_MISSING",
]


def _agent_failure_message(kind: AgentFailureKind, detail: str) -> str:
    """Prefix human-readable detail with a stable [agent_fail:...] tag (lowercase slug)."""
    slug = kind.lower()
    return f"[agent_fail:{slug}] {detail}"


@dataclass
class CursorResponse:
    success: bool
    data: Any = None
    error: str = ""
    raw: str = ""
    failure_kind: str | None = None


# extract_json fallback: cap work when scanning many `{`/`[` starts (noisy stdout).
_EXTRACT_JSON_MULTI_MAX_STARTS = 128
_EXTRACT_JSON_MULTI_MAX_OFFSET = 65_536
_EXTRACT_JSON_MULTI_MAX_INNER = 65_536
_EXTRACT_JSON_MULTI_MAX_STARTS_CAP = 4096


def _raw_decode_first_json_value(s: str) -> Any | None:
    """Decode the first JSON value in ``s`` via ``JSONDecoder.raw_decode``, allowing trailing prose."""
    decoder = json.JSONDecoder()
    i_curly = s.find("{")
    i_square = s.find("[")
    candidates: list[int] = []
    if i_curly >= 0:
        candidates.append(i_curly)
    if i_square >= 0:
        candidates.append(i_square)
    if not candidates:
        return None
    candidates.sort()
    for start in candidates:
        try:
            obj, _end = decoder.raw_decode(s, start)
            return obj
        except json.JSONDecodeError:
            continue
    return None


def _balanced_chunk(t: str, start: int) -> str | None:
    """Return the balanced {...} or [...] segment starting at ``start``, or None if unclosed."""
    if start < 0 or start >= len(t):
        return None
    opener = t[start]
    if opener not in "{[":
        return None
    close = "}" if opener == "{" else "]"
    depth = 0
    for i in range(start, len(t)):
        c = t[i]
        if c == opener:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return None


def _suffix_oriented_json_extract(t: str) -> Any | None:
    """
    Last-resort extraction when the full string and normal fence pass miss JSON that only
    appears after a late fence (e.g. huge preamble hitting scan caps, or ``` inside a string
    confusing the primary fence regex). Try ``_multi_candidate_json_scan`` on the substring
    starting at each `` ``` `` from the end backward.
    """
    pos = len(t)
    while True:
        idx = t.rfind("```", 0, pos)
        if idx < 0:
            return None
        tail = t[idx:]
        if (r := _multi_candidate_json_scan(tail)) is not None:
            return r
        if idx == 0:
            return None
        pos = idx


def _extract_json_via_fenced_raw_decode(t: str) -> Any | None:
    """
    Parse JSON from ```lang fenced blocks using JSONDecoder.raw_decode so string values
    may contain literal triple-backtick runs without the non-greedy fence regex splitting
    the payload.
    """
    decoder = json.JSONDecoder()
    openings: list[int] = []
    for m in re.finditer(r"```\s*(\w+)\s*(?:\r?\n)+", t):
        openings.append(m.end())
    for start in reversed(openings):
        rest = t[start:]
        try:
            obj, _idx = decoder.raw_decode(rest)
        except json.JSONDecodeError:
            continue
        # Last fence in document order that yields a full JSON value (reversed iteration);
        # accepts unclosed fences and trailing prose after the value.
        return obj
    return None


def _multi_candidate_json_scan(t: str) -> Any | None:
    """
    Try json.loads on each balanced `{`/`[` segment in order of appearance (capped).
    Used when the first balanced segment is invalid JSON but a later one is valid.
    """

    def _try(s: str) -> Any | None:
        s = s.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None

    n = min(len(t), _EXTRACT_JSON_MULTI_MAX_OFFSET)
    max_starts = min(
        _EXTRACT_JSON_MULTI_MAX_STARTS_CAP,
        max(_EXTRACT_JSON_MULTI_MAX_STARTS, len(t) // 2 + 128),
    )
    starts: list[int] = []
    for i in range(n):
        if t[i] in "{[":
            starts.append(i)
            if len(starts) >= max_starts:
                break
    for start in starts:
        chunk = _balanced_chunk(t, start)
        if chunk is None:
            continue
        if (r := _try(chunk)) is not None:
            return r
    return None


def extract_json(text: str) -> Any | None:
    """Parse JSON from bare string, fenced ``` blocks (prefers ```json), or embedded object/array."""
    if text is None:
        return None
    t = text.strip()
    if t.startswith("\ufeff"):
        t = t[1:]
    if not t:
        return None

    def _try(s: str) -> Any | None:
        s = s.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None

    if (r := _try(t)) is not None:
        return r

    if (r := _extract_json_via_fenced_raw_decode(t)) is not None:
        return r

    # All fenced blocks in document order; if multiple fences yield valid JSON, prefer the
    # last (composer often emits a schema/example first and the real payload last).
    last_from_fence: Any | None = None
    for m in re.finditer(r"```\s*(\w*)\s*([\s\S]*?)```", t, re.IGNORECASE):
        inner = m.group(2)
        part = _try(inner)
        if part is None and inner.strip():
            if len(inner) <= _EXTRACT_JSON_MULTI_MAX_INNER:
                part = _multi_candidate_json_scan(inner)
            else:
                # composer may put many tokens inside one fence; JSON may be only at the end
                tail = inner[-_EXTRACT_JSON_MULTI_MAX_OFFSET:]
                part = _multi_candidate_json_scan(tail)
                if part is None:
                    part = _suffix_oriented_json_extract(tail)
        if part is not None:
            last_from_fence = part
    if last_from_fence is not None:
        return last_from_fence

    if (r := _raw_decode_first_json_value(t)) is not None:
        return r

    # First balanced `{` / `[` only (backward compatible with older harness behavior).
    for opener in ("{", "["):
        start = t.find(opener)
        if start < 0:
            continue
        chunk = _balanced_chunk(t, start)
        if chunk is not None and (r := _try(chunk)) is not None:
            return r

    # Fallback: later balanced segments or JSON inside a fence after a bad first object.
    if (r := _multi_candidate_json_scan(t)) is not None:
        return r

    if (r := _suffix_oriented_json_extract(t)) is not None:
        return r

    return None


def _resolve_agent_executable(bin_name: str) -> str:
    """Resolve ``agent`` CLI path; raises :class:`platform_runtime.CursorAgentBinaryNotFound` if missing."""
    return resolve_cursor_agent_executable(bin_name)


def _write_prompt_md(cfg: HarnessConfig, body: str) -> Path:
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    name = f"prompt_{uuid.uuid4().hex}.md"
    path = cfg.prompts_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def _unwrap_cursor_envelope(text: str) -> str:
    """If Cursor wrapped output in {type: result, result: ...}, unwrap inner payload for extract_json."""
    if not text or not text.strip():
        return text
    try:
        outer = json.loads(text.strip())
    except json.JSONDecodeError:
        return text
    if not isinstance(outer, dict) or outer.get("type") != "result":
        return text
    inner = outer.get("result", text)
    if isinstance(inner, (dict, list)):
        return json.dumps(inner, ensure_ascii=False)
    if inner is None:
        return text
    return str(inner)


_FAILURE_FILE_UTF8_CAP = 10_240
_STD_STREAM_CAP = 4000
_EXTRA_CAP = 2000


def _persist_last_cursor_failure(
    cfg: HarnessConfig,
    *,
    label: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    extra: str = "",
) -> None:
    """Write a bounded UTF-8 artifact under harness_dir for diagnosis (stderr/stdout may contain paths)."""
    try:
        cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    ex = (extra or "").strip()

    def _trim(s: str, limit: int) -> tuple[str, bool]:
        if len(s) <= limit:
            return s, False
        return s[:limit] + "\n...(truncated)", True

    ex_body, ex_trunc = _trim(ex, _EXTRA_CAP)
    err_body, err_trunc = _trim(err, _STD_STREAM_CAP)
    out_body, out_trunc = _trim(out, _STD_STREAM_CAP)

    lines = [
        f"timestamp_utc: {ts}",
        f"label: {label}",
        f"exit_code: {exit_code if exit_code is not None else 'n/a'}",
    ]
    if ex_body:
        lines.append("extra:")
        lines.extend(ex_body.splitlines())
        if ex_trunc:
            lines.append("(extra truncated)")
    lines.append("stderr:")
    lines.extend((err_body if err_body else "(empty)").splitlines())
    if err_trunc:
        lines.append("(stderr truncated)")
    lines.append("stdout:")
    lines.extend((out_body if out_body else "(empty)").splitlines())
    if out_trunc:
        lines.append("(stdout truncated)")

    text = "\n".join(lines) + "\n"
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > _FAILURE_FILE_UTF8_CAP:
        text = (
            encoded[: _FAILURE_FILE_UTF8_CAP - 32].decode("utf-8", errors="replace")
            + "\n...(file truncated)\n"
        )

    try:
        (cfg.harness_dir / "last_cursor_failure.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _is_windows_process_interrupt(exit_code: int) -> bool:
    """True for STATUS_CONTROL_C_EXIT (Ctrl+C / batch interrupt class exits on Windows)."""
    if exit_code == STATUS_CONTROL_C_EXIT:
        return True
    if exit_code == -1073741510:
        return True
    return (exit_code & 0xFFFFFFFF) == 0xC000013A


def _cli_interrupted_detail() -> str:
    return (
        "The Cursor agent subprocess was terminated or interrupted at the OS or batch layer "
        "(not a normal agent error). On Windows, run the harness from PowerShell, invoke "
        "agent.exe directly, or avoid interactive cmd-based wrappers."
    )


def _nonzero_exit_message(code: int, stdout: str, stderr: str) -> str:
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    if err:
        detail = err
    elif out:
        detail = out
    else:
        detail = "(no stdout/stderr)"
    return f"Cursor agent CLI exited with code {code}: {detail}"


def _cursor_windows_creationflags() -> int | None:
    """Deprecated: use ``platform_runtime.subprocess_creationflags_no_window``."""
    from .platform_runtime import subprocess_creationflags_no_window

    return subprocess_creationflags_no_window()


def _cursor_cmd(
    cfg: HarnessConfig,
    prompt_path: Path,
    mode: Literal["plan", "agent"],
    model: str,
) -> list[str]:
    exe = _resolve_agent_executable(cfg.cursor.agent_bin)
    path = str(prompt_path.resolve())
    if mode == "plan":
        return [
            exe,
            "-p",
            "--mode=plan",
            "--trust",
            f"--model={model}",
            path,
        ]
    return [exe, "-p", "--yolo", "--trust", f"--model={model}", path]


def _run_cursor(
    cfg: HarnessConfig,
    system: str,
    user: str,
    *,
    mode: Literal["plan", "agent"],
    model: str,
    timeout_seconds: int | None = None,
    failure_label: str = "cursor",
) -> tuple[int, str, str]:
    c = cfg.cursor
    timeout = timeout_seconds if timeout_seconds is not None else c.timeout_seconds
    workspace = str(cfg.project_root.resolve())
    combined = (
        "# System instructions\n\n"
        + system.strip()
        + "\n\n---\n\n# Task / context\n\n"
        + user.strip()
    )
    prompt_path = _write_prompt_md(cfg, combined)
    cmd = _cursor_cmd(cfg, prompt_path, mode, model)
    if os.environ.get("META_HARNESS_DEBUG"):
        _console.print(f"[dim]Cursor cmd: {' '.join(str(x) for x in cmd)}[/dim]")
    env = os.environ.copy()
    run_kw: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
        "timeout": timeout,
        "env": env,
        "cwd": workspace,
    }
    run_kw.update(merge_subprocess_no_window_kwargs())
    proc = subprocess.run(cmd, **run_kw)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        _persist_last_cursor_failure(
            cfg,
            label=failure_label,
            exit_code=proc.returncode,
            stdout=out,
            stderr=err,
        )
    return proc.returncode, out, err


def json_call(
    cfg: HarnessConfig,
    system: str,
    user: str,
    *,
    label: str = "json",
    timeout_seconds: int | None = None,
    max_retries: int = 1,
    cursor_mode: Literal["plan", "agent"] = "plan",
    parse_retry_user_suffix: str | None = None,
) -> CursorResponse:
    """
    Run the agent; stdout must contain JSON (prefer ```json fenced block for Cursor CLI;
    bare or embedded JSON also supported — see extract_json).
    Retries on empty stdout and on parse failure up to max_retries.
    When ``max_retries > 0``, a retry triggered by a **parse** failure (non-empty stdout that
    does not yield JSON) sends the same user prompt plus escalating default repair suffixes
    (milder, then stricter) that ask for fenced JSON, unless ``parse_retry_user_suffix`` is
    set: the **first** parse-only retry appends only that string; **later** parse-only
    retries append the same escalating default paragraphs after it (matching the no-suffix
    curve). Retries for **empty** stdout do not append parse-repair suffixes.

    cursor_mode:
      - ``plan``: read-only plan mode; uses ``cfg.cursor.model`` (same as EXECUTE) so structured JSON instructions are followed reliably.
      - ``agent``: ``--yolo`` (implementation); uses ``cfg.cursor.model``. JSON is parsed from stdout via ``extract_json`` (and ``_unwrap_cursor_envelope`` when Cursor wraps output).
    """
    model = cfg.cursor.model
    last_raw = ""
    last_err = ""
    user_base = user.strip()
    prev_failure_kind: Literal["empty", "parse"] | None = None
    parse_retry_index = 0
    for attempt in range(max_retries + 1):
        if attempt == 0:
            user_msg = user_base
        elif prev_failure_kind == "parse":
            if parse_retry_user_suffix is not None:
                if parse_retry_index == 0:
                    suffix = parse_retry_user_suffix
                else:
                    suffix = parse_retry_user_suffix + _default_parse_retry_suffix(
                        parse_retry_index
                    )
            else:
                suffix = _default_parse_retry_suffix(parse_retry_index)
            parse_retry_index += 1
            user_msg = user_base + suffix
        else:
            user_msg = user_base
        try:
            code, out, err = _run_cursor(
                cfg,
                system,
                user_msg,
                mode=cursor_mode,
                model=model,
                timeout_seconds=timeout_seconds,
                failure_label=f"json_call:{label}",
            )
        except FileNotFoundError as e:
            hint = f" (agent binary: {cfg.cursor.agent_bin})"
            _persist_last_cursor_failure(
                cfg,
                label=f"json_call:{label}",
                exit_code=None,
                stdout=last_raw,
                stderr=last_err,
                extra=str(e) + hint,
            )
            detail = str(e) + hint
            return CursorResponse(
                success=False,
                failure_kind=FAILURE_KIND_AGENT_BINARY_MISSING,
                error=_agent_failure_message(FAILURE_KIND_AGENT_BINARY_MISSING, detail),
                raw="",
            )
        except subprocess.TimeoutExpired as e:
            hint = f" (agent binary: {cfg.cursor.agent_bin})"
            _persist_last_cursor_failure(
                cfg,
                label=f"json_call:{label}",
                exit_code=None,
                stdout=last_raw,
                stderr=last_err,
                extra=str(e) + hint,
            )
            detail = str(e) + hint
            return CursorResponse(
                success=False,
                failure_kind=FAILURE_KIND_TIMEOUT,
                error=_agent_failure_message(FAILURE_KIND_TIMEOUT, detail),
                raw="",
            )

        out_for_parse = _unwrap_cursor_envelope(out)
        last_raw = out_for_parse
        last_err = err

        if code != 0:
            if _is_windows_process_interrupt(code):
                return CursorResponse(
                    success=False,
                    failure_kind=FAILURE_KIND_CLI_INTERRUPTED,
                    error=_agent_failure_message(
                        FAILURE_KIND_CLI_INTERRUPTED,
                        _cli_interrupted_detail(),
                    ),
                    raw=out,
                )
            return CursorResponse(
                success=False,
                failure_kind=FAILURE_KIND_CLI_NONZERO,
                error=_agent_failure_message(
                    FAILURE_KIND_CLI_NONZERO,
                    _nonzero_exit_message(code, out, err),
                ),
                raw=out,
            )

        if not out_for_parse and attempt < max_retries:
            prev_failure_kind = "empty"
            continue

        parsed = extract_json(out_for_parse)
        if parsed is None:
            if attempt < max_retries:
                prev_failure_kind = "parse"
                continue
            if not (out_for_parse or "").strip():
                detail = (last_err or "").strip() or "empty stdout from agent"
                _persist_last_cursor_failure(
                    cfg,
                    label="json_call:empty",
                    exit_code=code,
                    stdout=out_for_parse or out,
                    stderr=err,
                    extra="No usable stdout after retries",
                )
                return CursorResponse(
                    success=False,
                    failure_kind=FAILURE_KIND_EMPTY_STDOUT,
                    error=_agent_failure_message(FAILURE_KIND_EMPTY_STDOUT, detail),
                    raw=out_for_parse,
                )
            preview = (out_for_parse or "")[:200]
            _console.print(
                f"[dim yellow]Cursor returned non-JSON. Preview: {preview}[/dim yellow]"
            )
            _persist_last_cursor_failure(
                cfg,
                label="json_call:parse",
                exit_code=code,
                stdout=out_for_parse or out,
                stderr=err,
                extra="No valid JSON in agent output after retries",
            )
            return CursorResponse(
                success=False,
                failure_kind=FAILURE_KIND_JSON_PARSE,
                error=_agent_failure_message(
                    FAILURE_KIND_JSON_PARSE,
                    "No valid JSON in agent output",
                ),
                raw=out_for_parse,
            )
        return CursorResponse(success=True, data=parsed, raw=out_for_parse)

    detail = (last_err or "").strip() or "empty stdout from agent"
    return CursorResponse(
        success=False,
        failure_kind=FAILURE_KIND_EMPTY_STDOUT,
        error=_agent_failure_message(FAILURE_KIND_EMPTY_STDOUT, detail),
        raw=last_raw,
    )


def agent_call(
    cfg: HarnessConfig,
    system: str,
    user: str,
    *,
    label: str = "agent",
    timeout_seconds: int | None = None,
) -> CursorResponse:
    """Run the agent with ``--yolo``; response is free-form text (markdown), not JSON."""
    try:
        code, out, err = _run_cursor(
            cfg,
            system,
            user,
            mode="agent",
            model=cfg.cursor.model,
            timeout_seconds=timeout_seconds,
            failure_label=f"agent_call:{label}",
        )
    except FileNotFoundError as e:
        hint = f" (agent binary: {cfg.cursor.agent_bin})"
        _persist_last_cursor_failure(
            cfg,
            label=f"agent_call:{label}",
            exit_code=None,
            stdout="",
            stderr="",
            extra=str(e) + hint,
        )
        detail = str(e) + hint
        return CursorResponse(
            success=False,
            failure_kind=FAILURE_KIND_AGENT_BINARY_MISSING,
            error=_agent_failure_message(FAILURE_KIND_AGENT_BINARY_MISSING, detail),
            raw="",
        )
    except subprocess.TimeoutExpired as e:
        hint = f" (agent binary: {cfg.cursor.agent_bin})"
        _persist_last_cursor_failure(
            cfg,
            label=f"agent_call:{label}",
            exit_code=None,
            stdout="",
            stderr="",
            extra=str(e) + hint,
        )
        detail = str(e) + hint
        return CursorResponse(
            success=False,
            failure_kind=FAILURE_KIND_TIMEOUT,
            error=_agent_failure_message(FAILURE_KIND_TIMEOUT, detail),
            raw="",
        )

    if code != 0:
        if _is_windows_process_interrupt(code):
            return CursorResponse(
                success=False,
                failure_kind=FAILURE_KIND_CLI_INTERRUPTED,
                error=_agent_failure_message(
                    FAILURE_KIND_CLI_INTERRUPTED,
                    _cli_interrupted_detail(),
                ),
                raw=out,
            )
        return CursorResponse(
            success=False,
            failure_kind=FAILURE_KIND_CLI_NONZERO,
            error=_agent_failure_message(
                FAILURE_KIND_CLI_NONZERO,
                _nonzero_exit_message(code, out, err),
            ),
            raw=out,
        )
    return CursorResponse(success=True, data=None, raw=out)


def complete(
    cfg: HarnessConfig,
    system: str,
    user: str,
    *,
    timeout_seconds: int | None = None,
) -> str:
    """
    Ask-mode style completion: same CLI as agent_call; returns stdout text.
    Raises on failure (backward compatible with diagnoser / agent phases expecting text or JSON in stdout).
    """
    resp = agent_call(cfg, system, user, label="complete", timeout_seconds=timeout_seconds)
    if not resp.success:
        raise RuntimeError(resp.error or "Cursor Agent CLI failed")
    return resp.raw

