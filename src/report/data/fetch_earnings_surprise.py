"""Finnhub 어닝 surprise — **백워드(실제 결과)** 전용. EPS+revenue surprise·beat 판정.

FMP earnings-calendar(`fetch_earnings.py`)는 EPS surprise만 제공. 본 모듈은 같은 윈도의
`/calendar/earnings`를 Finnhub로 fetch해 revenue actual/estimate까지 가져오고, FMP 결과와
cross-validation해 EPS 충돌 시 `eps_disputed=True` flag.

용도: watchlist의 `earnings_actual` 카테고리 후보. 종목별 enriched dict에
`earnings_actual: {asof, eps_actual, eps_est, eps_surprise_pct, rev_actual, rev_est,
rev_surprise_pct, source_consensus}` 형태로 부착.

키 없거나 fetch 실패 시 빈 list — watchlist에 영향 없이 graceful degrade.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1"


def _calendar_call(start_iso: str, end_iso: str, key: str) -> list[dict]:
    import httpx
    url = f"{_BASE}/calendar/earnings?from={start_iso}&to={end_iso}&token={key}"
    try:
        r = httpx.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json() or {}
        items = data.get("earningsCalendar")
        return items if isinstance(items, list) else []
    except Exception as e:
        log.info("[earnings_surprise] Finnhub calendar 실패 %s~%s: %s", start_iso, end_iso, e)
        return []


def fetch_finnhub_calendar(days_back: int = 14, days_fwd: int = 0) -> list[dict]:
    """Finnhub `/calendar/earnings` 결과 정규화 list. 키 없으면 빈 list.

    각 entry:
      {symbol, date, hour, year, quarter,
       eps_actual, eps_est, eps_surprise_pct,
       rev_actual, rev_est, rev_surprise_pct}
    """
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        log.info("[earnings_surprise] FINNHUB_API_KEY 없음 — Finnhub 스킵")
        return []
    today = date.today()
    start = (today - timedelta(days=days_back)).isoformat()
    end = (today + timedelta(days=days_fwd)).isoformat()
    raw = _calendar_call(start, end, key)
    out: list[dict] = []
    for it in raw:
        try:
            sym = (it.get("symbol") or "").strip().upper()
            if not sym:
                continue
            eps_a = it.get("epsActual")
            eps_e = it.get("epsEstimate")
            rev_a = it.get("revenueActual")
            rev_e = it.get("revenueEstimate")
            eps_surp = None
            if eps_a is not None and eps_e and eps_e != 0:
                eps_surp = float((eps_a - eps_e) / abs(eps_e) * 100)
            rev_surp = None
            if rev_a is not None and rev_e and rev_e != 0:
                rev_surp = float((rev_a - rev_e) / abs(rev_e) * 100)
            out.append({
                "symbol": sym,
                "date": it.get("date"),
                "hour": it.get("hour"),
                "year": it.get("year"),
                "quarter": it.get("quarter"),
                "eps_actual": eps_a,
                "eps_est": eps_e,
                "eps_surprise_pct": eps_surp,
                "rev_actual": rev_a,
                "rev_est": rev_e,
                "rev_surprise_pct": rev_surp,
            })
        except Exception:
            continue
    return out


def cross_validate_eps(symbol: str, finnhub_entries: list[dict],
                       fmp_entries: list[dict] | None,
                       tol_pct: float = 5.0) -> dict:
    """심볼의 가장 최근 EPS를 Finnhub vs FMP 대조.

    반환: {source_consensus: "FMP+Finnhub agree" | "출처 충돌, 보수 채택" | "Finnhub only" | "FMP only" | "none",
           eps_disputed: bool}
    """
    fh = [e for e in finnhub_entries if e.get("symbol") == symbol and e.get("eps_actual") is not None]
    fm = []
    if fmp_entries:
        # FMP earnings entries from fetch_earnings have keys: symbol, epsActual, ...
        fm = [
            e for e in fmp_entries
            if (e.get("symbol") or "").upper() == symbol and e.get("epsActual") is not None
        ]
    if not fh and not fm:
        return {"source_consensus": "none", "eps_disputed": False}
    if fh and not fm:
        return {"source_consensus": "Finnhub only", "eps_disputed": False}
    if fm and not fh:
        return {"source_consensus": "FMP only", "eps_disputed": False}
    fh_eps = fh[0].get("eps_actual")
    fm_eps = fm[0].get("epsActual")
    try:
        if fh_eps and fm_eps and abs(fh_eps) > 0:
            diff_pct = abs((fh_eps - fm_eps) / abs(fh_eps) * 100)
            if diff_pct > tol_pct:
                return {"source_consensus": "출처 충돌, 보수 채택", "eps_disputed": True}
        return {"source_consensus": "FMP+Finnhub agree", "eps_disputed": False}
    except Exception:
        return {"source_consensus": "none", "eps_disputed": False}


def select_top_surprises(entries: list[dict], top_beats: int = 8, top_misses: int = 5,
                         min_abs_surprise_pct: float = 5.0) -> dict:
    """후보 풀용 — eps_surprise_pct 기준 top beats + misses 선별.

    반환: {"beats": [...], "misses": [...]}
    """
    valid = [e for e in entries if e.get("eps_surprise_pct") is not None]
    beats = sorted(
        [e for e in valid if e["eps_surprise_pct"] >= min_abs_surprise_pct],
        key=lambda e: -float(e["eps_surprise_pct"]),
    )[:top_beats]
    misses = sorted(
        [e for e in valid if e["eps_surprise_pct"] <= -min_abs_surprise_pct],
        key=lambda e: float(e["eps_surprise_pct"]),
    )[:top_misses]
    return {"beats": beats, "misses": misses}
