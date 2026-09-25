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
RETRY_AFTER_DAYS = 30          # 성공(행 있음)했는데도 이력 짧음 = 신규 상장 → 30일 뒤 재확인
FAIL_RETRY_AFTER_DAYS = 1      # 빈 결과·오류 = 일시 장애일 수 있음 → 다음날 재시도
STALE_DAYS = 10                # DB 최신일이 대상일보다 이만큼 오래되면 공백 → 재백필


def _load_attempts() -> dict[str, str]:
    """{ticker: 'YYYY-MM-DD' (성공) | 'fail:YYYY-MM-DD' (빈 결과/오류)}."""
    try:
        return json.loads(db.meta_get(ATTEMPTS_META_KEY) or "{}")
    except (ValueError, TypeError):
        return {}


def _recently_attempted(stamp: Optional[str], today) -> bool:
    if not stamp:
        return False
    failed = stamp.startswith("fail:")
    day = stamp[5:] if failed else stamp
    wait = FAIL_RETRY_AFTER_DAYS if failed else RETRY_AFTER_DAYS
    try:
        return (today - datetime.fromisoformat(day).date()).days < wait
    except ValueError:
        return False


def _target_date():
    from src.us_screener import incremental
    return incremental.us_target_date()


def pending_tickers(min_rows: int = FULL_HISTORY_ROWS) -> list[str]:
    """백필 필요 종목 (시총 desc):
      - row < min_rows 이고 최근 시도 기록 없음 (성공 30일 / 실패 1일)
      - 또는 DB 최신일이 대상일보다 STALE_DAYS 넘게 오래됨 (비활성→재활성 공백·과거 사고 잔여)
    """
    active = db.get_active_tickers()
    stats = db.ticker_row_stats({t["ticker"] for t in active})
    attempts = _load_attempts()
    today = datetime.now(KST).date()
    stale_before = (_target_date() - timedelta(days=STALE_DAYS)).isoformat()
    out = []
    for t in sorted(active, key=lambda x: -(x.get("market_cap") or 0)):
        n, latest = stats.get(t["ticker"], (0, None))
        short = n < min_rows
        stale = n > 0 and latest is not None and latest < stale_before
        if not (short or stale):
            continue
        if _recently_attempted(attempts.get(t["ticker"]), today):
            continue
        out.append(t["ticker"])
    return out


def rebuild_history(tickers: list[str], days: int = DEFAULT_DAYS) -> dict:
    """분할·비율변경 종목의 이력 전체를 새로 받아 원자적으로 교체. 반환: {ticker: rows}."""
    end_iso = _target_date().isoformat()
    start_iso = (datetime.fromisoformat(end_iso) - timedelta(days=int(days * 1.5) + 15)).strftime("%Y-%m-%d")
    out = {}
    for t in tickers:
        rows = data_source.fetch_ohlcv_by_ticker_via_naver(t, start_iso, end_iso)
        # 새 이력이 기존보다 확연히 짧으면(소스 일시 장애) 교체 안 함 — 이력 손실 방지
        have = db.ticker_row_stats([t]).get(t, (0, None))[0]
        if rows and len(rows) >= min(have, 250) * 0.8:
            out[t] = db.replace_ticker_history(t, rows)
        else:
            out[t] = 0
            log.warning("[backfill] %s 재구축 보류 (새 %d행 vs 기존 %d행)", t, len(rows), have)
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
    # 미국은 date-batch 소스가 없고(fetch_market_ohlcv_by_date=[]), 이하 FDR 순차 폴백은 Yahoo와
    # 같은 엔드포인트라 이득 없이 timeout 없는 순차 호출만 늘린다 → 종목별 체인 결과로 종료.
    # (체인 자체가 Yahoo → Stooq → Nasdaq 폴백을 포함)
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

      - 종료일 = 마지막 마감 US 거래일 (KST 날짜를 쓰면 미국 장중 실행 시 진행 봉이 저장돼
        base_date가 앞당겨짐 — 리뷰 지적)
      - 워커가 직접 upsert하고 행 수만 반환 (5년치 결과를 메모리에 쌓지 않음)
      - 시도 기록: 성공은 날짜, 빈 결과·오류는 'fail:날짜' (다음날 재시도)
      - US_SCREENER_BACKFILL_WORKERS (6), US_SCREENER_BACKFILL_TIMEOUT_S (1500)
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
        return {"success": 0, "fail": 0, "rows": 0, "skipped_existing": 0, "mode": "noop",
                "failed": []}

    end_d = _target_date()
    start_iso = (end_d - timedelta(days=int(days * 1.5) + 15)).isoformat()
    end_iso = end_d.isoformat()
    log.info("[backfill] 시작: %d종목 (%s ~ %s, workers=%d, timeout=%ds)",
             len(tickers), start_iso, end_iso, workers, timeout_s)

    def _one(t: str) -> int:
        rows = data_source.fetch_ohlcv_by_ticker_via_naver(t, start_iso, end_iso)
        return db.upsert_ohlcv_bulk(rows) if rows else 0

    success = fail = total_rows = 0
    failed: list[str] = []
    attempts = _load_attempts()
    stamp = datetime.now(KST).date().isoformat()
    t0 = time.monotonic()
    done = 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as _FutTimeout
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(_one, t): t for t in tickers}
    try:
        for fut in as_completed(futures, timeout=timeout_s):
            t = futures.pop(fut)
            try:
                n = fut.result()
            except Exception as e:
                log.warning("[backfill] %s 예외: %s", t, e)
                n = 0
            if n:
                total_rows += n
                success += 1
                attempts[t] = stamp
            else:
                fail += 1
                failed.append(t)
                attempts[t] = f"fail:{stamp}"
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
    except _FutTimeout:
        log.warning("[backfill] timeout %ds → %d/%d에서 중단 (나머지는 다음 실행)",
                    timeout_s, done, len(tickers))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    db.meta_set(ATTEMPTS_META_KEY, json.dumps(attempts))
    if failed:
        log.warning("[backfill] 빈 결과 %d종목 (다음날 재시도): %s", len(failed), ", ".join(failed[:60]))

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
        "skipped_existing": 0, "mode": "ticker-batch-parallel", "failed": failed,
        "not_done": len(tickers) - done,
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
