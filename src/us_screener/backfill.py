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

from src.us_screener import data_source, db, universe

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


ATTEMPTS_META_KEY = "backfill_attempts"
# row 수가 이 미만이면 이력 부족 (5년 ≈ 1260행; 상장 5년 미만 종목은 시도 기록으로 재시도 억제)
FULL_HISTORY_ROWS = 1000
RETRY_AFTER_DAYS = 30


def _load_attempts() -> dict[str, str]:
    try:
        return json.loads(db.meta_get(ATTEMPTS_META_KEY) or "{}")
    except (ValueError, TypeError):
        return {}


def pending_tickers(min_rows: int = FULL_HISTORY_ROWS) -> list[str]:
    """백필 필요 종목: 활성 + row < min_rows + 최근 RETRY_AFTER_DAYS일 내 시도 기록 없음.

    시총 desc 정렬. 신규 편입(유니버스 확장·신규 상장)은 여기로 잡히고, 이력이 짧은 신규
    상장은 한 번 시도 후 30일간 재시도 안 함 (매일 백필 재트리거 방지).
    """
    active = db.get_active_tickers()
    stats = db.ticker_row_stats({t["ticker"] for t in active})
    attempts = _load_attempts()
    cutoff = (datetime.now(KST).date() - timedelta(days=RETRY_AFTER_DAYS)).isoformat()
    out = []
    for t in sorted(active, key=lambda x: -(x.get("market_cap") or 0)):
        n = stats.get(t["ticker"], (0, None))[0]
        if n >= min_rows:
            continue
        last = attempts.get(t["ticker"])
        if last and last >= cutoff:
            continue
        out.append(t["ticker"])
    return out


def run_full_backfill(
    days: int = DEFAULT_DAYS,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
    tickers: Optional[list[str]] = None,
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

    # 0순위: 종목별 batch (Yahoo→Nasdaq gap 보충→FDR/Stooq), 5년치 단일 요청, 병렬.
    # tickers 미지정 = 이력 부족 종목만 (pending_tickers). 전체 재수집은 tickers=전체 명시.
    if tickers is None:
        tickers = pending_tickers()
    naver_result = _run_naver_batch_backfill(days=days, progress_cb=progress_cb, tickers=tickers)
    if naver_result["rows"] > 0 or not tickers:
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
    tickers: Optional[list[str]] = None,
) -> dict:
    """종목별 5년치 fetch (병렬). 시총 desc (대형주 우선 — timeout 시 소형주만 누락).

      - US_SCREENER_BACKFILL_WORKERS (기본 6), US_SCREENER_BACKFILL_TIMEOUT_S (기본 1500)
      - 시도한 종목은 meta backfill_attempts에 날짜 기록 (성공·빈 결과 모두)
    """
    db.ensure_schema()
    timeout_s = _int_env("US_SCREENER_BACKFILL_TIMEOUT_S", 1500)
    workers = max(1, _int_env("US_SCREENER_BACKFILL_WORKERS", 6))
    if tickers is None:
        all_tickers = db.get_active_tickers()
        all_tickers.sort(key=lambda t: -(t.get("market_cap") or 0))
        tickers = [t["ticker"] for t in all_tickers]
    if not tickers:
        log.info("[backfill] 대상 종목 없음 — skip")
        return {"success": 0, "fail": 0, "rows": 0, "skipped_existing": 0, "mode": "noop"}

    today = datetime.now(KST).date()
    start_iso = (today - timedelta(days=int(days * 1.5) + 15)).strftime("%Y-%m-%d")
    end_iso = today.strftime("%Y-%m-%d")
    log.info("[backfill] 시작: %d종목 (%s ~ %s, workers=%d, timeout=%ds)",
             len(tickers), start_iso, end_iso, workers, timeout_s)

    def _one(t: str) -> tuple[str, list[tuple]]:
        return t, data_source.fetch_ohlcv_by_ticker_via_naver(t, start_iso, end_iso)

    success = fail = total_rows = 0
    failed: list[str] = []
    attempts = _load_attempts()
    stamp = today.isoformat()
    t0 = time.monotonic()
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as _FutTimeout
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, t): t for t in tickers}
        done = 0
        try:
            for fut in as_completed(futures, timeout=timeout_s + 60):
                t = futures[fut]
                try:
                    _, rows = fut.result()
                except Exception as e:
                    log.warning("[backfill] %s 예외: %s", t, e)
                    rows = []
                if rows:
                    total_rows += db.upsert_ohlcv_bulk(rows)
                    success += 1
                else:
                    fail += 1
                    failed.append(t)
                attempts[t] = stamp
                done += 1
                if done % 200 == 0 or done == len(tickers):
                    log.info("[backfill] 진행 %d/%d success=%d fail=%d rows=%d",
                             done, len(tickers), success, fail, total_rows)
                    db.meta_set(ATTEMPTS_META_KEY, json.dumps(attempts))
                    if progress_cb:
                        try:
                            progress_cb(done, len(tickers), success)
                        except Exception:
                            log.exception("[backfill] progress_cb 실패")
                if time.monotonic() - t0 > timeout_s:
                    log.warning("[backfill] timeout %ds → %d/%d에서 중단 (나머지는 다음 실행)",
                                timeout_s, done, len(tickers))
                    for f in futures:
                        f.cancel()
                    break
        except _FutTimeout:
            log.warning("[backfill] futures timeout → %d/%d", done, len(tickers))
            for f in futures:
                f.cancel()
    db.meta_set(ATTEMPTS_META_KEY, json.dumps(attempts))
    if failed:
        log.warning("[backfill] 빈 결과 %d종목: %s", len(failed), ", ".join(failed[:60]))

    db.meta_set(
        "backfill_summary",
        json.dumps(
            {
                "success": success, "fail": fail, "rows": total_rows,
                "skipped_existing": 0, "mode": "ticker-batch-parallel",
                "completed_at": datetime.now(KST).isoformat(),
            },
            ensure_ascii=False,
        ),
    )
    if total_rows > 0:
        db.meta_set("naver_backfill_done", datetime.now(KST).isoformat())
    log.info("[backfill] 완료 success=%d fail=%d rows=%d (%.0fs)",
             success, fail, total_rows, time.monotonic() - t0)
    return {
        "success": success, "fail": fail, "rows": total_rows,
        "skipped_existing": 0, "mode": "ticker-batch-parallel",
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
