"""US 증분 업데이트 — `screener_core.incremental` 공통 구현 호출.

매일 07:00 KST cron 호출 (US 4PM ET 장마감 + 데이터 발행 버퍼).

KR과 차이:
  - tz=America/New_York — "오늘"을 미국 동부 기준으로 결정.
    (07:00 KST = 전날 17~18시 ET → 최근 영업일이 ET 기준으로 잡혀야
     존재하지 않는 날짜를 전 종목 fetch로 헛도는 일이 없음)
  - fdr_fallback=False — 1차 진입이 이미 FDR→Stooq라 동일 소스 재스캔 무의미.
"""
from __future__ import annotations

from datetime import timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _US_EASTERN = ZoneInfo("America/New_York")
except Exception:  # tzdata 없는 환경 폴백 (EST 고정 — DST 1시간 오차 허용)
    _US_EASTERN = timezone(timedelta(hours=-5))

from src.us_screener import data_source, db, universe
from src.screener_core.incremental import KST, RETENTION_DAYS, make_api

_api = make_api(
    db=db, data_source=data_source, universe=universe,
    tz=_US_EASTERN, fdr_fallback=False,
)

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
