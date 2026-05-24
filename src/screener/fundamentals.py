"""한국 종목 지표 — EPS YoY (pykrx TTM EPS), YTD, 거래대금.

EPS YoY: pykrx get_market_fundamental_by_ticker(date)로 base_date와 ~1년 전 영업일의
전종목 TTM EPS를 일괄 fetch → 종목별 YoY%. pykrx 실패/데이터 부족이면 None(N/A).
DB(fundamentals 테이블) 7일 캐시로 호출 부하 최소화.

가격 단위: 한국 OHLCV는 원 단위 정수(미장의 cent×100과 다름) → /100 안 함.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.screener import db

log = logging.getLogger(__name__)


def _norm_yyyymmdd(d: str) -> str:
    return d.replace("-", "")[:8]


def _bulk_eps(date_yyyymmdd: str) -> dict[str, float]:
    """해당 날짜 전종목 TTM EPS {ticker: eps}. 실패/빈 결과면 {}."""
    try:
        from pykrx import stock
        df = stock.get_market_fundamental_by_ticker(date_yyyymmdd, market="ALL")
        if df is None or len(df) == 0 or "EPS" not in df.columns:
            return {}
        return {str(idx): float(v) for idx, v in df["EPS"].items() if v}
    except Exception as e:
        log.info("[kr.fundamentals] bulk EPS %s 실패: %s", date_yyyymmdd, e)
        return {}


def eps_yoy_map(tickers: list[str], base_date: str) -> dict[str, float | None]:
    """signal 티커들의 EPS YoY%. 캐시 우선, 미스만 pykrx 일괄(2회) 호출.

    base_date: 'YYYY-MM-DD'. 반환 {ticker: yoy|None}.
    """
    out: dict[str, float | None] = {}
    misses: list[str] = []
    for t in tickers:
        c = db.fundamentals_get(t)
        if c is not None:
            out[t] = c.get("eps_yoy")
        else:
            misses.append(t)
    if not misses:
        return out

    base = _norm_yyyymmdd(base_date)
    cur_eps = _bulk_eps(base)
    prior_eps: dict[str, float] = {}
    if cur_eps:
        # ~1년 전 영업일 (데이터 없으면 며칠 뒤로 물러나며 재시도)
        try:
            base_dt = datetime.strptime(base, "%Y%m%d")
        except Exception:
            base_dt = datetime.now()
        for back in (365, 366, 367, 368, 371, 374):
            cand = (base_dt - timedelta(days=back)).strftime("%Y%m%d")
            prior_eps = _bulk_eps(cand)
            if prior_eps:
                break

    for t in misses:
        val = None
        asof = None
        a = cur_eps.get(t)
        b = prior_eps.get(t)
        if a and b and a > 0 and b > 0:
            val = round((a - b) / b * 100, 1)
            asof = base_date
        out[t] = val
        db.fundamentals_put(t, val, asof)  # None도 캐시(재호출 방지)
    return out


def ytd_pct(rows: list[dict]) -> float | None:
    """연초대비 % (rows: load_ohlcv, asc). 스케일 무관(비율)."""
    if not rows:
        return None
    latest = rows[-1]
    year = latest["date"][:4]
    base = next((r for r in rows if r["date"][:4] == year and r.get("close")), None)
    if not base or not base.get("close"):
        return None
    return round((latest["close"] - base["close"]) / base["close"] * 100, 1)


def turnover_won(row: dict) -> float:
    """거래대금(원) = close(원) × volume. 한국 close는 원 단위 정수."""
    return row.get("close", 0) * row.get("volume", 0)
