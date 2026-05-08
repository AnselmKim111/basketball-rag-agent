"""매일 1일치 증분 업데이트.

매일 16:30 KST에 호출됨. 오늘 거래일 OHLCV를 fetch해 DB에 추가.
휴장일/장중 미발행이면 빈 결과 → 신호 계산은 누적 DB로 진행.

또한 280일 이전 데이터는 정리 (DB 비대화 방지).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from src.screener import data_source, db, universe

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 252거래일 + 여유 28일 = 280일치만 보관
RETENTION_DAYS = 280


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def update_today() -> dict:
    """오늘(KST) 데이터 fetch + 280일 이전 정리.

    반환: {"date": iso_str, "rows": int, "is_business_day": bool, "empty": bool}.

    pykrx date-batch 실패 시 FDR ticker-batch 폴백은 기본 비활성화 (sequential
    수천 종목 fetch가 1시간+ 걸려 self-test/cron을 hang시키기 때문).
    `SCREENER_INCREMENTAL_FDR_FALLBACK=1` 시 활성화하되 cap+timeout으로 보호:
      - SCREENER_INCREMENTAL_FDR_CAP (기본 80종목)
      - SCREENER_INCREMENTAL_FDR_TIMEOUT_S (기본 180초)
      - 50종목마다 progress log
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

    if not rows and active_set:
        if _int_env("SCREENER_INCREMENTAL_FDR_FALLBACK", 0) == 1:
            cap = _int_env("SCREENER_INCREMENTAL_FDR_CAP", 80)
            timeout_s = _int_env("SCREENER_INCREMENTAL_FDR_TIMEOUT_S", 180)
            log.warning(
                "[incremental] %s pykrx 빈 결과 → FDR ticker-batch 폴백 (cap=%d, timeout=%ds)",
                iso, cap, timeout_s,
            )
            start_iso = (today - timedelta(days=5)).strftime("%Y-%m-%d")
            end_iso = iso
            merged: list[tuple] = []
            t0 = time.monotonic()
            scanned = 0
            for ticker in sorted(active_set)[:cap]:
                if time.monotonic() - t0 > timeout_s:
                    log.warning(
                        "[incremental] FDR 폴백 timeout %ds 초과 → %d/%d에서 중단",
                        timeout_s, scanned, cap,
                    )
                    break
                try:
                    tr = data_source.fetch_ohlcv_by_ticker_via_fdr(ticker, start_iso, end_iso)
                    merged.extend([r for r in tr if r[1] >= start_iso])
                except Exception:
                    pass
                scanned += 1
                if scanned % 50 == 0:
                    log.info("[incremental] FDR 폴백 진행 %d/%d (rows=%d)", scanned, cap, len(merged))
            rows = merged
            log.info("[incremental] FDR 폴백 완료 scanned=%d rows=%d", scanned, len(rows))
        else:
            log.info(
                "[incremental] %s pykrx 빈 결과 (장중/휴장일 가능) → 폴백 skip, 누적 DB로 신호 계산 진행",
                iso,
            )

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
