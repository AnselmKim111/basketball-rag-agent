"""미국 유니버스 빌드 — 광역 보통주 (시총 floor 이상).

Nasdaq screener → FDR 거래소 listing → S&P500+NASDAQ100 안전망의 3단 소스(data_source.
fetch_us_listings)로 NYSE/NASDAQ/AMEX 보통주 전체를 받아 시총 floor 이상만 활성화.
floor 미달·상장폐지 종목은 매일 비활성화(동적 유니버스). incremental/backfill 복사본은
db.get_active_tickers를 소비하므로 무수정 작동.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

from src.us_screener import data_source, db

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 유니버스 시총 floor (USD). RVMD($9B)급 미드캡 포함 + fetch 예산 유지 → 기본 $2B.
DEFAULT_UNIVERSE_MIN_CAP = 2_000_000_000


def _universe_min_cap() -> float:
    try:
        return float(os.getenv("SCREENER_US_UNIVERSE_MIN_CAP", "") or DEFAULT_UNIVERSE_MIN_CAP)
    except ValueError:
        return DEFAULT_UNIVERSE_MIN_CAP


def refresh_universe() -> dict:
    """광역 보통주 fetch → 시총 floor 필터 → upsert + 동적 비활성화 + audit.

    반환: {"count": 활성종목수, "new_entries": 신규진입수, "dropped": 비활성화수,
           "floor": floor, "no_cap": 시총결측수}.
    실패 시 count=0 (호출자가 baseline 알림).
    """
    listings = data_source.fetch_us_listings()
    if not listings:
        log.error("[us_universe] listing 비어있음 — 소스/네트워크 점검")
        return {"count": 0, "new_entries": 0, "dropped": 0,
                "floor": _universe_min_cap(), "no_cap": 0}

    today = datetime.now(KST).strftime("%Y-%m-%d")
    floor = _universe_min_cap()

    prev_active = db.get_active_symbols()

    rows: list[tuple] = []
    secs: dict[str, str] = {}
    no_cap = 0
    kept: set[str] = set()
    for symbol, name, cap, sector, exch in listings:
        # 시총 floor: cap 있으면 floor 이상만. cap 결측(None)은 보수적 제외
        # (광역 소스는 시총을 제공하므로 결측은 비정상 — 안전망 소스일 때만 발생).
        if cap is None:
            no_cap += 1
            continue
        if cap < floor:
            continue
        rows.append((symbol, name or symbol, exch, 1, today, cap))
        kept.add(symbol)
        if sector:
            secs[symbol] = sector

    if not rows:
        log.error("[us_universe] floor 필터 후 0종목 (floor=$%.1fB, no_cap=%d) — upsert/비활성화 skip",
                  floor / 1e9, no_cap)
        return {"count": 0, "new_entries": 0, "dropped": 0, "floor": floor, "no_cap": no_cap}

    db.upsert_tickers(rows)
    if secs:
        db.update_sectors(secs)
    dropped = db.deactivate_missing(kept)

    new_entries = len(kept - prev_active)
    audit = {
        "count": len(kept), "new_entries": new_entries, "dropped": dropped,
        "floor": floor, "no_cap": no_cap, "at": today,
    }
    try:
        db.meta_set("us_universe_audit", json.dumps(audit, ensure_ascii=False))
    except Exception:
        log.exception("[us_universe] audit meta_set 실패")
    log.info(
        "[us_universe] %d종목 활성화 (floor=$%.1fB, 신규 %d, 비활성화 %d, 섹터 %d, no_cap %d)",
        len(kept), floor / 1e9, new_entries, dropped, len(secs), no_cap,
    )
    return audit


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
