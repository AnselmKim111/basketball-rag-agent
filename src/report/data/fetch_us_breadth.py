"""S&P500 트리맵 데이터 — 개별 종목 시총·섹터·세부산업·당일등락 (TradingView식).

TradingView 히트맵 = 미국 주요 개별 종목 수백 개를 시총 크기로 섹터(+세부산업)별로
묶어 등락을 한눈에. 이 모듈이 그 데이터를 공급한다.

데이터 경로:
1. 섹터/세부산업: FDR StockListing("S&P500")의 Sector/Industry 컬럼.
2. 시총: us_screener.db(미장스크리너 cron)에 있으면 사용, 없으면 SEC 발행주식수×종가.
3. 당일등락: 시총 계산용 가격 fetch에서 동시 산출(추가 비용 0).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def load_us_market_map(top_n: int = 500, universe_limit: int = 520):
    """(caps, changes, sectors, industries). 시총 상위 top_n 개별 종목.

    caps/changes는 종목당 1회 가격 fetch로 동시 확보(SEC 폴백 시). 어느 경로든
    changes만 있어도 트리맵이 렌더되도록 보장.
    """
    sectors, industries = _sector_industry_map()
    caps, changes = _caps_changes(sectors, universe_limit)

    # 표시 대상: 시총 상위 top_n (시총 없으면 등락 보유 종목)
    if caps:
        keep = sorted(caps, key=lambda s: -caps[s])[:top_n]
    else:
        keep = list(changes.keys())[:top_n]
    keep_set = set(keep)
    caps = {s: caps[s] for s in keep if s in caps}
    changes = {s: changes[s] for s in keep_set if s in changes}
    log.info("[us_breadth] caps=%d changes=%d sectors=%d industries=%d (top_n=%d)",
             len(caps), len(changes), len(sectors), len(industries), top_n)
    return caps, changes, sectors, industries


def _sector_industry_map():
    """FDR S&P500 listing → (sector_map, industry_map). 실패 시 ({}, {})."""
    try:
        from src.us_screener.data_source import _fdr_listing
        df = _fdr_listing("S&P500")
        if df is None or df.empty:
            return {}, {}
        cols = list(df.columns)
        sym_c = next((c for c in ("Symbol", "Code", "Ticker") if c in cols), None)
        sec_c = next((c for c in ("Sector", "sector") if c in cols), None)
        ind_c = next((c for c in ("Industry", "industry") if c in cols), None)
        sec_map: dict[str, str] = {}
        ind_map: dict[str, str] = {}
        if not sym_c:
            return {}, {}
        for _, row in df.iterrows():
            try:
                sym = str(row[sym_c]).strip().upper()
            except Exception:
                continue
            if not sym or sym == "NAN":
                continue
            if sec_c:
                v = str(row[sec_c]).strip()
                if v and v.lower() != "nan":
                    sec_map[sym] = v
            if ind_c:
                v = str(row[ind_c]).strip()
                if v and v.lower() != "nan":
                    ind_map[sym] = v
        return sec_map, ind_map
    except Exception:
        log.warning("[us_breadth] sector/industry map 실패", exc_info=True)
        return {}, {}


def _caps_changes(sectors: dict, limit: int):
    """(caps, changes). db 시총 있으면 사용(+등락 fetch), 없으면 SEC 폴백(가격 1회 fetch로 동시)."""
    from src.report.data.fetch_prices import fetch_many, day_change

    # Path A: 미장스크리너 db 시총 (Railway cron이 채움)
    try:
        from src.us_screener import db
        rows = db.get_active_tickers()
        dbcaps = {r["ticker"]: int(r["market_cap"]) for r in rows if r.get("market_cap")}
        if len(dbcaps) >= 50:
            keep = sorted(dbcaps, key=lambda s: -dbcaps[s])[:limit]
            dfs = fetch_many({s: s for s in keep}, days=10, workers=12)
            changes = {}
            for s, d in dfs.items():
                dc = day_change(d)
                if dc is not None:
                    changes[s] = dc
            log.info("[us_breadth] db 시총 %d개 사용", len(dbcaps))
            return {s: dbcaps[s] for s in keep}, changes
        log.info("[us_breadth] db 시총 %d개(<50) — SEC 폴백", len(dbcaps))
    except Exception:
        log.warning("[us_breadth] db 조회 실패 — SEC 폴백", exc_info=True)

    # Path B: SEC 발행주식수 × 종가 (가격 fetch 1회로 caps + changes 동시)
    from src.us_screener import fundamentals as fund
    syms = (list(sectors.keys()) or [])[:limit]
    if not syms:
        log.warning("[us_breadth] 유니버스(FDR 섹터) 미확보 — 빈 반환")
        return {}, {}
    dfs = fetch_many({s: s for s in syms}, days=10, workers=12)
    caps: dict[str, int] = {}
    changes: dict[str, float] = {}
    for s, d in dfs.items():
        try:
            close = float(d["Close"].iloc[-1])
            mc = fund.market_cap_usd(s, close)
            if mc:
                caps[s] = int(mc)
            dc = day_change(d)
            if dc is not None:
                changes[s] = dc
        except Exception:
            continue
    log.info("[us_breadth] SEC 폴백 시총 %d개·등락 %d개", len(caps), len(changes))
    return caps, changes
