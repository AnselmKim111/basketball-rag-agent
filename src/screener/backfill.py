"""1회성 1년치 백필 — date-batch 방식.

pykrx의 get_market_ohlcv(date, market="ALL")로 그 날짜의 전 종목을 한 번에 받아오므로
종목 단위 백필(2,500 req)이 아닌 252영업일 × 1 req로 끝남.

호출은 blocking이므로 봇 핸들러에서 run_in_executor로 감쌀 것.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.screener import data_source, db, universe

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

DEFAULT_DAYS = 252
SLEEP_BETWEEN_DAYS_S = 1.5


def run_full_backfill(
    days: int = DEFAULT_DAYS,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> dict:
    """전체 백필. 반환: {"success": int, "fail": int, "rows": int, "skipped_existing": int}.

    progress_cb(done, total, success) — 25일마다 호출. None이면 로그만.
    """
    db.ensure_schema()
    # universe 먼저 보장
    if not db.get_active_tickers():
        log.info("[backfill] universe 비어있음 → refresh")
        universe.refresh_universe()

    active_set = {t["ticker"] for t in db.get_active_tickers()}

    today = datetime.now(KST).date()
    target_dates = data_source.last_n_business_days(today, days)
    log.info("[backfill] 시작: %d영업일 (%s ~ %s)", len(target_dates), target_dates[0], target_dates[-1])

    success = fail = total_rows = skipped = 0
    for i, ymd in enumerate(target_dates, 1):
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        if db.has_date(iso):
            skipped += 1
            continue
        try:
            rows = data_source.fetch_market_ohlcv_by_date(ymd)
            # universe 필터 (보통주만 저장 → DB 용량 절약)
            if active_set:
                rows = [r for r in rows if r[0] in active_set]
            if rows:
                inserted = db.upsert_ohlcv_bulk(rows)
                total_rows += inserted
                success += 1
            else:
                # 빈 결과 = 휴장일 — 실패 아님
                log.info("[backfill] %s 빈 결과 (휴장일?)", ymd)
        except Exception:
            fail += 1
            log.exception("[backfill] %s 실패", ymd)

        if i % 25 == 0 or i == len(target_dates):
            msg = f"[backfill] 진행 {i}/{len(target_dates)} success={success} skipped={skipped} fail={fail} rows={total_rows}"
            log.info(msg)
            if progress_cb:
                try:
                    progress_cb(i, len(target_dates), success)
                except Exception:
                    log.exception("[backfill] progress_cb 실패")
        time.sleep(SLEEP_BETWEEN_DAYS_S)

    db.meta_set(
        "backfill_summary",
        json.dumps(
            {
                "success": success, "fail": fail, "rows": total_rows,
                "skipped_existing": skipped,
                "completed_at": datetime.now(KST).isoformat(),
            },
            ensure_ascii=False,
        ),
    )
    log.info("[backfill] 완료 success=%d fail=%d rows=%d skipped=%d", success, fail, total_rows, skipped)
    return {"success": success, "fail": fail, "rows": total_rows, "skipped_existing": skipped}
