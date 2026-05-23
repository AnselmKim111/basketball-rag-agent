"""미국 유니버스 빌드 — S&P500 + Nasdaq100.

한국 src/screener/universe.py와 동일 인터페이스(refresh_universe / refresh_market_caps /
get_universe_tickers)를 제공해 us_screener의 incremental/backfill 복사본이 무수정 작동.
미국은 FDR StockListing이 GICS 섹터를 직접 제공하므로 keyword 휴리스틱 불필요.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from src.us_screener import data_source, db

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


def refresh_universe() -> int:
    """S&P500 + Nasdaq100 fetch → DB upsert. 시총·섹터 함께 갱신. 반환: 활성 종목 수."""
    raw = data_source.fetch_us_tickers()
    if not raw:
        log.error("[us_universe] ticker list 비어있음 — FDR/네트워크 점검")
        return 0

    today = datetime.now(KST).strftime("%Y-%m-%d")

    caps: dict[str, int] = {}
    secs: dict[str, str] = {}
    try:
        caps = data_source.fetch_market_caps()
    except Exception:
        log.exception("[us_universe] market_cap fetch 실패 — 시총 없이 진행")
    try:
        secs = data_source.fetch_sectors()
    except Exception:
        log.exception("[us_universe] sector fetch 실패")

    rows: list[tuple] = []
    for symbol, name, index_label in raw:
        cap = caps.get(symbol)
        rows.append((symbol, name or symbol, index_label, 1, today, cap))

    db.upsert_tickers(rows)
    if secs:
        db.update_sectors(secs)
    log.info(
        "[us_universe] %d종목 활성화 (S&P500+NASDAQ100, 시총 %d, 섹터 %d)",
        len(rows), len(caps), len(secs),
    )
    return len(rows)


def refresh_market_caps() -> int:
    """시총 + 섹터만 갱신 (universe rebuild 없이). cron 매일 호출."""
    updated = 0
    try:
        caps = data_source.fetch_market_caps()
        if caps:
            updated = db.update_market_caps(caps)
    except Exception:
        log.exception("[us_universe] market_cap fetch 실패")
    try:
        secs = data_source.fetch_sectors()
        if secs:
            db.update_sectors(secs)
            log.info("[us_universe] sector 갱신 %d종목", len(secs))
    except Exception:
        log.exception("[us_universe] sector fetch 실패")
    return updated


def get_universe_tickers() -> list[dict]:
    items = db.get_active_tickers()
    if not items:
        refresh_universe()
        items = db.get_active_tickers()
    return items
