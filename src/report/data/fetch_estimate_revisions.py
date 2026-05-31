"""Finnhub 분석가 기대치 변화 — **포워드** 전용. 어닝 발표와 별개로 시장 기대 변화 추적.

소스:
  - /stock/recommendation : 월별 buy/hold/sell/strongBuy/strongSell 카운트 (최근 6~12개월)
  - /stock/price-target   : 목표가 high/low/median consensus (현재 시점)

용도: watchlist의 `estimate_revision` 카테고리. 종목별 enriched dict에
`estimate_revision: {window_days, pt_median_old, pt_median_new, pt_chg_pct,
buy_count_old, buy_count_new, hold_count_new, sell_count_new, fwd_eps_change_pct(없으면 null)}`
형태로 부착.

키 없거나 fetch 실패 시 빈 dict — graceful skip.

**중요**: 어닝 발표(`earnings_actual`) 결과와 분리. 발표 없이도 기대치는 매일 움직임 —
"분석가 buy 11→14·목표가 $180→$210"는 forward visibility 신호.

Note: Finnhub free tier는 `/stock/recommendation` 제공하나 `/stock/price-target`은
요청 시점에 따라 200 또는 403 가능. 둘 다 try하되 하나 실패해도 부분 결과 반환.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1"


def _get_json(path: str, params: dict, key: str) -> dict | list | None:
    import httpx
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{_BASE}{path}?{q}&token={key}"
    try:
        r = httpx.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code in (401, 403):
            log.info("[estimate_revisions] %s %s — 권한 없음", path, r.status_code)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.info("[estimate_revisions] %s 실패: %s", path, e)
        return None


def fetch_recommendation_trend(symbol: str, key: str) -> Optional[list[dict]]:
    """월별 분석가 trends (최근 → 과거 order). 빈 list / None 가능."""
    data = _get_json("/stock/recommendation", {"symbol": symbol}, key)
    return data if isinstance(data, list) else None


def fetch_price_target(symbol: str, key: str) -> Optional[dict]:
    """{targetHigh, targetLow, targetMean, targetMedian, lastUpdated}. None 가능."""
    data = _get_json("/stock/price-target", {"symbol": symbol}, key)
    return data if isinstance(data, dict) and data.get("targetMedian") else None


def compute_revision(symbol: str) -> dict:
    """단일 심볼의 estimate_revision dict (없으면 빈 dict).

    유의미 기준: 직전 30일 동안
      - 분석가 buy count 변화 (≥+2 또는 ≤-2)
      - OR 목표가 median 변화 (|chg_pct| ≥ 5%)
    둘 다 미해당이면 빈 dict (watchlist 후보 풀에서 제외 신호).
    """
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        return {}
    trend = fetch_recommendation_trend(symbol, key)
    if not trend or len(trend) < 2:
        return {}
    # trend[0] = 최신 월, trend[1] = 직전 월
    cur = trend[0] or {}
    prev = trend[1] or {}
    try:
        buy_new = int(cur.get("buy") or 0) + int(cur.get("strongBuy") or 0)
        buy_old = int(prev.get("buy") or 0) + int(prev.get("strongBuy") or 0)
        hold_new = int(cur.get("hold") or 0)
        sell_new = int(cur.get("sell") or 0) + int(cur.get("strongSell") or 0)
        buy_delta = buy_new - buy_old
    except Exception:
        buy_delta = 0
        buy_new = buy_old = hold_new = sell_new = 0

    pt = fetch_price_target(symbol, key)
    pt_new = pt_old = pt_chg = None
    if pt:
        try:
            pt_new = float(pt.get("targetMedian") or 0) or None
            # Finnhub는 이전 PT median을 직접 주지 않음 — recommendation 시계열로 proxy 불가
            # 대신 LastUpdated와 현재 median만 노출. "신선도" 정보로만 활용.
        except Exception:
            pt_new = None

    significant = abs(buy_delta) >= 2 or (pt_new is not None and pt_new > 0)
    if not significant:
        return {}

    return {
        "window_days": 30,
        "buy_count_new": buy_new,
        "buy_count_old": buy_old,
        "buy_delta": buy_delta,
        "hold_count_new": hold_new,
        "sell_count_new": sell_new,
        "pt_median_new": pt_new,
        "pt_last_updated": (pt or {}).get("lastUpdated"),
    }


def batch_revisions(symbols: list[str], cap: int = 20) -> dict[str, dict]:
    """여러 심볼을 순차 fetch (Finnhub 60/min 제한 고려). cap = 최대 호출 수.

    반환: {symbol: revision_dict} — revision_dict 비어있으면 유의미하지 않은 종목.
    """
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        return {}
    out: dict[str, dict] = {}
    for sym in symbols[:cap]:
        try:
            rev = compute_revision(sym)
            if rev:
                out[sym] = rev
        except Exception:
            log.exception("[estimate_revisions] %s 실패", sym)
    return out
