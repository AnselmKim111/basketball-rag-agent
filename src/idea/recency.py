"""리포트 발행일 → 가중치·라벨 — narrow/synthesis LLM 입력 가공용.

리포트 sch_dt(발행일) 기준:
  - 7일 이내: ×1.5 (가장 최신)
  - 30일 이내: ×1.2
  - 그 외:   ×1.0

가중치 정렬·필터링뿐 아니라 LLM 프롬프트에 "발행일 X일 전, 가중치 1.5×" 라벨로
주입해 분석가의 가장 최근 의견을 우대시킬 수 있게 한다.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Iterable, Optional

KST = timezone(timedelta(hours=9))


def _parse_date(s: str) -> Optional[datetime]:
    """sch_dt 흔한 포맷들을 datetime(KST)로. 실패 시 None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def days_old(sch_dt: str, *, today: datetime | None = None) -> Optional[int]:
    """sch_dt에서 today까지 경과 일수. parse 실패 시 None."""
    dt = _parse_date(sch_dt)
    if dt is None:
        return None
    today = today or datetime.now(KST)
    delta = today - dt
    return max(delta.days, 0)


def weight(sch_dt: str, *, today: datetime | None = None) -> float:
    """발행일 → 가중치. parse 실패 시 1.0 (중립)."""
    age = days_old(sch_dt, today=today)
    if age is None:
        return 1.0
    if age <= 7:
        return 1.5
    if age <= 30:
        return 1.2
    return 1.0


def label(sch_dt: str, *, today: datetime | None = None) -> str:
    """LLM 프롬프트 주입용 라벨. 예: `[발행 5일 전 · 가중치 1.5×]`.

    parse 실패 시 `[발행일 미상]`.
    """
    age = days_old(sch_dt, today=today)
    if age is None:
        return "[발행일 미상]"
    w = weight(sch_dt, today=today)
    return f"[발행 {age}일 전 · 가중치 {w:.1f}×]"


def sort_by_weight(items: Iterable[dict], *, date_key: str = "sch_dt") -> list[dict]:
    """리포트 dict 리스트를 가중치 desc → 그 후 sch_dt desc로 정렬.

    같은 가중치(예: 둘 다 30일 내) 안에서는 더 최신이 앞.
    """
    def _key(it: dict):
        s = (it.get(date_key) or "").strip()
        return (weight(s), s)
    return sorted(items, key=_key, reverse=True)
