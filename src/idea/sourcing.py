"""IdeaBot 후보 발굴 다중 소스 통합.

기존 perplexity research 결과(LLM 추측)를 다음 소스로 보강·교차검증:
  - 1b. ScreenerBot universe (screener.db tickers) — 시총·섹터 정확 (LLM 추측 폐기)
  - 1c. 산업 리포트에서 분석가 인용 종목 추출 (LLM 한 번 더)  ← Step 3
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
from typing import Any

from src.cross_bot import screener_query
from src.idea.tickers import names_match, normalize_name

log = logging.getLogger(__name__)


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
# 통합: candidate pool 빌드
# ------------------------------------------------------------------
def build_candidate_pool(
    research: dict,
    parsed: dict,
    target_size: int = 60,
) -> list[dict]:
    """research(perplexity) + screener universe 통합 dedup pool.

    추후 Step 3 (산업리포트 인용) + Step 4 (DART 공시) 가 추가될 예정 — 시그니처
    안정 유지.

    target_size: 합쳐서 N개 cap (시총 desc 우선).
    """
    research_candidates = list(research.get("candidates") or [])
    industries = list(research.get("industries") or [])
    constraints = (parsed or {}).get("constraints") or {}

    # 1단계: research candidates를 enrich (정확 시총·섹터)
    research_candidates = enrich_with_screener_data(research_candidates)
    for c in research_candidates:
        c.setdefault("source", "perplexity")

    # 2단계: screener universe에서 추가 발굴
    screener_picks = from_screener_universe(constraints, industries, max_picks=max(target_size, 30))

    # 3단계: 합쳐서 dedup
    pool = _dedup_candidates(research_candidates + screener_picks)

    # 4단계: 시총 desc로 정렬 (정확 시총 우선 — screener·perplexity 둘 다 사용)
    pool.sort(key=lambda c: int(c.get("mcap_estimate_krw_eok") or 0), reverse=True)

    # 5단계: target_size 상한
    pool = pool[:target_size]
    log.info(
        "[sourcing] candidate pool: research %d + screener %d → dedup %d → top %d",
        len(research_candidates), len(screener_picks),
        len(research_candidates) + len(screener_picks),
        len(pool),
    )
    return pool
