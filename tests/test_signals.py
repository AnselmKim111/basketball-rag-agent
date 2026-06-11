"""signals.compute_signals_for_ticker — base_date-anchored 신호 계산 회귀 테스트.

CLAUDE.md '이중확인 구조 절대 깨지 말 것' 1단계(base_date anchoring)의
불변식을 고정한다:
  - base_date 명시 시 그 날짜 row가 없으면 {} (silent skip)
  - base_date 이후 row는 truncate (미래 데이터 누설 방지)
  - rows < 60이면 {} (신규상장)
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

pytest.importorskip("pandas")

from src.screener import signals


def _make_rows(n: int, close: float = 10_000.0, volume: float = 100_000.0,
               start: str = "2026-01-02") -> list[dict]:
    """평평한 OHLCV n개 (영업일 가정 없이 달력일 증가 — date 문자열만 unique하면 됨)."""
    d0 = date.fromisoformat(start)
    rows = []
    for i in range(n):
        d = (d0 + timedelta(days=i)).isoformat()
        rows.append({"date": d, "open": close, "high": close,
                     "low": close, "close": close, "volume": volume})
    return rows


def test_short_history_returns_empty():
    rows = _make_rows(59)
    assert signals.compute_signals_for_ticker(rows) == {}


def test_missing_base_date_returns_empty():
    rows = _make_rows(100)
    out = signals.compute_signals_for_ticker(rows, base_date="1999-01-01")
    assert out == {}


def test_high_all_triggers_on_new_high():
    rows = _make_rows(100, close=10_000)
    rows[-1] = {**rows[-1], "close": 11_000, "high": 11_000}
    out = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "high_all" in out
    assert out["high_all"]["close"] == 11_000
    assert out["high_all"]["prev_high"] == 10_000
    assert out["high_all"]["chg_pct"] == pytest.approx(10.0)


def test_no_signal_when_flat():
    rows = _make_rows(100)
    out = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "high_all" not in out
    assert "high_52w" not in out
    assert "volume_breakout" not in out


def test_high_52w_requires_252_rows():
    # 251개 → 52주 신고가 미계산, 신고가여도 high_52w 없음
    rows = _make_rows(251, close=10_000)
    rows[-1] = {**rows[-1], "close": 11_000, "high": 11_000}
    out = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "high_52w" not in out
    assert "high_all" in out  # 역사적 신고가는 발생

    rows = _make_rows(252, close=10_000)
    rows[-1] = {**rows[-1], "close": 11_000, "high": 11_000}
    out = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "high_52w" in out


def test_base_date_truncates_future_rows():
    """base_date 이후의 신고가 row가 끼어 있어도 미래 데이터로 신호 만들면 안 됨."""
    rows = _make_rows(100, close=10_000)
    anchor = rows[-2]["date"]  # 마지막에서 두 번째 날을 base로
    rows[-1] = {**rows[-1], "close": 99_999, "high": 99_999}  # 미래의 폭등
    out = signals.compute_signals_for_ticker(rows, base_date=anchor)
    # anchor 날은 평평 — 미래 row가 truncate됐다면 신고가 신호 없음
    assert "high_all" not in out
    assert "high_52w" not in out


def test_volume_breakout_needs_volume_and_price_up():
    rows = _make_rows(100, close=10_000, volume=100_000)
    # 거래량 3배 + 종가 +2%
    rows[-1] = {**rows[-1], "close": 10_200, "high": 10_200, "volume": 300_000}
    out = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "volume_breakout" in out
    assert out["volume_breakout"]["vol_ratio"] == pytest.approx(3.0)

    # 거래량만 늘고 종가 하락이면 신호 없음
    rows[-1] = {**rows[-1], "close": 9_800, "volume": 300_000}
    out = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "volume_breakout" not in out
