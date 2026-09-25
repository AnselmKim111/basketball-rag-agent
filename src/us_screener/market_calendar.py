"""NYSE 거래일 캘린더 (규칙 기반, 외부 의존 없음).

용도: 미국 cron이 대상일을 '마감된 거래일'로 정확히 잡고, 휴장일과 데이터 소스 장애를 구분.
예전엔 '대상일 봉이 전 종목 없음'을 휴장으로 추정 → Yahoo 장애일에도 조용히 skip 될 수 있었고,
휴장일엔 재시도 루프가 전 종목 fetch를 6번 반복했다 (2026-09-25 리뷰 지적).

NYSE 정규 휴장일: New Year's Day, MLK Day(1월 셋째 월), Presidents' Day(2월 셋째 월),
Good Friday, Memorial Day(5월 마지막 월), Juneteenth(6/19), Independence Day(7/4),
Labor Day(9월 첫 월), Thanksgiving(11월 넷째 목), Christmas(12/25).
관측일 규칙: 토요일 → 전날 금요일(단 1/1이 토요일이면 대체휴장 없음), 일요일 → 다음 월요일.
임시 휴장(국장 등)은 규칙으로 알 수 없음 → US_MARKET_EXTRA_HOLIDAYS="YYYY-MM-DD,..." env.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from functools import lru_cache


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date, allow_saturday_shift: bool = True) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1) if allow_saturday_shift else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=32)
def nyse_holidays(year: int) -> frozenset:
    out = set()
    ny = _observed(date(year, 1, 1), allow_saturday_shift=False)
    if ny:
        out.add(ny)
    out.add(_nth_weekday(year, 1, 0, 3))        # MLK
    out.add(_nth_weekday(year, 2, 0, 3))        # Presidents
    out.add(_easter(year) - timedelta(days=2))  # Good Friday
    out.add(_last_weekday(year, 5, 0))          # Memorial
    if year >= 2022:
        out.add(_observed(date(year, 6, 19)))   # Juneteenth
    out.add(_observed(date(year, 7, 4)))        # Independence
    out.add(_nth_weekday(year, 9, 0, 1))        # Labor
    out.add(_nth_weekday(year, 11, 3, 4))       # Thanksgiving
    out.add(_observed(date(year, 12, 25)))      # Christmas
    # 다음 해 1/1이 토요일이면 관측 없음(NYSE 규칙) — 올해 12/31은 거래일
    return frozenset(x for x in out if x and x.year == year)


def _extra_holidays() -> set[date]:
    out = set()
    for tok in (os.getenv("US_MARKET_EXTRA_HOLIDAYS", "") or "").split(","):
        tok = tok.strip()
        if tok:
            try:
                out.add(date.fromisoformat(tok))
            except ValueError:
                pass
    return out


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year) and d not in _extra_holidays()


def prev_trading_day(d: date) -> date:
    """d 이전(미포함) 가장 가까운 거래일."""
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def last_trading_day_on_or_before(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_between(start: date, end: date) -> list[date]:
    """start~end(포함) 거래일 목록."""
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out
