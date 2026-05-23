"""미국 종목 지표 — EPS YoY (SEC EDGAR diluted EPS), YTD, 거래대금.

EPS는 companyfacts의 us-gaap:EarningsPerShareDiluted(units "USD/shares")에서 단일분기
값을 뽑아 최근분기 vs 전년동분기 YoY%를 계산. SEC는 키 불필요·rate limit만 주의.
DB(fundamentals 테이블)에 7일 캐시해 매일 재호출 부하 최소화.
"""
from __future__ import annotations

import logging

from src.us_screener import db

log = logging.getLogger(__name__)

EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")


def _fetch_facts(cik: str):
    import time
    import httpx
    from src.earnings import sec_edgar
    url = f"{sec_edgar.EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    for attempt in range(3):
        sec_edgar._throttle()
        try:
            r = httpx.get(url, timeout=sec_edgar.DEFAULT_TIMEOUT,
                          headers={"User-Agent": sec_edgar.USER_AGENT, "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None  # 해당 종목 facts 없음 — 재시도 무의미
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def _quarterly_eps(facts: dict) -> list[dict]:
    """단일분기 EPS 포인트 [{fy,fp,end,val}] (오름차순). diluted 우선, 없으면 basic."""
    from src.earnings.sec_edgar import _days_between
    root = facts.get("facts", facts)  # companyfacts는 {cik, entityName, facts:{us-gaap:..}}
    usgaap = root.get("us-gaap") or {}
    for tag in EPS_TAGS:
        node = usgaap.get(tag)
        if not node:
            continue
        arr = (node.get("units") or {}).get("USD/shares") or []
        picked: dict[tuple[int, str], dict] = {}
        for e in arr:
            try:
                fp = e.get("fp") or ""
                fy = int(e.get("fy") or 0)
                start, end = str(e.get("start") or ""), str(e.get("end") or "")
                if fy <= 0 or not fp.startswith("Q") or not start or not end:
                    continue
                if not (80 <= _days_between(start, end) <= 100):  # 단일분기만
                    continue
                key = (fy, fp)
                cur = picked.get(key)
                if cur is None or end > str(cur.get("end") or "") or (
                        end == str(cur.get("end") or "")
                        and (e.get("filed") or "") > (cur.get("filed") or "")):
                    picked[key] = e
            except Exception:
                continue
        if picked:
            pts = [{"fy": fy, "fp": fp, "end": str(e.get("end") or ""),
                    "val": float(e.get("val") or 0)} for (fy, fp), e in picked.items()]
            pts.sort(key=lambda p: (p["fy"], p["fp"]))
            return pts
    return []


def eps_yoy(ticker: str, use_cache: bool = True) -> float | None:
    """최근분기 diluted EPS YoY %. 데이터 부족/전년 음수기준이면 None. 결과는 캐시."""
    if use_cache:
        c = db.fundamentals_get(ticker)
        if c is not None:
            return c.get("eps_yoy")
    val: float | None = None
    asof: str | None = None
    try:
        from src.earnings import sec_edgar
        cik = sec_edgar.ticker_to_cik(ticker)
        if cik:
            facts = _fetch_facts(cik)
            if facts:
                pts = _quarterly_eps(facts)
                if len(pts) >= 2:
                    latest = pts[-1]
                    prior = next((p for p in reversed(pts)
                                  if p["fp"] == latest["fp"] and p["fy"] == latest["fy"] - 1), None)
                    if prior and prior["val"] > 1e-6:  # 전년 양수기준만 (YoY 의미 보존)
                        val = round((latest["val"] - prior["val"]) / abs(prior["val"]) * 100, 1)
                        asof = latest["end"]
    except Exception:
        log.warning("[fundamentals] %s EPS YoY 실패", ticker)
    db.fundamentals_put(ticker, val, asof)  # None도 캐시 (재호출 방지)
    return val


def ytd_pct(rows: list[dict]) -> float | None:
    """연초대비 % (rows: load_ohlcv, close=cents 정수, asc)."""
    if not rows:
        return None
    latest = rows[-1]
    year = latest["date"][:4]
    base = next((r for r in rows if r["date"][:4] == year and r.get("close")), None)
    if not base or not base.get("close"):
        return None
    return round((latest["close"] - base["close"]) / base["close"] * 100, 1)


def turnover_usd(row: dict) -> float:
    """거래대금(USD) = close($) × volume. close는 cents."""
    return (row.get("close", 0) / 100.0) * row.get("volume", 0)
