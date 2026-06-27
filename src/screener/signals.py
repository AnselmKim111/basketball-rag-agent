"""KR 시장 신호 계산 — `screener_core.signals`의 시장 비의존 함수를 호출.

핵심 신호 로직은 `src/screener_core/signals.py`에 단일 출처로 존재.
이 모듈은 KR 특화 부분(시총 필터 단위·DB 접근·로깅 단위)만 담당.
"""
from __future__ import annotations

import logging

from src.screener import db, universe  # noqa: F401  (universe는 호출자 호환)
from src.screener_core.signals import (
    CATEGORIES,
    _get_float_env,
    compute_signals_for_ticker,
    composite_score,
)

# Re-export — 기존 호출자(`screener_bot` 등)가 `from src.screener import signals`로 쓰면서
# `signals.CATEGORIES` / `signals.compute_signals_for_ticker`도 접근 가능해야 함.
__all__ = [
    "CATEGORIES",
    "compute_signals_for_ticker",
    "composite_score",
    "compute_all",
    "DEFAULT_MIN_MARKET_CAP",
]

log = logging.getLogger(__name__)

# 시가총액 필터 (원). 기본 3000억 — 사용자 요구사항.
DEFAULT_MIN_MARKET_CAP = 300_000_000_000


def compute_all(base_date: str | None = None) -> tuple[dict[str, list[dict]], dict]:
    """전 종목 신호 계산 → 카테고리별 시총·상승률 복합 정렬.

    base_date: "YYYY-MM-DD" — 모든 종목이 이 날짜의 close를 today로 사용.
               None이면 db.latest_date() 자동 사용.

    반환: (results, stats) 튜플
      results: {category_key: [...]}
      stats: {"base_date": str, "processed": int, "skipped_cap": int,
              "skipped_no_base": int, "total_active": int}

    구조 변경 (잘못된 신호 영구 방지):
      - 모든 신호가 base_date의 close 기준 — 종목별 "today" 다른 날짜 가능성 차단
      - base_date 데이터 없는 종목 = silent skip + skipped_no_base 카운트
      - stats에 검증 정보 포함 → 사용자에게 명시적 표기
    """
    db.ensure_schema()
    tickers = db.get_active_tickers()
    if not tickers:
        log.warning("[signals] universe 비어있음")
        return {}, {"base_date": base_date, "processed": 0, "skipped_cap": 0,
                    "skipped_no_base": 0, "total_active": 0}

    if base_date is None:
        base_date = db.latest_date()
        log.info("[signals] base_date 자동 결정: %s", base_date)
    else:
        log.info("[signals] base_date 명시: %s", base_date)

    min_cap = _get_float_env("SCREENER_MIN_MARKET_CAP", DEFAULT_MIN_MARKET_CAP)

    by_cat: dict[str, list[dict]] = {k: [] for k in CATEGORIES}

    processed = 0
    skipped_cap = 0
    skipped_no_base = 0
    max_cap_seen = 0
    for tinfo in tickers:
        ticker = tinfo["ticker"]
        cap = tinfo.get("market_cap")
        if cap is not None and cap < min_cap:
            skipped_cap += 1
            continue
        rows = db.load_ohlcv(ticker, days=1300)
        if len(rows) < 60:
            continue
        has_base = any(r["date"] == base_date for r in rows)
        if not has_base:
            skipped_no_base += 1
            continue
        try:
            sigs = compute_signals_for_ticker(rows, base_date=base_date)
        except Exception:
            log.exception("[signals] %s 계산 실패", ticker)
            continue
        if cap and cap > max_cap_seen:
            max_cap_seen = cap
        for cat_key, payload in sigs.items():
            if cat_key not in by_cat:
                continue
            entry = {
                "ticker": ticker,
                "name": tinfo.get("name") or ticker,
                "market": tinfo.get("market") or "",
                "market_cap": cap,
                "sector": tinfo.get("sector") or "",
                **payload,
            }
            by_cat[cat_key].append(entry)
        processed += 1

    for cat, items in by_cat.items():
        items.sort(key=lambda it: composite_score(it, max_cap_seen))

    stats = {
        "base_date": base_date,
        "processed": processed,
        "skipped_cap": skipped_cap,
        "skipped_no_base": skipped_no_base,
        "total_active": len(tickers),
    }
    log.info(
        "[signals] base_date=%s processed=%d skipped_cap=%d skipped_no_base=%d "
        "(min=%.1f억) categories=%s",
        base_date, processed, skipped_cap, skipped_no_base, min_cap / 1e8,
        {k: len(v) for k, v in by_cat.items()},
    )
    return by_cat, stats
