"""
meta_harness/embedding_retrieval.py

Optional semantic recall: index completed directive bodies and retrieve similar snippets
for compact harness context. Uses SQLite for vectors; OpenAI or local sentence-transformers.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .config import HarnessConfig
    from .evidence import Evidence

_DDL = """
CREATE TABLE IF NOT EXISTS embedding_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  directive_id TEXT NOT NULL,
  text TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vec_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emb_directive ON embedding_chunks(directive_id);
"""


def _now() -> str:
    from datetime import datetime

    return datetime.utcnow().isoformat() + "Z"


def _normalize(v: list[float]) -> list[float]:
    s = math.sqrt(sum(x * x for x in v))
    if s <= 0:
        return v
    return [x / s for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _chunk_text(text: str, max_chars: int) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]
    chunks: list[str] = []
    step = max(200, max_chars // 2)
    i = 0
    while i < len(t):
        chunks.append(t[i : i + max_chars])
        i += step
    return chunks[:24]


def _embed_openai(cfg: "HarnessConfig", texts: list[str]) -> list[list[float]]:
    import os

    import httpx

    ec = cfg.embedding
    key = os.environ.get(ec.api_key_env, "")
    if not key.strip():
        return [[] for _ in texts]
    base = (ec.base_url or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/embeddings"
    out: list[list[float]] = []
    batch = 16
    for i in range(0, len(texts), batch):
        sub = texts[i : i + batch]
        payload = {"model": ec.model, "input": sub}
        with httpx.Client(timeout=120.0) as client:
            r = client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        embs: list[Optional[list[float]]] = [None for _ in sub]
        for item in data.get("data", []):
            idx = int(item.get("index", 0))
            emb = item.get("embedding")
            if isinstance(emb, list) and 0 <= idx < len(embs):
                embs[idx] = [float(x) for x in emb]
        for row in embs:
            if row is None:
                out.append([])
            else:
                out.append(_normalize(row))
    return out


def _embed_local(cfg: "HarnessConfig", texts: list[str]) -> list[list[float]]:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return [[] for _ in texts]
    ec = cfg.embedding
    model = SentenceTransformer(ec.local_model_id)
    vecs = model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    out: list[list[float]] = []
    for row in vecs:
        try:
            out.append([float(x) for x in row.tolist()])
        except AttributeError:
            out.append([float(x) for x in list(row)])
    return out


def embed_batch(cfg: "HarnessConfig", texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    ec = cfg.embedding
    if ec.provider == "local":
        return _embed_local(cfg, texts)
    return _embed_openai(cfg, texts)


class EmbeddingIndex:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def clear_directive(self, directive_id: str) -> None:
        self._conn.execute("DELETE FROM embedding_chunks WHERE directive_id = ?", (directive_id,))
        self._conn.commit()

    def upsert_chunks(
        self,
        directive_id: str,
        chunks: list[tuple[str, list[float]]],
    ) -> None:
        self.clear_directive(directive_id)
        ts = _now()
        for text, vec in chunks:
            if not vec:
                continue
            self._conn.execute(
                """INSERT INTO embedding_chunks (directive_id, text, dim, vec_json, created_at)
                   VALUES (?,?,?,?,?)""",
                (directive_id, text, len(vec), json.dumps(vec), ts),
            )
        self._conn.commit()

    def all_vectors(self) -> list[tuple[int, str, str, list[float]]]:
        rows = self._conn.execute(
            "SELECT id, directive_id, text, vec_json FROM embedding_chunks"
        ).fetchall()
        out: list[tuple[int, str, str, list[float]]] = []
        for r in rows:
            try:
                vec = json.loads(r["vec_json"])
                if isinstance(vec, list):
                    out.append(
                        (
                            int(r["id"]),
                            str(r["directive_id"]),
                            str(r["text"]),
                            [float(x) for x in vec],
                        )
                    )
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                continue
        return out


def index_directive_body(cfg: "HarnessConfig", directive_id: str, directive_text: str) -> None:
    """Embed and store directive text chunks (no-op if disabled or empty)."""
    if not cfg.embedding.enabled or not directive_id or not (directive_text or "").strip():
        return
    ec = cfg.embedding
    chunks = _chunk_text(directive_text, ec.max_chunk_chars)
    if not chunks:
        return
    vecs = embed_batch(cfg, chunks)
    pairs: list[tuple[str, list[float]]] = []
    for text, vec in zip(chunks, vecs):
        if vec:
            pairs.append((text, vec))
    if not pairs:
        return
    idx = EmbeddingIndex(cfg.embedding_index_path)
    try:
        idx.upsert_chunks(directive_id, pairs)
    finally:
        idx.close()


def _query_text_from_evidence(ev: "Evidence") -> str:
    parts: list[str] = []
    if ev.tests.failed_names:
        parts.append(" ".join(ev.tests.failed_names[:40]))
    if ev.error_patterns:
        parts.append(" ".join(p.get("pattern", "") for p in ev.error_patterns[:10]))
    tail = (ev.test_results or "")[-1500:]
    if tail.strip():
        parts.append(tail)
    if ev.log_tail:
        parts.append(ev.log_tail[-1500:])
    return " ".join(parts).strip()


def retrieve_compact_lines(cfg: "HarnessConfig", ev: Optional["Evidence"]) -> list[str]:
    """Return 0–top_k lines like `id ~0.42 short snippet` for compact context."""
    if not cfg.embedding.enabled or ev is None:
        return []
    qtext = _query_text_from_evidence(ev)
    if len(qtext) < 12:
        return []
    ec = cfg.embedding
    qvecs = embed_batch(cfg, [qtext[:8000]])
    if not qvecs or not qvecs[0]:
        return []
    q = qvecs[0]
    path = cfg.embedding_index_path
    if not path.is_file():
        return []
    idx = EmbeddingIndex(path)
    try:
        rows = idx.all_vectors()
    finally:
        idx.close()
    if not rows:
        return []
    scored: list[tuple[float, str, str]] = []
    for _cid, did, text, vec in rows:
        if len(vec) != len(q):
            continue
        sim = _dot(q, vec)
        scored.append((sim, did, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    lines = []
    seen: set[tuple[str, str]] = set()
    for sim, did, text in scored:
        if sim < ec.min_similarity:
            continue
        snippet = " ".join(text.split())[:120]
        key = (did, snippet[:40])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{did} ~{sim:.2f} {snippet}")
        if len(lines) >= ec.top_k:
            break
    return lines
