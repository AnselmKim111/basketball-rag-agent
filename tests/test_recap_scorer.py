"""src/recap/scorer.py 단위 테스트.

LLM 호출 없는 순수 산술 → mocking 없이 즉시 검증.
"""

from __future__ import annotations

import pytest

from src.recap.scorer import (
    SignalStats,
    TickerHit,
    compute_signal_stats,
    overall_summary,
)


def _hit(ticker: str, signal: str, hit_close: float, horizon_close: float) -> TickerHit:
    return TickerHit(
        ticker=ticker, signal=signal,
        hit_date="2026-05-26", hit_close=hit_close,
        horizon_close=horizon_close, horizon_date="2026-06-02",
    )


def test_pnl_pct_basic():
    h = _hit("005930", "high_52w", 100.0, 110.0)
    assert h.pnl_pct == pytest.approx(10.0)
    h2 = _hit("000660", "volume_breakout", 200.0, 180.0)
    assert h2.pnl_pct == pytest.approx(-10.0)


def test_pnl_pct_graceful():
    """hit_close 0 / horizon 0 → None (NaN/division zero 방지)."""
    assert _hit("X", "high_all", 0.0, 50.0).pnl_pct is None
    assert _hit("X", "high_all", 50.0, 0.0).pnl_pct is None


def test_compute_signal_stats_grouping_and_topbottom():
    hits = [
        _hit("A", "high_52w", 100, 115),  # +15%
        _hit("B", "high_52w", 100, 90),   # -10%
        _hit("C", "high_52w", 100, 105),  # +5%
        _hit("D", "high_52w", 100, 95),   # -5%
        _hit("E", "volume_breakout", 100, 108),  # +8%
        _hit("F", "volume_breakout", 100, 92),   # -8%
    ]
    out = compute_signal_stats(hits, top_n=2)
    by_sig = {s.signal: s for s in out}

    h52 = by_sig["high_52w"]
    assert h52.hit_count == 4
    assert h52.win_rate == 0.5  # 2/4
    assert h52.avg_pnl == pytest.approx((15 - 10 + 5 - 5) / 4)
    # top 2 by pnl desc: A(+15), C(+5)
    assert [t.ticker for t in h52.top] == ["A", "C"]
    # bottom 2 by pnl asc: B(-10), D(-5)
    assert [t.ticker for t in h52.bottom] == ["B", "D"]

    vb = by_sig["volume_breakout"]
    assert vb.hit_count == 2
    assert vb.win_rate == 0.5
    assert vb.avg_pnl == pytest.approx(0.0)


def test_compute_signal_stats_empty_and_unscored():
    """빈 hits, 또는 모든 pnl None인 hits → SignalStats(hit_count, 나머지 0)."""
    assert compute_signal_stats([]) == []
    # 모두 hit_close=0 → pnl_pct None
    bad = [_hit("X", "vcp_breakout", 0, 100), _hit("Y", "vcp_breakout", 0, 50)]
    out = compute_signal_stats(bad)
    assert len(out) == 1
    s = out[0]
    assert s.signal == "vcp_breakout"
    assert s.hit_count == 2
    assert s.avg_pnl == 0.0
    assert s.win_rate == 0.0
    assert s.top == [] and s.bottom == []


def test_overall_summary_picks_best_worst():
    stats = [
        SignalStats(signal="high_52w", hit_count=4, avg_pnl=1.25, win_rate=0.5),
        SignalStats(signal="volume_breakout", hit_count=2, avg_pnl=-2.0, win_rate=0.5),
        SignalStats(signal="vcp_breakout", hit_count=0, avg_pnl=0.0, win_rate=0.0),
    ]
    out = overall_summary(stats)
    assert out["total_hits"] == 6
    assert out["best_signal"] == "high_52w"
    assert out["worst_signal"] == "volume_breakout"


def test_overall_summary_empty():
    out = overall_summary([])
    assert out["total_hits"] == 0
    assert out["best_signal"] == ""
