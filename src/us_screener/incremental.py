"""US 증분 업데이트 — `screener_core.incremental` 공통 구현 호출.

매일 07:00 KST cron 호출 (US 4PM ET 장마감 + 데이터 발행 버퍼).
"""
from __future__ import annotations

from src.us_screener import data_source, db, universe
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
