"""Phase 1 검증 자산 — backtest 하네스 + breadth 패널 단위 테스트."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for m in list(sys.modules):
        if m.startswith("src.screener") or m == "src.state_store":
            del sys.modules[m]
    from src.screener import db as fresh_db
    fresh_db.DB_PATH = Path(tmp_path) / "screener.db"
    fresh_db._INITIALIZED = False
    fresh_db.ensure_schema()
    return fresh_db


def _cal_dates(n: int, end: date | None = None) -> list[str]:
    end = end or date.today()
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


# ------------------------------------------------------------------
# breadth
# ------------------------------------------------------------------
def test_breadth_accumulator_basic():
    from src.screener.breadth import BreadthAccumulator
    acc = BreadthAccumulator()
    # 상승 추세 종목: 종가 우상향 → MA 위, 52주 신고가, 상승
    up = [{"close": 10000 + i * 10} for i in range(260)]
    acc.add(up)
    # 하락 추세 종목: 우하향 → MA 아래, 신고가 아님, 하락
    down = [{"close": 30000 - i * 10} for i in range(260)]
    acc.add(down)
    b = acc.result()
    assert b["universe"] == 2
    assert b["above_ma200_pct"] == 50.0   # 1/2 위
    assert b["at_high_n"] == 1            # 상승종목만 신고가
    assert b["advancers"] == 1
    assert b["decliners"] == 1


def test_breadth_line_format():
    from src.screener.breadth import format_breadth_line
    b = {"universe": 100, "above_ma200_pct": 18.0, "at_high_n": 0,
         "adv_pct": 41.0, "median_3m": -6.0}
    line = format_breadth_line(b)
    assert "🌡️ 시장 폭" in line
    assert "MA200위 18%" in line
    assert "52주신고 0종목" in line


def test_breadth_empty():
    from src.screener.breadth import format_breadth_line
    assert format_breadth_line({}) == ""


# ------------------------------------------------------------------
# backtest
# ------------------------------------------------------------------
def test_backtest_edge_and_aggregation(fresh):
    db = fresh
    dates = _cal_dates(120)
    # 종목 A: 마지막날 직전까지 평탄하다 base_date들에서 신고가 → 이후 상승(양의 forward)
    # 단순화: 전 구간 매일 +0.3% 상승 (매일 종가 신고가 = high_all 계속 발화), 60행 이상
    rows = []
    base = 10000.0
    for i, d in enumerate(dates):
        c = int(base * (1.003 ** i))
        rows.append(("AAA", d, c, c, c, c, 1000, None))
    db.upsert_ohlcv_bulk(rows)
    db.upsert_tickers([("AAA", "상승종목", "KOSPI", 1, dates[-1], 5_000_000_000_000)])

    from src.screener import backtest
    res = backtest.backtest_signals(lookback=90, sample_every=5, horizons=(5, 10))
    assert res, "결과가 비어있음"
    assert "high_all" in res["categories"]
    cat = res["categories"]["high_all"]
    # 상승 추세라 forward 수익률 양수 + 승률 높음
    assert cat[5]["n"] > 0
    assert cat[5]["avg"] > 0
    assert cat[5]["win"] >= 50
    # baseline도 계산됨
    assert res["baseline_n"][5] > 0


def test_backtest_empty_when_no_data(fresh):
    from src.screener import backtest
    assert backtest.backtest_signals(lookback=120) == {}


def test_format_backtest_renders():
    from src.screener.backtest import format_backtest
    result = {
        "lookback": 120, "sample_every": 3, "eval_dates": 40, "tickers": 700,
        "horizons": [5, 10], "baseline_n": {5: 1000, 10: 900},
        "categories": {"high_all": {
            5: {"n": 50, "win": 60.0, "avg": 2.3, "median": 1.8, "baseline": 0.4, "edge": 1.9},
            10: {"n": 50, "win": 58.0, "avg": 3.1, "median": 2.0, "baseline": 0.6, "edge": 2.5},
        }},
    }
    txt = format_backtest(result)
    assert "역사적 신고가" in txt
    assert "edge" in txt
    assert "+1.9%p" in txt
