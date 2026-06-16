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


def _latest_shares(facts: dict) -> float | None:
    """발행주식수 — dei:EntityCommonStockSharesOutstanding 최신값. 폴백 us-gaap 가중평균."""
    root = facts.get("facts", facts)
    for ns, tag in (("dei", "EntityCommonStockSharesOutstanding"),
                    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
                    ("us-gaap", "CommonStockSharesOutstanding")):
        node = (root.get(ns) or {}).get(tag)
        if not node:
            continue
        arr = (node.get("units") or {}).get("shares") or []
        vals = [e for e in arr if e.get("val") and e.get("end")]
        if vals:
            vals.sort(key=lambda e: str(e.get("end")))
            return float(vals[-1]["val"])
    return None


def ticker_fundamentals(ticker: str, use_cache: bool = True) -> dict:
    """{eps_yoy, shares, eps_asof}. companyfacts 1회 fetch로 EPS+발행주식수 동시 추출.

    캐시 규칙(시총 N/A 포이즈닝 방지): 회사는 항상 발행주식수가 존재하므로 shares=None은
    'fetch 실패'로 간주 → 캐시를 신뢰하지 않고 재시도. 성공(shares 확보) 시에만 캐시.
    """
    if use_cache:
        c = db.fundamentals_get(ticker)
        if c is not None and c.get("shares"):  # shares 있는 캐시만 신뢰 (None은 실패 → 재fetch)
            return c
    eps_val: float | None = None
    shares: float | None = None
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
                    if prior and prior["val"] > 1e-6:
                        eps_val = round((latest["val"] - prior["val"]) / abs(prior["val"]) * 100, 1)
                        asof = latest["end"]
                shares = _latest_shares(facts)
    except Exception:
        log.warning("[fundamentals] %s 펀더멘털 실패", ticker)
    if shares:  # 실데이터(발행주식수) 확보 시에만 캐시 — None 포이즈닝 방지
        db.fundamentals_put(ticker, eps_val, asof, shares)
    return {"eps_yoy": eps_val, "shares": shares, "eps_asof": asof}


def eps_yoy(ticker: str, use_cache: bool = True) -> float | None:
    return ticker_fundamentals(ticker, use_cache).get("eps_yoy")


def market_cap_usd(ticker: str, close_dollars: float, use_cache: bool = True) -> float | None:
    """시가총액(USD) = 발행주식수(SEC) × 종가($). 발행주식수 없으면 None."""
    sh = ticker_fundamentals(ticker, use_cache).get("shares")
    if sh and close_dollars:
        return sh * close_dollars
    return None


def ytd_pct(rows: list[dict]) -> float | None:
    """연초대비 % (rows: load_ohlcv, close=cents 정수, asc).

    미국은 일일 제한이 없어 +50%도 정상이므로, 분할급 스케일 브레이크(>5배/<1/5)만
    걸러 잘못된 값 노출을 막는다.
    """
    if not rows:
        return None
    latest = rows[-1]
    year = latest["date"][:4]
    base_idx = next((i for i, r in enumerate(rows)
                     if r["date"][:4] == year and r.get("close")), None)
    if base_idx is None:
        return None
    base = rows[base_idx]
    if not base.get("close"):
        return None
    from src.screener_common.sanity import US_DAILY_HI, US_DAILY_LO, series_is_continuous
    closes = [r["close"] for r in rows[base_idx:] if r.get("close")]
    if not series_is_continuous(closes, US_DAILY_LO, US_DAILY_HI):
        log.warning("[ytd_pct] %s 스케일 브레이크 — 분할/조정 불일치 의심, N/A 처리", latest["date"])
        return None
    return round((latest["close"] - base["close"]) / base["close"] * 100, 1)


def turnover_usd(row: dict) -> float:
    """거래대금(USD) = close($) × volume. close는 cents."""
    return (row.get("close", 0) / 100.0) * row.get("volume", 0)
