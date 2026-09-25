"""신호 종목 cross-validation — 발송 전 마지막 데이터 정합성 검증.

배경: 신호 계산이 끝난 후 마지막 한 단계 더 — 신호 종목들의 base_date 종가를
독립적으로 다시 fetch하여 DB 값과 대조. 외부 데이터 소스 lag/캐시/누락으로 인한
잘못된 신호를 메시지 발송 직전 차단.

검증 절차:
  1. 신호 발생 종목 리스트 (보통 50-150개) 추출
  2. 각 종목 재조회 (US_SCREENER_VALIDATE_WORKERS 병렬, 종목 수 비례 timeout)
  3. 응답의 base_date close vs 우리 DB의 close 비교 — 불일치 시 그 종목 제외
  4. 검증 통과 종목만 결과에 유지

비용: 신호 종목 ~200개 × ~0.4초 / 6 workers ≈ 15초. timeout = max(120s, 0.6s×종목수).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterable

from src.us_screener import data_source

log = logging.getLogger(__name__)


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _all_signal_tickers(results: dict[str, list[dict]]) -> set[str]:
    """결과 dict에서 신호 발생한 모든 ticker(unique) 추출."""
    out: set[str] = set()
    for items in results.values():
        for it in items:
            t = it.get("ticker")
            if t:
                out.add(t)
    return out


def cross_validate(
    results: dict[str, list[dict]],
    base_date: str,
) -> tuple[dict[str, list[dict]], dict]:
    """신호 종목들의 base_date close 값을 독립 fetch로 cross-check.

    반환: (validated_results, validation_stats).
    validated_results는 검증 통과 종목만 유지. 불일치 종목은 모든 카테고리에서 제거.
    """
    tolerance = _int_env("SCREENER_VALIDATE_TOLERANCE", 1)  # ±1 cent (반올림 오차)

    tickers = _all_signal_tickers(results)
    if not tickers:
        return results, {"validated": 0, "rejected": 0, "fetch_failed": 0, "skipped_timeout": 0}
    # 유니버스 ~2,550 → 신호 종목 수백 개 가능. KR과 env 분리 + 종목 수 비례 (리뷰 지적:
    # 순차 60초로는 알파벳 뒤쪽 신호가 매일 NoFetch로 탈락).
    timeout_s = _int_env("US_SCREENER_VALIDATE_TIMEOUT_S", max(120, int(len(tickers) * 0.6)))
    workers = max(1, _int_env("US_SCREENER_VALIDATE_WORKERS", 6))
    log.info("[validator] %d종목 cross-validate 시작 (base_date=%s, workers=%d, timeout=%ds)",
             len(tickers), base_date, workers, timeout_s)

    from datetime import datetime as _dt, timedelta as _td
    target_dt = _dt.strptime(base_date, "%Y-%m-%d").date()
    start = (target_dt - _td(days=3)).strftime("%Y-%m-%d")
    end = (target_dt + _td(days=1)).strftime("%Y-%m-%d")
    # base_date 외 날짜는 '보유'로 넘겨 불필요한 gap 보충 호출 방지 (base 봉만 필요)
    known = {(target_dt + _td(days=k)).isoformat() for k in range(-3, 2) if k != 0}

    def _fetch_one(ticker: str) -> tuple[str, int | None]:
        rows = data_source.fetch_ohlcv_by_ticker_via_naver(ticker, start, end, known_dates=known)
        match = [r for r in rows if r[1] == base_date]
        return ticker, (int(match[0][5]) if match else None)

    truth: dict[str, int | None] = {}
    fetch_failed = 0
    skipped_timeout = 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as _FutTimeout
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(_fetch_one, t): t for t in sorted(tickers)}
    try:
        for fut in as_completed(futures, timeout=timeout_s):
            t = futures[fut]
            try:
                _, close = fut.result()
            except Exception as e:
                log.debug("[validator] %s fetch 실패: %s", t, e)
                close = None
            truth[t] = close
            if close is None:
                fetch_failed += 1
    except _FutTimeout:
        skipped_timeout = len(tickers) - len(truth)
        log.warning("[validator] timeout %ds — %d종목 미검증", timeout_s, skipped_timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # 검증: 각 신호 종목의 DB close vs truth close 비교
    validated: dict[str, list[dict]] = {k: [] for k in results.keys()}
    rejected = 0
    rejected_tickers: list[str] = []
    for cat, items in results.items():
        for it in items:
            t = it.get("ticker")
            db_close = int(it.get("close", 0))
            true_close = truth.get(t)
            if true_close is None:
                # fetch 실패 — 보수적으로 제외 (잘못된 데이터 방지)
                rejected += 1
                rejected_tickers.append(f"{t}({it.get('name','?')})NoFetch")
                continue
            if abs(db_close - true_close) <= tolerance:
                validated[cat].append(it)
            else:
                rejected += 1
                rejected_tickers.append(
                    f"{t}({it.get('name','?')}) DB={db_close} vs Naver={true_close}"
                )
                log.warning(
                    "[validator] REJECT %s(%s) DB=%d Naver=%d diff=%d",
                    t, it.get("name", "?"), db_close, true_close, db_close - true_close,
                )

    stats = {
        "validated": len(tickers) - rejected,
        "rejected": rejected,
        "fetch_failed": fetch_failed,
        "skipped_timeout": skipped_timeout,
        "rejected_samples": rejected_tickers[:10],  # 로그용 sample
    }
    log.info(
        "[validator] 완료 base_date=%s validated=%d rejected=%d fetch_failed=%d timeout=%d "
        "categories_after=%s",
        base_date, stats["validated"], stats["rejected"], stats["fetch_failed"],
        stats["skipped_timeout"],
        {k: len(v) for k, v in validated.items()},
    )
    if rejected_tickers:
        log.warning("[validator] REJECTED samples: %s", rejected_tickers[:10])
    return validated, stats
