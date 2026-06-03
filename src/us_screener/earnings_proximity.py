"""US 스크리너 신호 종목의 7일 내 어닝 임박 확인 — ⚡ 마커용.

FMP earnings-calendar 윈도 fetch 로직은 src/report/data/fetch_earnings.py에 이미 검증됨 →
그대로 재사용. 결과를 {ticker: "YYYY-MM-DD"} 매핑으로 평탄화하여 caller가 즉시 사용 가능.

매일 1회 호출(7일 윈도 = 1 fetch) — 캐싱 불필요.
FMP_API_KEY 없거나 fetch 실패 시 빈 dict 반환(graceful degrade).
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from src.report.data.fetch_earnings import _calendar

log = logging.getLogger(__name__)


def fetch_upcoming_earnings(tickers: set[str], days_fwd: int = 7) -> dict[str, str]:
    """티커 set 중 today ~ today+days_fwd 어닝 예정자만 {ticker: "YYYY-MM-DD"} 반환.

    동일 ticker가 윈도 내 여러 번 등장하면 가장 빠른 date 선택.
    FMP_API_KEY 없거나 fetch 실패 시 빈 dict (graceful degrade).
    """
    if not tickers:
        return {}
    key = os.getenv("FMP_API_KEY")
    if not key:
        log.info("[earnings_proximity] FMP_API_KEY 없음 — ⚡ 마커 생략")
        return {}

    today = date.today()
    try:
        rows = _calendar(today, today + timedelta(days=days_fwd), key)
    except Exception:
        log.exception("[earnings_proximity] _calendar 호출 실패")
        return {}

    out: dict[str, str] = {}
    tickers_upper = {t.upper() for t in tickers}
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        d = r.get("date")
        if not sym or not d or sym not in tickers_upper:
            continue
        # 동일 ticker 여러 entry: 가장 빠른 date 선택
        if sym not in out or d < out[sym]:
            out[sym] = d
    log.info("[earnings_proximity] %d/%d 티커 7일 내 어닝 발견", len(out), len(tickers))
    return out
