"""가격 스케일 브레이크 가드 — YTD/회고의 +244%·+98.7% 글리치 차단 회귀 테스트.

2026-06-16 국장 스크리너가 보험·백화점 가치주에 연초대비 +244%, 회고 '최고
+98.7%(5영업일)' 같은 물리적으로 불가능한 값을 표시 → 과거 종가 스케일 브레이크
(분할/조정 불일치) 미검증이 원인. 한국 ±30% 일일제한 위반을 탐지해 N/A 처리.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

pytest.importorskip("pandas")

from src.screener import fundamentals as kr_f
from src.us_screener import fundamentals as us_f
from src.screener_common.sanity import (
    KR_DAILY_HI,
    KR_DAILY_LO,
    US_DAILY_HI,
    US_DAILY_LO,
    series_is_continuous,
)


# ------------------------------------------------------------------
# series_is_continuous
# ------------------------------------------------------------------
def test_continuous_within_kr_limit():
    # 매일 +5% — ±30% 안, 통과
    closes = [10000]
    for _ in range(10):
        closes.append(int(closes[-1] * 1.05))
    assert series_is_continuous(closes, KR_DAILY_LO, KR_DAILY_HI)


def test_scale_break_detected_kr():
    # 중간에 3배 점프(분할 조정 불일치) → 차단
    closes = [10000, 10100, 30300, 30200, 30500]
    assert not series_is_continuous(closes, KR_DAILY_LO, KR_DAILY_HI)


def test_zero_close_is_break():
    assert not series_is_continuous([10000, 0, 10000], KR_DAILY_LO, KR_DAILY_HI)


def test_single_element_passes():
    assert series_is_continuous([10000], KR_DAILY_LO, KR_DAILY_HI)


def test_us_allows_large_legit_move():
    # 미국은 +60% 하루도 정상(어닝 서프라이즈 등) → 통과
    assert series_is_continuous([100, 160, 170], US_DAILY_LO, US_DAILY_HI)
    # 단 10배 점프(분할급)는 차단
    assert not series_is_continuous([100, 1000, 1010], US_DAILY_LO, US_DAILY_HI)


# ------------------------------------------------------------------
# ytd_pct 통합 — 스크린샷 시나리오
# ------------------------------------------------------------------
def _rows(closes: list[int], start="2026-01-02") -> list[dict]:
    d0 = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        d = (d0 + timedelta(days=i)).isoformat()
        out.append({"date": d, "open": c, "high": c, "low": c, "close": c, "volume": 1000})
    return out


def test_kr_ytd_suppressed_on_scale_break():
    # 연초 4000 → 중간 분할 글리치로 14000 점프 → +244%처럼 보이던 케이스
    closes = [4000] * 5 + [14000] * 5   # 4000→14000 = +250% 단일점프 (불가능)
    out = kr_f.ytd_pct(_rows(closes))
    assert out is None  # N/A 처리


def test_kr_ytd_normal_passes():
    # 연초 10000 → 점진 상승 12000 (+20%), 일일 변동 ±30% 안 → 정상
    closes = [10000, 10500, 11000, 11200, 11500, 11800, 12000]
    out = kr_f.ytd_pct(_rows(closes))
    assert out == pytest.approx(20.0)


def test_us_ytd_allows_big_move():
    # 미국 100 → 180 점진(+80%), 일일 최대 +30%대 → 통과
    closes = [100, 125, 150, 165, 175, 180]
    out = us_f.ytd_pct(_rows(closes))
    assert out == pytest.approx(80.0)


def test_us_ytd_suppressed_on_split_glitch():
    closes = [100] * 3 + [1500] * 3   # 15배 점프 → 분할 글리치
    out = us_f.ytd_pct(_rows(closes))
    assert out is None
