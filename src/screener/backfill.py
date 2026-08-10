"""1회성 1년치 백필 — Naver ticker-batch 1순위, pykrx/FDR 폴백.

기본 모드 (1순위): Naver Finance siseJson API로 종목별 1년치를 단일 요청에 받음
(시총 desc 정렬, ~1200종목). incremental·validator에서 검증된 1순위 소스로,
시뮬레이션 환경에서도 정확한 정규장 종가를 반환.

폴백: pykrx.get_market_ohlcv(date, market="ALL") date-batch → FDR ticker-batch.
이 환경에서 pykrx/FDR은 일관 실패하지만 운영 환경 대비 보존.

호출은 blocking이므로 봇 핸들러에서 run_in_executor로 감쌀 것.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.screener import data_source, db, universe

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

DEFAULT_DAYS = 1260  # 5년 — 역사적 신고가(ATH 근접)와 52주 신고가 차별화
SLEEP_BETWEEN_DAYS_S = 1.5
SLEEP_BETWEEN_TICKERS_S = 0.3
# date-batch가 N영업일 연속 빈 결과면 데이터 소스 다운으로 판단해 ticker-batch로 전환
DATE_BATCH_FAIL_THRESHOLD = 5


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def run_full_backfill(
    days: int = DEFAULT_DAYS,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> dict:
    """전체 백필. 반환: {"success": int, "fail": int, "rows": int, "skipped_existing": int, "mode": str}.

    progress_cb(done, total, success) — 주기적으로 호출. None이면 로그만.
    데이터 소스 우선순위: Naver ticker-batch → pykrx date-batch → FDR ticker-batch.
    """
    db.ensure_schema()
    # universe 먼저 보장
    if not db.get_active_tickers():
        log.info("[backfill] universe 비어있음 → refresh")
        universe.refresh_universe()

    active_set = {t["ticker"] for t in db.get_active_tickers()}

    # 0순위: Naver ticker-batch (1년치 단일 요청)
    naver_result = _run_naver_batch_backfill(days=days, progress_cb=progress_cb)
    # resume 재실행에서 전 종목이 이미 완료(skip)여도 성공 — date-batch 폴백으로 새지 않게
    if naver_result["rows"] > 0 or naver_result.get("skipped_existing", 0) > 0:
        return naver_result
    log.warning("[backfill] Naver 백필 빈 결과 → pykrx date-batch 폴백 시도")

    today = datetime.now(KST).date()
    target_dates = data_source.last_n_business_days(today, days)
    log.info("[backfill] 시작 (date-batch): %d영업일 (%s ~ %s)", len(target_dates), target_dates[0], target_dates[-1])

    success = fail = total_rows = skipped = 0
    consecutive_empty = 0
    fallback_triggered = False

    for i, ymd in enumerate(target_dates, 1):
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        if db.has_date(iso):
            skipped += 1
            consecutive_empty = 0
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
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                log.info("[backfill] %s 빈 결과 (휴장일?) consecutive_empty=%d", ymd, consecutive_empty)
        except Exception:
            fail += 1
            consecutive_empty += 1
            log.exception("[backfill] %s 실패", ymd)

        if i % 25 == 0 or i == len(target_dates):
            msg = f"[backfill] 진행 {i}/{len(target_dates)} success={success} skipped={skipped} fail={fail} rows={total_rows}"
            log.info(msg)
            if progress_cb:
                try:
                    progress_cb(i, len(target_dates), success)
                except Exception:
                    log.exception("[backfill] progress_cb 실패")

        # date-batch 일관 실패 감지 → ticker-batch 폴백
        if (
            success == 0
            and consecutive_empty >= DATE_BATCH_FAIL_THRESHOLD
            and not fallback_triggered
        ):
            log.warning(
                "[backfill] date-batch %d일 연속 빈 결과 → ticker-batch (FDR) 폴백",
                consecutive_empty,
            )
            fallback_triggered = True
            return _run_ticker_batch_backfill(
                days=days, progress_cb=progress_cb, active_set=active_set
            )

        time.sleep(SLEEP_BETWEEN_DAYS_S)

    db.meta_set(
        "backfill_summary",
        json.dumps(
            {
                "success": success, "fail": fail, "rows": total_rows,
                "skipped_existing": skipped, "mode": "date-batch",
                "completed_at": datetime.now(KST).isoformat(),
            },
            ensure_ascii=False,
        ),
    )
    log.info("[backfill] 완료 (date-batch) success=%d fail=%d rows=%d skipped=%d", success, fail, total_rows, skipped)
    return {
        "success": success, "fail": fail, "rows": total_rows,
        "skipped_existing": skipped, "mode": "date-batch",
    }


def _run_naver_batch_backfill(
    days: int,
    progress_cb: Optional[Callable[[int, int, int], None]],
) -> dict:
    """Naver Finance 종목별 1년치 fetch (1순위). 단일 요청에 full history.

    시총 desc 정렬 (대형주 우선). cap/timeout으로 보호:
      - SCREENER_BACKFILL_NAVER_CAP (기본 1600)
      - SCREENER_BACKFILL_NAVER_TIMEOUT_S (기본 1200=20분)
    """
    db.ensure_schema()
    cap = _int_env("SCREENER_BACKFILL_NAVER_CAP", 1600)
    timeout_s = _int_env("SCREENER_BACKFILL_NAVER_TIMEOUT_S", 1200)

    # 시총 desc 정렬 (대형주 우선 — timeout 시 소형주만 누락)
    all_tickers = db.get_active_tickers()
    all_tickers.sort(key=lambda t: -(t.get("market_cap") or 0))
    tickers = [t["ticker"] for t in all_tickers][:cap]

    today = datetime.now(KST).date()
    start_iso = (today - timedelta(days=int(days * 1.5) + 15)).strftime("%Y-%m-%d")
    end_iso = today.strftime("%Y-%m-%d")
    log.info(
        "[backfill] Naver 시작: %d종목 (%s ~ %s, cap=%d, timeout=%ds)",
        len(tickers), start_iso, end_iso, cap, timeout_s,
    )

    # resume 지원 — 이미 충분한 히스토리(기본 1000행+)를 가진 종목은 skip.
    # timeout으로 중단된 백필을 재실행하면 완료분을 건너뛰고 꼬리부터 이어감.
    skip_rows = _int_env("SCREENER_BACKFILL_SKIP_ROWS", 1000)
    have = db.rows_per_ticker()

    success = fail = total_rows = skipped_existing = 0
    t0 = time.monotonic()
    for i, ticker in enumerate(tickers, 1):
        if time.monotonic() - t0 > timeout_s:
            log.warning("[backfill] Naver timeout %ds 초과 → %d/%d에서 중단", timeout_s, i - 1, len(tickers))
            break
        if have.get(ticker, 0) >= skip_rows:
            skipped_existing += 1
            continue
        try:
            rows = data_source.fetch_ohlcv_by_ticker_via_naver(ticker, start_iso, end_iso)
            if rows:
                inserted = db.upsert_ohlcv_bulk(rows)
                total_rows += inserted
                success += 1
            else:
                fail += 1
        except Exception:
            fail += 1
            log.exception("[backfill] Naver %s 실패", ticker)

        if i % 100 == 0 or i == len(tickers):
            log.info(
                "[backfill] Naver 진행 %d/%d success=%d fail=%d rows=%d",
                i, len(tickers), success, fail, total_rows,
            )
            if progress_cb:
                try:
                    progress_cb(i, len(tickers), success)
                except Exception:
                    log.exception("[backfill] progress_cb 실패")

    db.meta_set(
        "backfill_summary",
        json.dumps(
            {
                "success": success, "fail": fail, "rows": total_rows,
                "skipped_existing": skipped_existing, "mode": "naver-ticker-batch",
                "completed_at": datetime.now(KST).isoformat(),
            },
            ensure_ascii=False,
        ),
    )
    if total_rows > 0:
        db.meta_set("naver_backfill_done", datetime.now(KST).isoformat())
    log.info(
        "[backfill] 완료 (Naver) success=%d fail=%d rows=%d skipped_existing=%d",
        success, fail, total_rows, skipped_existing,
    )
    return {
        "success": success, "fail": fail, "rows": total_rows,
        "skipped_existing": skipped_existing, "mode": "naver-ticker-batch",
    }


def _run_ticker_batch_backfill(
    days: int,
    progress_cb: Optional[Callable[[int, int, int], None]],
    active_set: set[str],
) -> dict:
    """FDR 기반 종목별 1년치 fetch. date-batch 폴백 전용.

    종목별로 1요청 = 1년치 → 종목 수만큼 호출 (~2,300종목 × 0.3s ≈ 12분).
    """
    today = datetime.now(KST).date()
    # 영업일이 252개 정도 되도록 365일 여유로 잡음
    start = today - timedelta(days=int(days * 1.5) + 10)
    start_iso = start.strftime("%Y-%m-%d")
    end_iso = today.strftime("%Y-%m-%d")

    if not active_set:
        log.error("[backfill] ticker-batch 폴백 진입했으나 universe 비어있음")
        return {"success": 0, "fail": 0, "rows": 0, "skipped_existing": 0, "mode": "ticker-batch"}

    tickers = sorted(active_set)
    log.info("[backfill] ticker-batch 시작: %d종목 (%s ~ %s)", len(tickers), start_iso, end_iso)

    success = fail = total_rows = 0
    for i, ticker in enumerate(tickers, 1):
        try:
            rows = data_source.fetch_ohlcv_by_ticker_via_fdr(ticker, start_iso, end_iso)
            if rows:
                inserted = db.upsert_ohlcv_bulk(rows)
                total_rows += inserted
                success += 1
            else:
                fail += 1
        except Exception:
            fail += 1
            log.exception("[backfill] ticker-batch %s 실패", ticker)

        if i % 100 == 0 or i == len(tickers):
            log.info(
                "[backfill] ticker-batch 진행 %d/%d success=%d fail=%d rows=%d",
                i, len(tickers), success, fail, total_rows,
            )
            if progress_cb:
                try:
                    progress_cb(i, len(tickers), success)
                except Exception:
                    log.exception("[backfill] progress_cb 실패")
        time.sleep(SLEEP_BETWEEN_TICKERS_S)

    db.meta_set(
        "backfill_summary",
        json.dumps(
            {
                "success": success, "fail": fail, "rows": total_rows,
                "skipped_existing": 0, "mode": "ticker-batch",
                "completed_at": datetime.now(KST).isoformat(),
            },
            ensure_ascii=False,
        ),
    )
    log.info(
        "[backfill] 완료 (ticker-batch) success=%d fail=%d rows=%d",
        success, fail, total_rows,
    )
    return {
        "success": success, "fail": fail, "rows": total_rows,
        "skipped_existing": 0, "mode": "ticker-batch",
    }
