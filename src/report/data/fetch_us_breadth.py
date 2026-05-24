"""S&P500 트리맵 데이터 — 시총·섹터·당일등락.

데이터 소스 우선순위:
1. us_screener.db.get_active_tickers() — 미장스크리너 cron이 일별로 market_cap+sector 저장
   (같은 Railway /data 볼륨 공유). 즉시·무네트워크. 1순위.
2. 폴백: FDR StockListing 섹터(503) + SEC 발행주식수(EDGAR, 캐시) × 종가 = 시총.
   FDR가 더 이상 시총 컬럼을 안 줘서 SEC로 계산. 신선한 컨테이너/미장cron 미실행 대비.

어느 경로든 caps가 비어도 sectors+changes로 균등 트리맵을 그릴 수 있게 (caps, changes, sectors)
세 dict을 반환 — 트리맵이 절대 빈 화면이 되지 않도록.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def load_us_market_map(top_n: int = 140, fallback_limit: int = 510):
    """(caps, changes, sectors) 반환. caps/sectors는 가능한 만큼, changes는 표시할 top_n."""
    caps, sectors = _load_caps_sectors(fallback_limit)
    from src.report.data.fetch_prices import fetch_many, day_change

    if caps:
        universe = sorted(caps, key=lambda s: -caps[s])[:top_n]
    else:
        # 시총 미확보 → 섹터 universe로 균등 트리맵(당일 등락 색)
        universe = list(sectors.keys())[:top_n]

    changes: dict[str, float] = {}
    if universe:
        dfs = fetch_many({s: s for s in universe}, days=10, workers=12)
        for s, df in dfs.items():
            dc = day_change(df)
            if dc is not None:
                changes[s] = dc
    log.info("[us_breadth] caps=%d sectors=%d changes=%d (top_n=%d)",
             len(caps), len(sectors), len(changes), top_n)
    return caps, changes, sectors


def _load_caps_sectors(fallback_limit: int):
    # Path A: 미장스크리너 db (Railway에서 cron이 채움)
    try:
        from src.us_screener import db
        rows = db.get_active_tickers()
        caps = {r["ticker"]: int(r["market_cap"]) for r in rows if r.get("market_cap")}
        secs = {r["ticker"]: r["sector"] for r in rows if r.get("sector")}
        if len(caps) >= 50:
            log.info("[us_breadth] db 시총 %d개·섹터 %d개 사용", len(caps), len(secs))
            return caps, secs
        log.info("[us_breadth] db 시총 %d개(<50) — SEC 폴백", len(caps))
    except Exception:
        log.warning("[us_breadth] db 조회 실패 — SEC 폴백", exc_info=True)

    # Path B: FDR 섹터 + SEC 발행주식수 × 종가
    return _fallback_caps_sectors(fallback_limit)


def _fallback_caps_sectors(limit: int):
    from src.us_screener import data_source as us_ds, fundamentals as fund
    from src.report.data.fetch_prices import fetch_many
    sectors = us_ds.fetch_sectors()
    if not sectors:
        log.warning("[us_breadth] FDR 섹터도 미확보 — 시총·섹터 빈 반환")
        return {}, {}
    syms = list(sectors.keys())[:limit]
    dfs = fetch_many({s: s for s in syms}, days=10, workers=12)
    caps: dict[str, int] = {}
    for s, df in dfs.items():
        try:
            close = float(df["Close"].iloc[-1])
            mc = fund.market_cap_usd(s, close)
            if mc:
                caps[s] = int(mc)
        except Exception:
            continue
    secs = {s: sectors[s] for s in caps} if caps else sectors
    log.info("[us_breadth] SEC 폴백 시총 %d개", len(caps))
    return caps, secs
