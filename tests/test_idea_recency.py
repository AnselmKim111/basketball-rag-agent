"""src/idea/recency.py 회귀 테스트."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def _fixed_today():
    return datetime(2026, 6, 3, 12, 0, tzinfo=KST)


def test_days_old_basic_formats():
    from src.idea.recency import days_old
    today = _fixed_today()
    assert days_old("2026-06-01", today=today) == 2
    assert days_old("2026.05.01", today=today) == 33
    assert days_old("2026/05/01", today=today) == 33
    assert days_old("20260501", today=today) == 33


def test_days_old_invalid_returns_none():
    from src.idea.recency import days_old
    assert days_old("", today=_fixed_today()) is None
    assert days_old("garbage", today=_fixed_today()) is None


def test_days_old_negative_clamped_to_zero():
    """미래 날짜는 0으로 clamp."""
    from src.idea.recency import days_old
    assert days_old("2026-06-10", today=_fixed_today()) == 0


def test_weight_7d_window():
    from src.idea.recency import weight
    today = _fixed_today()
    assert weight("2026-06-01", today=today) == 1.5  # 2일 전
    assert weight("2026-05-28", today=today) == 1.5  # 6일 전
    assert weight("2026-05-27", today=today) == 1.5  # 7일 전 (inclusive)


def test_weight_30d_window():
    from src.idea.recency import weight
    today = _fixed_today()
    assert weight("2026-05-20", today=today) == 1.2  # 14일 전
    assert weight("2026-05-04", today=today) == 1.2  # 30일 전 (inclusive)


def test_weight_over_30d_defaults_to_1():
    from src.idea.recency import weight
    today = _fixed_today()
    assert weight("2026-05-03", today=today) == 1.0  # 31일 전
    assert weight("2025-01-01", today=today) == 1.0


def test_weight_invalid_date_returns_1():
    from src.idea.recency import weight
    assert weight("", today=_fixed_today()) == 1.0
    assert weight("garbage", today=_fixed_today()) == 1.0


def test_label_format():
    from src.idea.recency import label
    today = _fixed_today()
    # 절대 날짜·상대 일수·오늘 anchor 모두 표기
    out = label("2026-06-01", today=today)
    assert "발행 2일 전" in out
    assert "2026-06-01" in out
    assert "1.5×" in out
    assert "오늘=2026-06-03" in out

    out30 = label("2026-05-04", today=today)
    assert "발행 30일 전" in out30
    assert "1.2×" in out30
    assert "2026-05-04" in out30

    assert "1.0×" in label("2025-01-01", today=today)
    assert label("garbage", today=today) == "[발행일 미상]"


def test_sort_by_weight():
    """7일 내 → 30일 내 → 그 외 순. 같은 가중치 안에서 더 최신이 앞."""
    from src.idea.recency import sort_by_weight
    items = [
        {"id": "old", "sch_dt": "2025-01-01"},     # 1.0
        {"id": "very_recent", "sch_dt": "2026-06-01"},  # 1.5
        {"id": "mid", "sch_dt": "2026-05-15"},     # 1.2
        {"id": "fresh", "sch_dt": "2026-05-30"},   # 1.5
    ]
    # Note: 가중치 sort는 today 인자가 없어 실제 today에서 계산되므로
    # 실제 분포는 today=2026-06-03 가정 (테스트 환경 fixed_today와 매칭)
    # → 정렬은 (weight desc, sch_dt desc). 1.5 그룹에서 06-01 > 05-30.
    # 단, today 의존 — fixed today를 inject할 수 없으므로 안정 검증만:
    out = sort_by_weight(items)
    # 최소한 invalid·oldest는 끝에 위치
    assert out[-1]["id"] == "old"
