"""IdeaBot 후보 발굴 다중 소스 통합.

기존 perplexity research 결과(LLM 추측)를 다음 소스로 보강·교차검증:
  - 1b. ScreenerBot universe (screener.db tickers) — 시총·섹터 정확 (LLM 추측 폐기)
  - 1c. 산업 리포트에서 분석가 인용 종목 추출 (LLM 한 번 더)
  - 1d. DART 최근 30일 공시 + idea 키워드 cross-ref            ← Step 4

원칙:
  - 모든 외부 소스는 graceful (DB 없거나 외부 API 실패해도 빈 결과 + 로그)
  - dedup은 (name, ticker6) tuple 단위
  - market_cap은 모두 won 단위 int. screener.db 값이 perplexity 추측보다 정확.
  - LLM이 준 정보는 보조 — DB 정보가 1순위.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from src.cross_bot import screener_query
from src.idea.tickers import names_match, normalize_name
from src.llm_json import parse_json_object
from src.llm_models import narrow_model, summary_model

log = logging.getLogger(__name__)

# 산업리포트 텍스트는 길어서 한 번 호출당 cap
_MENTIONED_TEXT_CAP = 18000
_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "idea_extract_mentioned.txt"


# ------------------------------------------------------------------
# 정규화·dedup
# ------------------------------------------------------------------
def _key(candidate: dict) -> tuple[str, str]:
    """dedup 키 — (normalized_name, ticker6). ticker6 없으면 ('name', '')."""
    name = normalize_name(candidate.get("name", ""))
    ticker = (candidate.get("ticker6") or "").strip()
    return (name, ticker if re.match(r"^\d{6}$", ticker) else "")


def _dedup_candidates(items: list[dict]) -> list[dict]:
    """(name, ticker) 기준 중복 제거. 정보 풍부한 항목 우선."""
    seen: dict[tuple[str, str], dict] = {}
    for item in items:
        if not item:
            continue
        k = _key(item)
        if not k[0]:  # name 없음
            continue
        existing = seen.get(k)
        if existing is None:
            seen[k] = dict(item)
        else:
            # 후순위 항목으로 빈 필드 보완
            for field, value in item.items():
                if value and not existing.get(field):
                    existing[field] = value
    return list(seen.values())


# ------------------------------------------------------------------
# Source 1b. screener universe → 후보 풀
# ------------------------------------------------------------------
def from_screener_universe(
    constraints: dict,
    industries: list[dict],
    max_picks: int = 50,
) -> list[dict]:
    """ScreenerBot universe에서 constraints + industry 키워드 매칭 후보.

    인자:
      constraints: {market_cap_max_krw, market_cap_min_krw, exchange, industry_filter[]}
      industries: [{name, gics_hint, ...}] — research 1단계 산업 리스트
      max_picks: 상한 (시총 desc 순으로)

    반환: research candidate 포맷의 dict 리스트.
      [{name, ticker6, industry, mechanism, size_tier, mcap_estimate_krw_eok,
        purity_score(미정값 7), mechanism_link, source: 'screener'}]

    DB 미가용 → 빈 리스트.
    """
    if not screener_query.is_available():
        log.info("[sourcing] screener.db 미가용 — universe 후보 0개")
        return []

    # 산업 키워드 모음 (industry name + gics_hint + 사용자 industry_filter)
    sect_keywords: set[str] = set()
    for ind in industries or []:
        for k in ("name", "gics_hint"):
            v = (ind.get(k) or "").strip()
            if v:
                sect_keywords.add(v)
    for v in (constraints.get("industry_filter") or []):
        if v:
            sect_keywords.add(v)
    sect_list = list(sect_keywords)

    rows = screener_query.get_universe_filtered(
        market_cap_max_won=constraints.get("market_cap_max_krw"),
        market_cap_min_won=constraints.get("market_cap_min_krw"),
        sectors=sect_list if sect_list else None,
        markets=[constraints["exchange"]] if constraints.get("exchange") else None,
    )
    if not rows:
        log.info(
            "[sourcing] screener universe 필터 0건 (sectors=%s, cap_max=%s)",
            sect_list, constraints.get("market_cap_max_krw"),
        )
        return []

    # 시총 desc로 정렬 후 상한
    rows = sorted(rows, key=lambda r: r.get("market_cap") or 0, reverse=True)[:max_picks]

    out: list[dict] = []
    for r in rows:
        cap_won = r.get("market_cap") or 0
        cap_eok = cap_won // 100_000_000  # 억원
        # size_tier 자동 분류
        if cap_won >= 10_000_000_000_000:  # 10조+
            tier = "large"
        elif cap_won >= 1_000_000_000_000:  # 1-10조
            tier = "mid"
        else:
            tier = "small"
        out.append({
            "name": r.get("name", ""),
            "ticker6": r.get("ticker", ""),
            "industry": (industries[0]["name"] if industries else ""),
            "mechanism": "",  # narrow LLM에서 mechanism 매핑
            "size_tier": tier,
            "mcap_estimate_krw_eok": cap_eok,
            "purity_score": 6,  # screener universe 매칭은 보수적 — narrow LLM이 재평가
            "mechanism_link": f"sector={r.get('sector','?')} (screener universe 매칭)",
            "source": "screener",
            "screener_sector": r.get("sector", ""),
            "screener_market": r.get("market", ""),
        })
    log.info(
        "[sourcing] screener universe 후보 %d개 (cap_max=%s, sectors=%s)",
        len(out), constraints.get("market_cap_max_krw"), sect_list,
    )
    return out


# ------------------------------------------------------------------
# enrich — research candidates의 시총·섹터를 screener.db 정확 값으로 보강
# ------------------------------------------------------------------
def enrich_with_screener_data(candidates: list[dict]) -> list[dict]:
    """각 candidate의 ticker로 screener.db 시총·섹터 lookup해서 정확 값으로 교체.

    LLM이 추측한 mcap_estimate_krw_eok이 부정확한 경우 screener.db의 정확 값으로
    교체. 종목 size_tier도 정확 시총 기반으로 재분류.

    DB 미가용이면 그대로 반환.
    """
    if not candidates:
        return candidates
    if not screener_query.is_available():
        return candidates

    universe = screener_query.get_universe()
    by_ticker = {r["ticker"]: r for r in universe if r.get("ticker")}
    enriched_count = 0
    for c in candidates:
        ticker = (c.get("ticker6") or "").strip()
        if not re.match(r"^\d{6}$", ticker):
            continue
        row = by_ticker.get(ticker)
        if not row:
            continue
        cap_won = row.get("market_cap") or 0
        if cap_won > 0:
            c["mcap_estimate_krw_eok"] = cap_won // 100_000_000
            # size_tier 재분류
            if cap_won >= 10_000_000_000_000:
                c["size_tier"] = "large"
            elif cap_won >= 1_000_000_000_000:
                c["size_tier"] = "mid"
            else:
                c["size_tier"] = "small"
        if row.get("sector"):
            c["screener_sector"] = row["sector"]
        if row.get("market"):
            c["screener_market"] = row["market"]
        enriched_count += 1
    log.info("[sourcing] enrich: %d/%d candidates에 screener 시총·섹터 보강", enriched_count, len(candidates))
    return candidates


# ------------------------------------------------------------------
# Source 1c. 산업 리포트 텍스트 → 분석가 인용 종목 (LLM 한 번 더)
# ------------------------------------------------------------------
def _load_extract_prompt() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        log.exception("[sourcing] idea_extract_mentioned 프롬프트 로드 실패")
        return ""


def extract_mentioned_tickers(industry_texts: dict[str, str]) -> list[dict]:
    """산업 리포트 텍스트들에서 분석가가 명시 인용한 종목 추출.

    LLM(summary tier — kimi, narrow tier 폴백)이 리포트 1건씩 읽어 회사명·ticker6 추출.
    환각 방지: ticker6은 본문에 6자리로 명시된 경우만, 회사명은 본문 그대로.

    인자:
      industry_texts: {report_label: text}  ← _collect_industry_reports 산출과 동일 포맷.

    반환: [{name, ticker6, rationale, source: 'industry_report', industry: report_label}]
      ticker6은 본문 명시된 경우만. screener_query.get_ticker_name으로 사후 교정 시도.
      LLM/PDF 실패는 graceful — 빈 리스트.
    """
    if not industry_texts:
        return []

    prompt = _load_extract_prompt()
    if not prompt:
        return []

    try:
        from src import summarizer
        client = summarizer.get_client()
    except Exception:
        log.info("[sourcing] OpenRouter client 미가용 — 산업리포트 인용 추출 skip")
        return []

    out: list[dict] = []
    for label, text in industry_texts.items():
        if not text or len(text.strip()) < 200:
            continue
        snippet = text[:_MENTIONED_TEXT_CAP]
        user_msg = (
            f"리포트 제목/식별자: {label}\n\n"
            f"본문 (cap {_MENTIONED_TEXT_CAP}자):\n{snippet}\n\n"
            f"위 본문에서 분석가가 명시 인용한 종목을 시스템 프롬프트 형식대로 JSON 출력."
        )
        try:
            content = summarizer.chat_with_retry(
                client,
                model=summary_model(),
                fallback_model=narrow_model(),
                max_tokens=2500,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_msg},
                ],
                context=f"idea_extract_mentioned[{label[:30]}]",
            )
        except Exception:
            log.exception("[sourcing] 산업리포트 인용 추출 실패: %s", label)
            continue
        obj = parse_json_object(content) or {}
        mentioned = obj.get("mentioned") or []
        if not isinstance(mentioned, list):
            continue
        for m in mentioned:
            if not isinstance(m, dict):
                continue
            name = (m.get("name") or "").strip()
            if not name:
                continue
            ticker = (m.get("ticker6") or "").strip()
            if not re.match(r"^\d{6}$", ticker):
                ticker = ""
            rationale = (m.get("rationale") or "").strip()
            out.append({
                "name": name,
                "ticker6": ticker,
                "industry": label,
                "mechanism": rationale,
                "mechanism_link": f"산업리포트 '{label}' 인용",
                "size_tier": "",            # narrow LLM에서 평가
                "mcap_estimate_krw_eok": 0,  # enrich로 보강
                "purity_score": 7,           # 인용된 종목은 분석가 의견 = 시장 confirmation
                "source": "industry_report",
            })
    log.info("[sourcing] 산업리포트 인용 추출: %d개 (리포트 %d건)", len(out), len(industry_texts))
    return out


# ------------------------------------------------------------------
# 통합: candidate pool 빌드
# ------------------------------------------------------------------
def build_candidate_pool(
    research: dict,
    parsed: dict,
    industry_texts: dict[str, str] | None = None,
    target_size: int = 60,
) -> list[dict]:
    """research(perplexity) + screener universe + 산업리포트 인용 통합 dedup pool.

    추후 Step 4 (DART 공시) 가 추가될 예정 — 시그니처 안정 유지.

    인자:
      research: perplexity research 결과 (candidates·industries)
      parsed: idea parse 결과 (constraints)
      industry_texts: optional, _collect_industry_reports 산출. 있으면 1c 활성.
      target_size: 합쳐서 N개 cap (시총 desc 우선).
    """
    research_candidates = list(research.get("candidates") or [])
    industries = list(research.get("industries") or [])
    constraints = (parsed or {}).get("constraints") or {}

    # 1a) research candidates를 enrich (정확 시총·섹터)
    research_candidates = enrich_with_screener_data(research_candidates)
    for c in research_candidates:
        c.setdefault("source", "perplexity")

    # 1b) screener universe에서 추가 발굴
    screener_picks = from_screener_universe(constraints, industries, max_picks=max(target_size, 30))

    # 1c) 산업리포트 인용 종목 (있는 경우만)
    mentioned_picks: list[dict] = []
    if industry_texts:
        mentioned_picks = extract_mentioned_tickers(industry_texts)
        mentioned_picks = enrich_with_screener_data(mentioned_picks)

    # 합쳐서 dedup
    pool = _dedup_candidates(research_candidates + screener_picks + mentioned_picks)

    # 시총 desc로 정렬 (정확 시총 우선 — screener·perplexity·인용 다 사용)
    pool.sort(key=lambda c: int(c.get("mcap_estimate_krw_eok") or 0), reverse=True)

    # target_size 상한
    pool = pool[:target_size]
    log.info(
        "[sourcing] candidate pool: research %d + screener %d + mentioned %d → dedup %d → top %d",
        len(research_candidates), len(screener_picks), len(mentioned_picks),
        len(research_candidates) + len(screener_picks) + len(mentioned_picks),
        len(pool),
    )
    return pool
