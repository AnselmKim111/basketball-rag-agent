"""신호 종목 cross-validation — KR/US 공통 구현.

배경: 신호 계산이 끝난 후 마지막 한 단계 더 — 신호 종목들의 base_date 종가를
독립적으로 다시 fetch하여 DB 값과 대조. 외부 데이터 소스 lag/캐시/누락으로 인한
잘못된 신호를 메시지 발송 직전 차단.

`make_api(data_source)` closure factory로 시장별 data_source 모듈 주입.
"""
from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace

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


def make_api(data_source) -> SimpleNamespace:
    """data_source 주입 → cross_validate closure 반환."""

    def cross_validate(
        results: dict[str, list[dict]],
        base_date: str,
    ) -> tuple[dict[str, list[dict]], dict]:
        """신호 종목들의 base_date close 값을 독립 fetch로 cross-check.

        반환: (validated_results, validation_stats).
        validated_results는 검증 통과 종목만 유지. 불일치 종목은 모든 카테고리에서 제거.
        """
        timeout_s = _int_env("SCREENER_VALIDATE_TIMEOUT_S", 60)
        tolerance = _int_env("SCREENER_VALIDATE_TOLERANCE", 1)  # ±1원까지 OK (반올림 오차)

        tickers = _all_signal_tickers(results)
        if not tickers:
            return results, {"validated": 0, "rejected": 0, "fetch_failed": 0, "skipped_timeout": 0}

        log.info("[validator] %d종목 cross-validate 시작 (base_date=%s, timeout=%ds)",
                 len(tickers), base_date, timeout_s)

        # ticker → 검증된 close (Naver 응답)
        truth: dict[str, int | None] = {}
        fetch_failed = 0
        skipped_timeout = 0

        t0 = time.monotonic()
        for ticker in sorted(tickers):
            if time.monotonic() - t0 > timeout_s:
                log.warning("[validator] timeout %ds 초과 — %d종목 skip", timeout_s, len(tickers) - len(truth))
                skipped_timeout = len(tickers) - len(truth)
                break
            try:
                from datetime import datetime as _dt, timedelta as _td
                target_dt = _dt.strptime(base_date, "%Y-%m-%d").date()
                start = (target_dt - _td(days=3)).strftime("%Y-%m-%d")
                end = (target_dt + _td(days=1)).strftime("%Y-%m-%d")
                rows = data_source.fetch_ohlcv_by_ticker_via_naver(ticker, start, end)
            except Exception as e:
                log.debug("[validator] %s fetch 실패: %s", ticker, e)
                fetch_failed += 1
                truth[ticker] = None
                continue
            # base_date close 추출
            match = [r for r in rows if r[1] == base_date]
            if match:
                truth[ticker] = int(match[0][5])  # close (index 5)
            else:
                truth[ticker] = None
                fetch_failed += 1

        # 검증: 각 신호 종목의 DB close vs truth close 비교
        # rejected는 unique 종목 수 기준 — 한 종목이 여러 카테고리에서 거부돼도 1로 집계
        # (entry 수로 빼면 validated가 음수가 되거나 과소집계됨)
        validated: dict[str, list[dict]] = {k: [] for k in results.keys()}
        rejected_set: set[str] = set()
        rejected_tickers: list[str] = []
        for cat, items in results.items():
            for it in items:
                t = it.get("ticker")
                db_close = int(it.get("close", 0))
                true_close = truth.get(t)
                if true_close is None:
                    # fetch 실패 — 보수적으로 제외 (잘못된 데이터 방지)
                    if t not in rejected_set:
                        rejected_tickers.append(f"{t}({it.get('name','?')})NoFetch")
                    rejected_set.add(t)
                    continue
                if abs(db_close - true_close) <= tolerance:
                    validated[cat].append(it)
                else:
                    if t not in rejected_set:
                        rejected_tickers.append(
                            f"{t}({it.get('name','?')}) DB={db_close} vs Naver={true_close}"
                        )
                    rejected_set.add(t)
                    log.warning(
                        "[validator] REJECT %s(%s) DB=%d Naver=%d diff=%d",
                        t, it.get("name", "?"), db_close, true_close, db_close - true_close,
                    )

        stats = {
            "validated": len(tickers) - len(rejected_set),
            "rejected": len(rejected_set),
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

    return SimpleNamespace(cross_validate=cross_validate)
