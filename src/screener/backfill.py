"""KR 백필 — `screener_core.backfill` 공통 구현 호출."""
from __future__ import annotations

from src.screener import data_source, db, universe
from src.screener_core.backfill import (
    DATE_BATCH_FAIL_THRESHOLD,
    DEFAULT_DAYS,
    KST,
    SLEEP_BETWEEN_DAYS_S,
    SLEEP_BETWEEN_TICKERS_S,
    make_api,
)

_api = make_api(db=db, data_source=data_source, universe=universe)

run_full_backfill = _api.run_full_backfill

__all__ = [
    "run_full_backfill",
    "KST",
    "DEFAULT_DAYS",
    "SLEEP_BETWEEN_DAYS_S",
    "SLEEP_BETWEEN_TICKERS_S",
    "DATE_BATCH_FAIL_THRESHOLD",
]
