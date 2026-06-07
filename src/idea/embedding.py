"""OpenAI text-embedding-3-small 호출 + cosine 유틸.

OpenRouter는 (현재) embeddings endpoint 미지원이라 OpenAI direct가 1순위.
환경변수:
  - OPENAI_API_KEY (1순위)
  - OPENROUTER_API_KEY (폴백 — embeddings 미지원이면 명시적 에러)

text-embedding-3-small:
  - 1536-dim float32 vector
  - $0.02 / 1M tokens — 2000 종목 × ~500 tokens = ~$0.02 bootstrap
  - normalize=True로 호출 시 unit vector → cosine = dot product

저장 포맷: np.float32 1536-dim → tobytes() 6144 bytes/ticker.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
from openai import OpenAI

log = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536  # text-embedding-3-small 기본 차원

KST = timezone(timedelta(hours=9))

# OpenAI embeddings: 1 request당 max 300K tokens cap (max_tokens_per_request).
# 한국어 ~3 chars/token이라 12000 chars × 50 = 600K chars / 3 = 200K tokens 정도.
# 안전 마진 유지 (BadRequestError 발생 시 자동 분할 재시도도 추가).
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_TOKENS_PER_INPUT = 4000  # text-embedding-3-small은 8191 max, 4000으로 cap


class EmbeddingError(RuntimeError):
    pass


def _get_client() -> tuple[OpenAI, str]:
    """OpenAI direct 우선, OpenRouter 폴백. (client, provider) 반환."""
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return OpenAI(api_key=openai_key, timeout=60.0, max_retries=2), "openai"
    or_key = os.getenv("OPENROUTER_API_KEY")
    if or_key:
        log.warning(
            "[embedding] OPENAI_API_KEY 없음 — OpenRouter 폴백 시도 "
            "(embeddings 미지원 가능, 실패 시 OPENAI_API_KEY 설정 필요)"
        )
        return (
            OpenAI(
                api_key=or_key,
                base_url="https://openrouter.ai/api/v1",
                timeout=60.0,
                max_retries=2,
                default_headers={
                    "HTTP-Referer": "https://github.com/anselmkim111/basketball-rag-agent",
                    "X-Title": "idea-bot-universe-scan",
                },
            ),
            "openrouter",
        )
    raise EmbeddingError("OPENAI_API_KEY 또는 OPENROUTER_API_KEY 필요")


def _truncate_for_embed(text: str, max_chars: int = DEFAULT_MAX_TOKENS_PER_INPUT * 3) -> str:
    """텍스트가 너무 길면 앞부분만. 한국어는 ~3 char/token 가정.

    "사업의 내용" 첫 부분이 가장 중요(회사 개요 + 주력 사업부) — 뒷부분 잘려도 OK.
    """
    if not text:
        return ""
    return text[:max_chars]


def embed_texts(
    texts: list[str],
    model: str = DEFAULT_EMBED_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    normalize: bool = True,
) -> list[np.ndarray]:
    """입력 텍스트 리스트 → float32 1536-dim 벡터 리스트.

    빈 텍스트는 zero vector. 결정성: temperature 개념 없음, deterministic.
    """
    if not texts:
        return []
    client, provider = _get_client()
    out: list[np.ndarray] = [None] * len(texts)  # type: ignore

    # 빈 텍스트는 미리 zero vector 처리, 실 호출은 non-empty만
    to_embed: list[tuple[int, str]] = []
    for i, t in enumerate(texts):
        if not t or not t.strip():
            out[i] = np.zeros(EMBED_DIM, dtype=np.float32)
        else:
            to_embed.append((i, _truncate_for_embed(t)))

    if not to_embed:
        return out  # type: ignore

    def _call_with_split(chunk: list[tuple[int, str]]) -> None:
        """단일 batch 호출 + max_tokens_per_request 초과 시 절반으로 분할 재시도."""
        if not chunk:
            return
        inputs = [c[1] for c in chunk]
        try:
            resp = client.embeddings.create(model=model, input=inputs)
        except Exception as e:
            msg = str(e)
            if "404" in msg or "not found" in msg.lower():
                raise EmbeddingError(
                    f"[{provider}] embeddings endpoint 미지원 또는 모델 '{model}' 없음. "
                    "OPENAI_API_KEY 설정 권장."
                ) from e
            # 300K tokens cap 초과 — 절반으로 분할 재시도
            if "max_tokens_per_request" in msg or "max tokens per request" in msg.lower():
                if len(chunk) <= 1:
                    log.warning("[embedding] 단일 텍스트가 300K 초과 — skip idx=%d", chunk[0][0])
                    return
                mid = len(chunk) // 2
                log.info("[embedding] batch %d → %d+%d로 분할 (tokens cap)", len(chunk), mid, len(chunk) - mid)
                _call_with_split(chunk[:mid])
                _call_with_split(chunk[mid:])
                return
            raise
        for (orig_idx, _txt), data in zip(chunk, resp.data):
            vec = np.asarray(data.embedding, dtype=np.float32)
            if normalize:
                n = np.linalg.norm(vec)
                if n > 0:
                    vec = vec / n
            out[orig_idx] = vec

    for batch_start in range(0, len(to_embed), batch_size):
        chunk = to_embed[batch_start: batch_start + batch_size]
        _call_with_split(chunk)
        # 진행 로그 큰 batch만
        if len(to_embed) >= 500:
            log.info(
                "[embedding] batch %d/%d 완료 (size=%d, model=%s)",
                batch_start // batch_size + 1,
                (len(to_embed) + batch_size - 1) // batch_size,
                len(chunk), model,
            )
        # OpenAI는 rate limit 안 걸리지만 안전 마진
        time.sleep(0.02)
    return out  # type: ignore


def embed_one(text: str, model: str = DEFAULT_EMBED_MODEL) -> np.ndarray:
    return embed_texts([text], model=model)[0]


def cosine_top_n(
    query_emb: np.ndarray,
    candidate_embs: dict[str, np.ndarray],
    n: int,
) -> list[tuple[str, float]]:
    """deterministic cosine + sort. tie-break은 ticker 오름차순.

    Args:
        query_emb: shape (1536,) normalized 가정 (embed_texts default).
        candidate_embs: {ticker: vec} — 전부 normalized 가정.
        n: top-N.

    Returns:
        [(ticker, score), ...] 내림차순. score는 [-1, 1] cosine.
    """
    if not candidate_embs or query_emb is None:
        return []
    qn = np.linalg.norm(query_emb)
    if qn <= 0:
        return []
    q = query_emb if abs(qn - 1.0) < 1e-6 else query_emb / qn

    items: list[tuple[str, float]] = []
    for t, v in candidate_embs.items():
        if v is None or v.size == 0:
            continue
        vn = np.linalg.norm(v)
        if vn <= 0:
            continue
        vv = v if abs(vn - 1.0) < 1e-6 else v / vn
        s = float(np.dot(q, vv))
        items.append((t, s))
    # (score desc, ticker asc) — tie-break 결정성
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    return items[: n]


def bytes_to_vec(b: Optional[bytes]) -> Optional[np.ndarray]:
    """DB BLOB → np.float32 vector. None/짧으면 None."""
    if not b or len(b) < EMBED_DIM * 4:
        return None
    return np.frombuffer(b, dtype=np.float32)


def vec_to_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def now_kst_date() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")
