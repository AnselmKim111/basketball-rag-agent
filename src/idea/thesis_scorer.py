"""Universe-wide deterministic thesis scoring.

사용자 통찰: "매번 sampling할때마다 똑같은 아이디어인데 산점도가 달라진다 —
sampling 범위가 전범위 전종목이 아니기 때문." 정확히 이 모듈이 해결.

흐름 (per-thesis, deterministic):
  1) thesis_text + research dict 결합 → 1개 쿼리 텍스트
  2) text-embedding-3-small로 embed
  3) screener.db에서 embedding 보유 universe 로드
  4) cosine top-N (default 300)
  5) (옵션) LLM batch refinement (kimi) — 300 × 약식 매칭점수 → top60 정렬
  6) 결과: [{ticker, name, sector, market_cap, business_text_snippet,
              cosine_score, match_score, source: "universe_scan"}]

결정성 보장:
  - embedding API는 결정적 (temperature 개념 없음)
  - cosine은 순수 수학
  - LLM batch는 temperature=0 + seed 명시
  - tie-break: cosine score desc → ticker asc
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import numpy as np

from src.idea import embedding as emb_mod
from src.screener import db as screener_db

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent.parent
PROMPT_REFINE_PATH = ROOT / "prompts" / "idea_universe_refine.txt"

# 기본 컷오프
DEFAULT_TOP_N_COSINE = 300
DEFAULT_TOP_N_FINAL = 60
DEFAULT_MIN_MARKET_CAP_WON = 30_000_000_000  # 300억 (env로 조정 가능)
DEFAULT_REFINE_BATCH = 50  # kimi 컨텍스트 안전 단위
DEFAULT_REFINE_MODEL = os.getenv("OPENROUTER_MODEL") or "moonshotai/kimi-k2-thinking"


def _build_query_text(thesis_text: str, research: Optional[dict]) -> str:
    """thesis 본문 + research signals를 하나의 임베딩 쿼리로 합침.

    research가 있으면 logic_gradient_text, industries, mechanisms를 추가해
    매칭 시그널 강화. 종목명/티커는 의도적으로 제외 — universe 전체에 공평하게
    매칭되어야 하므로.
    """
    parts: list[str] = [thesis_text.strip()]
    if isinstance(research, dict):
        grad = research.get("logic_gradient_text")
        if isinstance(grad, str) and grad.strip():
            parts.append(f"\n핵심 논리: {grad.strip()}")
        # industries: 사용자 thesis가 hit하는 산업 키워드
        inds = research.get("industries") or []
        if isinstance(inds, list) and inds:
            names = [str(it.get("name", "")).strip() for it in inds if isinstance(it, dict)]
            names = [n for n in names if n]
            if names:
                parts.append("관련 산업: " + ", ".join(names))
        # mechanisms — 사용자 thesis가 EPS에 어떻게 작동하는지
        mechs = research.get("mechanisms") or []
        if isinstance(mechs, list) and mechs:
            parts.append("핵심 메커니즘: " + " · ".join(str(m).strip() for m in mechs if m))
    return "\n".join(parts)


def _row_to_candidate(row: dict, cosine_score: float) -> dict:
    """screener.db row → narrow 호환 candidate dict."""
    text = (row.get("business_text") or "").strip()
    return {
        "ticker6": row["ticker"],
        "ticker": row["ticker"],
        "name": row.get("name") or "",
        "industry": row.get("sector") or "",
        "sector": row.get("sector") or "",
        "market_cap": row.get("market_cap"),
        "mcap_estimate_krw_eok": (
            int(row["market_cap"]) // 100_000_000 if row.get("market_cap") else None
        ),
        "business_text_snippet": text[:400],
        "cosine_score": round(cosine_score, 4),
        "source": "universe_scan",
    }


def cosine_rank(
    query_emb: np.ndarray,
    min_market_cap_won: Optional[int] = None,
    top_n: int = DEFAULT_TOP_N_COSINE,
) -> list[dict]:
    """universe에서 cosine 상위 N — embedding 보유 종목만 대상.

    Returns: [_row_to_candidate(row, score), ...] 내림차순.
    """
    rows = screener_db.get_universe_for_scan(
        require_embedding=True,
        min_market_cap_won=min_market_cap_won,
    )
    if not rows:
        log.warning(
            "[thesis_scorer] universe에 embedding 종목 0건 — "
            "bootstrap_universe_embeddings.py 먼저 실행 필요"
        )
        return []
    candidate_embs: dict[str, np.ndarray] = {}
    row_by_ticker: dict[str, dict] = {}
    for r in rows:
        v = emb_mod.bytes_to_vec(r.get("embedding"))
        if v is None:
            continue
        candidate_embs[r["ticker"]] = v
        row_by_ticker[r["ticker"]] = r
    if not candidate_embs:
        log.warning("[thesis_scorer] embedding 디코딩 가능 종목 0건")
        return []
    log.info("[thesis_scorer] cosine rank — %d 종목 대상, top %d 추출",
             len(candidate_embs), top_n)
    ranked = emb_mod.cosine_top_n(query_emb, candidate_embs, top_n)
    out = [_row_to_candidate(row_by_ticker[t], s) for t, s in ranked]
    return out


def _refine_prompt() -> str:
    if PROMPT_REFINE_PATH.exists():
        return PROMPT_REFINE_PATH.read_text(encoding="utf-8")
    # 폴백: inline prompt
    return (
        "당신은 한국 종목 thesis 매칭 전문가. 사용자 thesis와 종목의 사업 description을 "
        "비교해 각 종목에 객관 매칭 점수를 부여합니다.\n\n"
        "각 종목당 JSON 한 줄:\n"
        '  {"ticker6": "...", "match_score": 1-10, "eps_lever_pre": 1-10, '
        '"specialty_purity": 1-10, "skip_reason": "..."}\n\n'
        "match_score: 사업 description이 thesis 직격이면 9-10, 일부 노출 4-6, 무관 1-2.\n"
        "eps_lever_pre: thesis 1pp 강화 시 EPS magnitude 약식 추정 (1-10).\n"
        "specialty_purity: description에 핵심 사업부 비중 명시되어 있으면 그 비중 기반 1-10. "
        "다각화·컨글로머릿은 3 이하.\n"
        "skip_reason: 무관·SPAC·청산예정 등 (빈 문자열이면 정상).\n"
        "출력은 종목 개수만큼 JSON 라인, 한 줄 한 객체. 자연어 서론·결론·코드 펜스 금지."
    )


def _parse_refine_jsonl(content: str) -> list[dict]:
    """LLM 응답에서 JSON object 라인들 추출. 코드 펜스·서론 모두 허용."""
    if not content:
        return []
    # 코드 펜스 제거
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
    if fence:
        content = fence.group(1)
    out: list[dict] = []
    for line in content.splitlines():
        line = line.strip().rstrip(",")
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    # JSONL 실패 시 array 시도
    if not out:
        m = re.search(r"\[\s*\{.*\}\s*\]", content, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    out = [x for x in arr if isinstance(x, dict)]
            except Exception:
                pass
    return out


def llm_refine_batch(
    thesis_text: str,
    candidates: list[dict],
    model: str = DEFAULT_REFINE_MODEL,
    batch_size: int = DEFAULT_REFINE_BATCH,
) -> list[dict]:
    """top N(보통 300) 종목에 LLM 약식 매칭 점수 부여 → match_score 필드 추가.

    실패 시 graceful — candidates 그대로 반환 (cosine_score만 정렬 기준).
    """
    if not candidates:
        return candidates
    try:
        from src import summarizer
        client = summarizer.get_client()
    except Exception:
        log.exception("[thesis_scorer] LLM refine — summarizer client 실패, cosine만 사용")
        return candidates

    sys_prompt = _refine_prompt()
    refined_by_t: dict[str, dict] = {}

    for batch_start in range(0, len(candidates), batch_size):
        batch = candidates[batch_start: batch_start + batch_size]
        # 입력 압축: 각 종목 {ticker, name, sector, snippet}
        items_str = "\n".join(
            json.dumps(
                {
                    "ticker6": c["ticker"],
                    "name": c.get("name"),
                    "sector": c.get("sector") or "",
                    "snippet": (c.get("business_text_snippet") or "")[:400],
                },
                ensure_ascii=False,
            )
            for c in batch
        )
        user_msg = (
            f"# 사용자 thesis\n{thesis_text.strip()}\n\n"
            f"# 후보 {len(batch)}종목 (JSON 라인)\n{items_str}\n\n"
            f"각 종목에 대해 JSON 한 줄씩 출력."
        )
        try:
            content = summarizer.chat_with_retry(
                client,
                model=model,
                max_tokens=4000,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                context=f"universe refine batch {batch_start // batch_size + 1}",
            )
        except Exception:
            log.exception(
                "[thesis_scorer] LLM refine batch %d 실패 — graceful skip",
                batch_start // batch_size + 1,
            )
            continue
        parsed = _parse_refine_jsonl(content or "")
        for obj in parsed:
            t = str(obj.get("ticker6") or "").strip()
            if t:
                refined_by_t[t] = obj

    log.info(
        "[thesis_scorer] LLM refine — %d/%d 종목 점수 추출",
        len(refined_by_t), len(candidates),
    )

    # 결과 merge: candidates에 match_score · eps_lever_pre · specialty_purity 추가
    for c in candidates:
        t = c["ticker"]
        r = refined_by_t.get(t) or {}
        c["match_score"] = int(r.get("match_score") or 5)
        c["eps_lever_pre"] = int(r.get("eps_lever_pre") or 5)
        c["specialty_purity"] = int(r.get("specialty_purity") or 5)
        skip_reason = (r.get("skip_reason") or "").strip()
        c["refine_skip_reason"] = skip_reason
        # universe_match = match × specialty / 10 (0-10)
        c["universe_match"] = round(c["match_score"] * c["specialty_purity"] / 10.0, 2)
    return candidates


def score_universe(
    thesis_text: str,
    research: Optional[dict] = None,
    top_n_cosine: int = DEFAULT_TOP_N_COSINE,
    top_n_final: int = DEFAULT_TOP_N_FINAL,
    min_market_cap_won: Optional[int] = None,
    use_llm_refine: bool = True,
) -> list[dict]:
    """공개 진입점 — thesis → universe-wide deterministic top-N candidates.

    Returns: top_n_final개 candidates (LLM refine이 활성이면 universe_match로
    재정렬, 아니면 cosine_score 그대로).
    """
    if not thesis_text or not thesis_text.strip():
        log.warning("[thesis_scorer] thesis_text 비어있음")
        return []
    if min_market_cap_won is None:
        env_val = os.getenv("UNIVERSE_SCAN_MIN_CAP_WON")
        if env_val:
            try:
                min_market_cap_won = int(env_val)
            except ValueError:
                min_market_cap_won = DEFAULT_MIN_MARKET_CAP_WON
        else:
            min_market_cap_won = DEFAULT_MIN_MARKET_CAP_WON

    query_text = _build_query_text(thesis_text, research)
    try:
        query_emb = emb_mod.embed_one(query_text)
    except emb_mod.EmbeddingError:
        log.exception("[thesis_scorer] thesis 임베딩 실패 — universe_scan 폴백 0건")
        return []

    cosine_top = cosine_rank(query_emb, min_market_cap_won=min_market_cap_won, top_n=top_n_cosine)
    if not cosine_top:
        return []
    if use_llm_refine:
        cosine_top = llm_refine_batch(thesis_text, cosine_top)
        # universe_match 기준 정렬, tie-break: cosine desc → ticker asc
        cosine_top.sort(
            key=lambda c: (
                -(c.get("universe_match") or 0),
                -(c.get("cosine_score") or 0),
                c["ticker"],
            )
        )
    return cosine_top[:top_n_final]


def is_universe_scan_ready() -> bool:
    """Universe scan 사용 가능 여부 — embedding 보유 종목 ≥ 50 필요."""
    try:
        stats = screener_db.universe_scan_stats()
        return stats.get("with_embedding", 0) >= 50
    except Exception:
        return False
