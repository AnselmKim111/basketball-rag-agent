"""/diag 지원 db 헬퍼 + compute_all skipped_short 카운터 회귀 테스트."""
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
        if m in ("src.screener.db", "src.screener.signals",
                 "src.screener.universe", "src.state_store"):
            del sys.modules[m]
    from src.screener import db as fresh_db
    fresh_db.DB_PATH = Path(tmp_path) / "screener.db"
    fresh_db._INITIALIZED = False
    fresh_db.ensure_schema()
    return fresh_db


def test_get_ticker_row_and_search(fresh):
    db = fresh
    db.upsert_tickers([
        ("005930", "삼성전자", "KOSPI", 1, "2026-06-19", 400_000_000_000_000),
        ("347850", "디앤디파마텍", "KOSDAQ", 1, "2026-06-19", 3_000_000_000_000),
    ])
    row = db.get_ticker_row("347850")
    assert row is not None
    assert row["name"] == "디앤디파마텍"
    assert row["is_active"] == 1
    assert row["market_cap"] == 3_000_000_000_000

    assert db.get_ticker_row("999999") is None

    hits = db.search_tickers_by_name("디앤디")
    assert ("347850", "디앤디파마텍") in hits


def test_compute_all_counts_skipped_short(fresh):
    db = fresh
    base = date.today().isoformat()
    db.upsert_tickers([("000001", "롱종목", "KOSPI", 1, base, 5_000_000_000_000),
                       ("000002", "숏종목", "KOSPI", 1, base, 5_000_000_000_000)])
    # 롱종목: 70행 (>=60), 숏종목: 30행 (<60 → skipped_short)
    d0 = date.today() - timedelta(days=80)
    long_rows = [("000001", (d0 + timedelta(days=i)).isoformat(),
                  10000, 10000, 10000, 10000, 1000, None) for i in range(70)]
    short_rows = [("000002", (d0 + timedelta(days=i)).isoformat(),
                   10000, 10000, 10000, 10000, 1000, None) for i in range(30)]
    # base_date row 보장: 마지막 날을 today로
    long_rows[-1] = ("000001", base, 11000, 11000, 11000, 11000, 1000, None)
    db.upsert_ohlcv_bulk(long_rows + short_rows)

    from src.screener import signals
    _, stats = signals.compute_all(base_date=base)
    assert stats["skipped_short"] >= 1
