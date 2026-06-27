"""KR 증분 업데이트 — `screener_core.incremental` 공통 구현 호출.

매일 16:00 KST cron 호출. 시장별 차이는 의존 모듈(db, data_source, universe)뿐 —
공통 함수는 `src/screener_core/incremental.py`에 단일 출처.
"""
from __future__ import annotations

from src.screener import data_source, db, universe
from src.screener_core.incremental import KST, RETENTION_DAYS, make_api

_api = make_api(db=db, data_source=data_source, universe=universe)

update_today = _api.update_today
update_specific_date = _api.update_specific_date
ensure_recent_business_day_data = _api.ensure_recent_business_day_data

__all__ = [
    "update_today",
    "update_specific_date",
    "ensure_recent_business_day_data",
    "KST",
    "RETENTION_DAYS",
]
