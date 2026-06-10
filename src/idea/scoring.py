"""IdeaBot 손익비 점수 — narrow 결과에 정량 신호 합산.

narrow LLM이 준 operating leverage 5축 점수(fixed_cost / capacity / growth /
margin / purity)에 다음 정량 신호를 합산해 top5 재선정:

  - momentum_confirmation: ScreenerBot signal hit (52w 신고가/VCP/거래량 돌파).
    14일 내 hit 수·종류로 0-10 점수.
  - (TODO) valuation_gap: price_fetcher YTD + forward consensus
  - (TODO) timing_gradient: DART 잠정실적 매출 yoy 가속

손익비 score = operating_leverage_score × (purity / 10) + momentum × 0.3

screener.db 미가용이면 momentum=0 — narrow 점수만으로 정렬.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.cross_bot import screener_query

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# momentum 점수 (ScreenerBot signal hit 기반)
# ------------------------------------------------------------------
# signal 종류별 가중치 — 본격적 돌파 > 임박 > 보조
_SIGNAL_WEIGHT: dict[str, float] = {
    "high_all": 3.0,           # 보유 데이터 역사적 신고가
    "high_52w": 3.0,           # 52주 신고가
    "vcp_breakout": 4.0,       # VCP 돌파 (가장 강한 신호)
    "volume_breakout": 2.0,    # 거래량 돌파
    "near_breakout_52w": 1.5,  # 임박
}


def _momentum_score(ticker: str, days_back: int = 14) -> tuple[int, dict[str, Any]]:
    """ticker의 ScreenerBot signal hit → 0-10 점수 + 상세 메타.

    가중치 합산 후 cap(10). 신호 없으면 0.
    """
    if not re.match(r"^\d{6}$", (ticker or "")):
        return 0, {}
    summary = screener_query.get_signal_summary(ticker, days_back=days_back)
    hits = summary.get("hit_count") or 0
    if hits == 0:
        return 0, {"hit_count": 0, "signals": [], "latest_date": None}
    sigs = summary.get("signals") or []
    raw = sum(_SIGNAL_WEIGHT.get(s, 0.5) for s in sigs)
    # hit_count도 반영 — 같은 신호 여러 번 = 강한 신호
    raw += min(hits, 5) * 0.5
    score = min(int(round(raw)), 10)
    return score, {
        "hit_count": hits,
        "signals": sigs,
        "latest_date": summary.get("latest_date"),
        "latest_signal": summary.get("latest_signal"),
    }


# ------------------------------------------------------------------
# 손익비 합산
# ------------------------------------------------------------------
def _extract_score(value: Any) -> int:
    """LLM이 '8 — 사유'처럼 답하기도 함. 첫 정수만 추출. 없으면 5(중간)."""
    if isinstance(value, (int, float)):
        return max(1, min(10, int(value)))
    if isinstance(value, str):
        m = re.search(r"\b([1-9]|10)\b", value)
        if m:
            return int(m.group(1))
    return 5


def _op_lev_score(c: dict) -> int:
    """narrow 결과의 operating_leverage_score를 정수화."""
    return _extract_score(c.get("operating_leverage_score"))


def _purity_from_breakdown(c: dict) -> int:
    """breakdown.idea_purity에서 점수 추출. 없으면 c.purity_score 폴백."""
    breakdown = c.get("breakdown") or {}
    pur = breakdown.get("idea_purity")
    if pur is not None:
        return _extract_score(pur)
    return _extract_score(c.get("purity_score", 7))


def score_top10(top10: list[dict], days_back: int = 14) -> list[dict]:
    """top10 각 종목에 momentum_score · risk_reward_score 추가.

    추가 필드:
      momentum_score: int (0-10)
      momentum_meta: {hit_count, signals, latest_date, latest_signal}
      risk_reward_score: float = op_lev × (purity/10) + momentum × 0.3

    원본 list 변경 + 반환 (in-place 호환).
    """
    if not top10:
        return top10
    available = screener_query.is_available()
    if not available:
        log.info("[scoring] screener.db 미가용 — momentum=0으로 계산")
    for c in top10:
        ticker = (c.get("ticker6") or "").strip()
        if available:
            mom, meta = _momentum_score(ticker, days_back=days_back)
        else:
            mom, meta = 0, {"hit_count": 0, "signals": [], "latest_date": None}
        op_lev = _op_lev_score(c)
        purity = _purity_from_breakdown(c)
        rr = op_lev * (purity / 10.0) + mom * 0.3
        c["momentum_score"] = mom
        c["momentum_meta"] = meta
        c["risk_reward_score"] = round(rr, 2)
    log.info(
        "[scoring] top10 손익비 산출 — momentum hit %d개 / %d",
        sum(1 for c in top10 if (c.get("momentum_score") or 0) > 0),
        len(top10),
    )
    return top10


def sort_by_risk_reward(top10: list[dict]) -> list[dict]:
    """risk_reward_score 내림차순 정렬. tie-breaker: purity desc → op_lev desc."""
    return sorted(
        top10,
        key=lambda c: (
            c.get("risk_reward_score", 0),
            _purity_from_breakdown(c),
            _op_lev_score(c),
        ),
        reverse=True,
    )


def select_top5(top10: list[dict], days_back: int = 14) -> list[dict]:
    """top10 → momentum·손익비 합산 후 상위 5개.

    원본 list 보존 (sorted_copy 반환).
    """
    scored = score_top10(top10, days_back=days_back)
    sorted_list = sort_by_risk_reward(scored)
    return sorted_list[:5]
