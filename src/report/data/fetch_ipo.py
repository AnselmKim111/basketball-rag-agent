"""IPO 캘린더 — Finnhub `/calendar/ipo` 기반 (US만, 무료).

직전 14일 priced + 향후 14일 expected. 자금 흐름 신호로서 watchlist의 `ipo` 카테고리.

키 없으면 빈 list — graceful skip.

각 entry: {symbol, name, date, exchange, price (range str | None), shares (int|None),
total_value (float|None — shares × midpoint price), status (priced/expected/withdrawn)}
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

log = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1"


def _parse_price_midpoint(price_str: str | None) -> float | None:
    """Finnhub `price` 필드는 "12.00-14.00" 같은 range str. midpoint float 반환."""
    if not price_str:
        return None
    try:
        s = price_str.strip()
        if "-" in s:
            lo, hi = s.split("-", 1)
            return (float(lo) + float(hi)) / 2
        return float(s)
    except Exception:
        return None


def fetch_ipo_calendar(days_back: int = 14, days_fwd: int = 14) -> list[dict]:
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        log.info("[ipo] FINNHUB_API_KEY 없음 — 스킵")
        return []
    import httpx
    today = date.today()
    start = (today - timedelta(days=days_back)).isoformat()
    end = (today + timedelta(days=days_fwd)).isoformat()
    url = f"{_BASE}/calendar/ipo?from={start}&to={end}&token={key}"
    try:
        r = httpx.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code in (401, 403):
            log.info("[ipo] %s — 권한 없음 (무료 티어 제외 가능)", r.status_code)
            return []
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        log.info("[ipo] fetch 실패: %s", e)
        return []
    raw = data.get("ipoCalendar") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for it in raw:
        try:
            sym = (it.get("symbol") or "").strip().upper()
            name = (it.get("name") or "").strip()
            d = it.get("date")
            exch = (it.get("exchange") or "").strip()
            # NYSE / NASDAQ만 (OTC·소형 시장 제외)
            if exch and not any(k in exch.upper() for k in ("NYSE", "NASDAQ", "GLOBAL")):
                continue
            price_str = it.get("price")
            shares = it.get("numberOfShares")
            total_str = it.get("totalSharesValue")
            try:
                total_val = float(total_str) if total_str else None
            except Exception:
                total_val = None
            mid = _parse_price_midpoint(price_str)
            status = (it.get("status") or "expected").strip().lower()
            est_total = total_val
            if est_total is None and shares and mid:
                try:
                    est_total = float(shares) * mid
                except Exception:
                    est_total = None
            out.append({
                "symbol": sym or name[:10],
                "name": name or sym,
                "date": d,
                "exchange": exch,
                "price": price_str,
                "price_midpoint": mid,
                "shares": int(shares) if shares else None,
                "total_value": est_total,
                "status": status,
            })
        except Exception:
            continue
    out.sort(key=lambda x: (x.get("date") or ""), reverse=True)
    return out


def select_top_ipos(entries: list[dict], top_priced: int = 6, top_expected: int = 4,
                    min_value: float = 5e8) -> dict:
    """후보 풀용 — priced + expected 분리. min_value 이하 소형 IPO 제외.

    반환: {"priced": [...], "expected": [...]}
    """
    priced = [
        e for e in entries
        if (e.get("status") in ("priced", "withdrawn") and (e.get("total_value") or 0) >= min_value)
    ]
    expected = [
        e for e in entries
        if (e.get("status") not in ("priced", "withdrawn") and (e.get("total_value") or 0) >= min_value)
    ]
    return {
        "priced": priced[:top_priced],
        "expected": expected[:top_expected],
    }
