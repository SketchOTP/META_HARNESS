"""
meta_harness/research.py

Research paper fetch, relevance evaluation (Cursor plan mode), and implementation queue.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore[misc, assignment]

from . import cursor_client
from .config import HarnessConfig

_USER_AGENT = "MetaHarness/1.0"
_FETCH_TIMEOUT = 30.0
_BODY_EXCERPT_MAX = 3000


@dataclass
class PaperContent:
    url: str
    title: str = ""
    abstract: str = ""
    body_excerpt: str = ""
    fetch_error: str = ""
    source_type: str = ""


@dataclass
class ResearchEvaluation:
    url: str
    title: str
    relevant: bool
    confidence: float
    applicable_to: str
    implementation_difficulty: str
    expected_impact: str
    recommendation: str
    reason: str
    raw: str = ""


def _http_get(url: str) -> Any:
    assert httpx is not None
    return httpx.get(
        url,
        timeout=_FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    )


def _extract_title_from_html(html: str) -> str:
    m = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>([\s\S]*?)</h1>', html, re.I)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _extract_arxiv_abstract(html: str) -> str:
    m = re.search(
        r'<blockquote[^>]*class="[^"]*abstract[^"]*"[^>]*>([\s\S]*?)</blockquote>',
        html,
        re.I,
    )
    if not m:
        return ""
    inner = m.group(1)
    inner = re.sub(r"<h2[^>]*>Abstract</h2>", "", inner, flags=re.I)
    return re.sub(r"<[^>]+>", "", inner).strip()


def _extract_html_meta_description(html: str) -> str:
    m = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)["\']',
        html,
        re.I,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r'<meta\s+content=["\']([^"\']*)["\']\s+name=["\']description["\']',
        html,
        re.I,
    )
    return m.group(1).strip() if m else ""


def _first_paragraph_text(html: str) -> str:
    m = re.search(r"<p[^>]*>([\s\S]*?)</p>", html, re.I)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()


def _parse_pdf_text(pdf_bytes: bytes, max_pages: int) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed (required for PDF text extraction)")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: list[str] = []
    n = min(max_pages, len(reader.pages))
    for i in range(n):
        try:
            t = reader.pages[i].extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            parts.append(t)
    return "\n\n".join(parts).strip()


def _arxiv_abs_and_pdf_urls(url: str) -> tuple[str, str] | None:
    p = urlparse(url)
    host = (p.netloc or "").lower()
    if "arxiv.org" not in host:
        return None
    path = p.path or ""
    m_abs = re.match(r"^/abs/([^/]+)/?$", path, re.I)
    m_pdf = re.match(r"^/pdf/([^/]+?)(?:\.pdf)?/?$", path, re.I)
    aid = (m_abs or m_pdf)
    if not aid:
        return None
    arxiv_id = aid.group(1).strip()
    if not arxiv_id:
        return None
    scheme = p.scheme or "https"
    base = f"{scheme}://{p.netloc}"
    abs_url = f"{base}/abs/{arxiv_id}"
    pdf_url = f"{base}/pdf/{arxiv_id}.pdf"
    return abs_url, pdf_url


def _is_probably_pdf_url(url: str, content_type: str | None) -> bool:
    u = url.lower().split("?", 1)[0]
    if u.endswith(".pdf"):
        return True
    ct = (content_type or "").lower()
    return "application/pdf" in ct


def _first_non_empty_paragraph(text: str) -> str:
    for block in re.split(r"\n\s*\n+", text):
        line = " ".join(block.split())
        if len(line) > 12:
            return line[:500]
    line = " ".join(text.split())
    return line[:500] if line else ""


def fetch_paper(url: str) -> PaperContent:
    u = (url or "").strip()
    if not u:
        return PaperContent(url=url, fetch_error="Empty URL")
    if httpx is None:
        return PaperContent(
            url=u,
            fetch_error="httpx is not installed. Add httpx to the environment (see meta-harness dependencies).",
        )

    try:
        arx = _arxiv_abs_and_pdf_urls(u)
        if arx is not None:
            abs_url, pdf_url = arx
            r = _http_get(abs_url)
            r.raise_for_status()
            html = r.text or ""
            title = _extract_title_from_html(html)
            abstract = _extract_arxiv_abstract(html)
            body_excerpt = ""
            if PdfReader is not None:
                try:
                    pr = _http_get(pdf_url)
                    pr.raise_for_status()
                    full = _parse_pdf_text(pr.content, max_pages=8)
                    body_excerpt = full[:_BODY_EXCERPT_MAX]
                except Exception as e:
                    return PaperContent(
                        url=u,
                        title=title,
                        abstract=abstract,
                        body_excerpt="",
                        fetch_error=str(e),
                        source_type="arxiv",
                    )
            else:
                return PaperContent(
                    url=u,
                    title=title,
                    abstract=abstract,
                    body_excerpt="",
                    fetch_error="pypdf is not installed; cannot extract PDF text for arXiv papers.",
                    source_type="arxiv",
                )
            return PaperContent(
                url=u,
                title=title,
                abstract=abstract,
                body_excerpt=body_excerpt,
                source_type="arxiv",
            )

        head = _http_get(u)
        head.raise_for_status()
        ct = head.headers.get("content-type", "")
        if _is_probably_pdf_url(u, ct):
            if PdfReader is None:
                return PaperContent(
                    url=u,
                    fetch_error="pypdf is not installed; cannot parse PDF.",
                    source_type="pdf",
                )
            try:
                text = _parse_pdf_text(head.content, max_pages=10)
            except Exception as e:
                return PaperContent(
                    url=u,
                    fetch_error=str(e),
                    source_type="pdf",
                )
            title = _first_non_empty_paragraph(text) or "PDF document"
            abstract = text[:500].strip() if text else ""
            return PaperContent(
                url=u,
                title=title[:500],
                abstract=abstract,
                body_excerpt=text[:_BODY_EXCERPT_MAX],
                source_type="pdf",
            )

        html = head.text or ""
        title = _extract_title_from_html(html)
        desc = _extract_html_meta_description(html)
        para = _first_paragraph_text(html)
        abstract = desc or para or ""
        plain = re.sub(r"<[^>]+>", " ", html)
        plain = re.sub(r"\s+", " ", plain).strip()
        body = plain[:_BODY_EXCERPT_MAX]
        return PaperContent(
            url=u,
            title=title,
            abstract=abstract[:2000],
            body_excerpt=body,
            source_type="html",
        )
    except Exception as e:
        return PaperContent(url=u, fetch_error=str(e))


_EVAL_SYSTEM = (
    "You evaluate research papers for software project relevance. "
    "Follow the user instructions and respond with exactly one ```json fenced block as requested."
)


def _eval_user_prompt(cfg: HarnessConfig, paper: PaperContent) -> str:
    v = cfg.vision
    return f"""You are evaluating a research paper for implementation relevance.

Project vision: {v.statement}
Current features (done): {", ".join(v.features_done) if v.features_done else "(none listed)"}
Roadmap (wanted): {", ".join(v.features_wanted) if v.features_wanted else "(none listed)"}

Paper title: {paper.title}
Abstract: {paper.abstract}
Body excerpt: {paper.body_excerpt}

Evaluate strictly:
- Is this paper's technique genuinely applicable to this specific project?
- What specific module or feature would benefit?
- How hard to implement realistically?
- What measurable improvement would result?

Be conservative — return relevant=false if the connection is weak or speculative.

Respond with a single ```json block:
{{
  "relevant": true/false,
  "confidence": 0.0-1.0,
  "applicable_to": "specific module or feature name",
  "implementation_difficulty": "low|medium|high",
  "expected_impact": "concrete description of improvement",
  "recommendation": "implement|monitor|discard",
  "reason": "explanation grounded in the paper content and project vision"
}}
"""


def _norm_recommendation(s: str) -> str:
    x = (s or "").strip().lower()
    if x in ("implement", "monitor", "discard"):
        return x
    return "discard"


def _norm_difficulty(s: str) -> str:
    x = (s or "").strip().lower()
    if x in ("low", "medium", "high"):
        return x
    return "medium"


def evaluate_paper(cfg: HarnessConfig, paper: PaperContent) -> ResearchEvaluation:
    base_title = paper.title or paper.url
    try:
        j_timeout = cfg.cursor.json_timeout
        if j_timeout is None:
            j_timeout = cfg.cursor.timeout_seconds
        resp = cursor_client.json_call(
            cfg,
            _EVAL_SYSTEM,
            _eval_user_prompt(cfg, paper),
            label="research_evaluate",
            timeout_seconds=j_timeout,
            max_retries=cfg.cursor.json_retries,
            cursor_mode="plan",
        )
        if not resp.success:
            err = resp.error or "unknown error"
            return ResearchEvaluation(
                url=paper.url,
                title=base_title,
                relevant=False,
                confidence=0.0,
                applicable_to="",
                implementation_difficulty="medium",
                expected_impact="",
                recommendation="discard",
                reason=f"Evaluation failed: {err}",
                raw=resp.raw or "",
            )
        data = resp.data
        if not isinstance(data, dict):
            return ResearchEvaluation(
                url=paper.url,
                title=base_title,
                relevant=False,
                confidence=0.0,
                applicable_to="",
                implementation_difficulty="medium",
                expected_impact="",
                recommendation="discard",
                reason="Evaluation failed: response was not a JSON object",
                raw=resp.raw or "",
            )
        rec = _norm_recommendation(str(data.get("recommendation", "discard")))
        rel = data.get("relevant")
        if rec == "discard":
            relevant = False
        elif isinstance(rel, bool):
            relevant = rel
        else:
            relevant = rec == "implement"
        try:
            conf = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        return ResearchEvaluation(
            url=paper.url,
            title=base_title,
            relevant=relevant,
            confidence=conf,
            applicable_to=str(data.get("applicable_to", "") or ""),
            implementation_difficulty=_norm_difficulty(str(data.get("implementation_difficulty", ""))),
            expected_impact=str(data.get("expected_impact", "") or ""),
            recommendation=rec,
            reason=str(data.get("reason", "") or ""),
            raw=resp.raw or "",
        )
    except Exception as e:
        return ResearchEvaluation(
            url=paper.url,
            title=base_title,
            relevant=False,
            confidence=0.0,
            applicable_to="",
            implementation_difficulty="medium",
            expected_impact="",
            recommendation="discard",
            reason=f"Evaluation failed: {e}",
            raw="",
        )


def _load_queue_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict) and "items" in raw and isinstance(raw["items"], list):
        return [x for x in raw["items"] if isinstance(x, dict)]
    return []


def _save_queue_list(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")


def queue_paper(cfg: HarnessConfig, evaluation: ResearchEvaluation) -> bool:
    if evaluation.recommendation != "implement":
        return False
    path = cfg.research_queue_path
    items = _load_queue_list(path)
    new_item = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "url": evaluation.url,
        "title": evaluation.title,
        "applicable_to": evaluation.applicable_to,
        "expected_impact": evaluation.expected_impact,
        "difficulty": evaluation.implementation_difficulty,
        "reason": evaluation.reason,
        "recommendation": evaluation.recommendation,
    }
    if len(items) >= 10:
        drop_idx: int | None = None
        for i, it in enumerate(items):
            r = str(it.get("recommendation", "implement") or "").lower()
            if r == "monitor":
                drop_idx = i
                break
        if drop_idx is not None:
            items.pop(drop_idx)
        else:
            return False
    items.append(new_item)
    _save_queue_list(path, items)
    return True


def get_queue(cfg: HarnessConfig) -> list[dict[str, Any]]:
    return list(_load_queue_list(cfg.research_queue_path))


def clear_queue_item(cfg: HarnessConfig, url: str) -> bool:
    target = (url or "").strip()
    if not target:
        return False
    path = cfg.research_queue_path
    items = _load_queue_list(path)
    out = [x for x in items if str(x.get("url", "")).strip() != target]
    if len(out) == len(items):
        return False
    _save_queue_list(path, out)
    return True


def format_slack_verdict(evaluation: ResearchEvaluation) -> str:
    title = evaluation.title or evaluation.url
    diff = evaluation.implementation_difficulty
    if evaluation.recommendation == "implement":
        conf_pct = f"{evaluation.confidence:.0%}"
        return (
            "📄 *Research Review — Queued for Implementation*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*{title}*\n"
            f"Applicable to: `{evaluation.applicable_to}`\n"
            f"Difficulty: {diff} | Confidence: {conf_pct}\n"
            f"Impact: {evaluation.expected_impact}\n"
            f"Reason: {evaluation.reason}\n\n"
            "✅ Added to product agent queue. Will be considered in next product cycle.\n"
            f"Tap to veto: `/metaharness research discard {evaluation.url}`"
        )
    if evaluation.recommendation == "monitor":
        return (
            "📄 *Research Review — Interesting, Not Yet*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*{title}*\n"
            f"Reason: {evaluation.reason}\n\n"
            "👀 Saved to research backlog. Not queued for immediate implementation."
        )
    conf_pct = f"{evaluation.confidence:.0%}"
    return (
        "📄 *Research Review — Not Applicable*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*{title}*\n"
        f"Confidence: {conf_pct}\n"
        f"Reason: {evaluation.reason}"
    )
