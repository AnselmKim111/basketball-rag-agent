"""매일 1일치 증분 업데이트.

매일 16:30 KST에 호출됨. 오늘 거래일 OHLCV를 fetch해 DB에 추가.
휴장일이면 빈 결과 → 0 반환.

또한 280일 이전 데이터는 정리 (DB 비대화 방지).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.screener import data_source, db, universe

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 252거래일 + 여유 28일 = 280일치만 보관
RETENTION_DAYS = 280


def update_today() -> dict:
    """오늘(KST) 데이터 fetch + 280일 이전 정리.

    반환: {"date": iso_str, "rows": int, "is_business_day": bool, "empty": bool}.
    """
    db.ensure_schema()
    if not db.get_active_tickers():
        log.info("[incremental] universe 비어있음 → refresh")
        universe.refresh_universe()
    active_set = {t["ticker"] for t in db.get_active_tickers()}

    today = datetime.now(KST).date()
    iso = today.strftime("%Y-%m-%d")
    ymd = today.strftime("%Y%m%d")

    if not data_source.is_business_day(today):
        log.info("[incremental] %s 주말 — skip", iso)
        return {"date": iso, "rows": 0, "is_business_day": False, "empty": True}

    rows = data_source.fetch_market_ohlcv_by_date(ymd)
    if active_set:
        rows = [r for r in rows if r[0] in active_set]

    # pykrx date-batch 실패 시 FDR ticker-batch 폴백 (어제~오늘 1-3일치)
    if not rows and active_set:
        log.warning("[incremental] %s pykrx 빈 결과 → FDR ticker-batch 폴백", iso)
        start_iso = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        end_iso = iso
        merged: list[tuple] = []
        for ticker in sorted(active_set):
            try:
                tr = data_source.fetch_ohlcv_by_ticker_via_fdr(ticker, start_iso, end_iso)
                # 오늘 또는 어제 데이터만 추출
                merged.extend([r for r in tr if r[1] >= start_iso])
            except Exception:
                pass
        rows = merged

    if not rows:
        log.info("[incremental] %s 빈 결과 (휴장일?)", iso)
        return {"date": iso, "rows": 0, "is_business_day": True, "empty": True}

    inserted = db.upsert_ohlcv_bulk(rows)

    # 오래된 데이터 정리
    cutoff = (today - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    deleted = db.delete_older_than(cutoff)
    if deleted:
        log.info("[incremental] %d행 정리 (cutoff=%s)", deleted, cutoff)

    log.info("[incremental] %s 추가 rows=%d", iso, inserted)
    return {"date": iso, "rows": inserted, "is_business_day": True, "empty": False}
