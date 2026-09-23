"""high_52w_hi(장중 고가 기준) + new_high_only(소형주 경량 판정) 회귀."""
from __future__ import annotations

import pytest

pytest.importorskip("pandas")

from src.screener import signals


def _rows(n=300, close=10000, high=10100):
    from datetime import date, timedelta
    d0 = date(2026, 1, 1)
    out = []
    for i in range(n):
        out.append({"date": (d0 + timedelta(days=i)).isoformat(), "open": close, "high": high,
                    "low": close - 100, "close": close, "volume": 1000})
    return out


def test_high_52w_hi_fires_on_intraday_high_without_close_high():
    rows = _rows()
    rows[-1] = dict(rows[-1], close=9990, high=10300)    # 종가 미경신(< 과거 종가 10000), 고가 경신
    sig = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "high_52w_hi" in sig and sig["high_52w_hi"]["high"] == 10300
    assert "high_52w" not in sig                          # 미미 정의: 고가 경신만으로 신고가


def test_high_52w_hi_not_when_high_below():
    rows = _rows()
    rows[-1] = dict(rows[-1], close=10050, high=10090)
    sig = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "high_52w_hi" not in sig


def test_new_high_only_smallcap():
    rows = _rows()
    base = rows[-1]["date"]
    assert signals.new_high_only(rows, base) is None            # 경신 없음
    rows[-1] = dict(rows[-1], close=10000, high=10200)          # 고가만 경신
    nh = signals.new_high_only(rows, base)
    assert nh and nh["close"] == 10000 and nh["prev_high"] == 10100
    assert signals.new_high_only(rows[:100], base) is None      # 데이터 부족
