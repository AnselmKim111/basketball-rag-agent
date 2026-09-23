"""코스닥 중소형 확장 — 표시 정책 + validator 병렬화 회귀 테스트.

2026-09-23 정책(미미 스타일): 시총 필터(1000억) 이상은 업종별 신고가 한 줄, 미만은
'(소형주/1,000억↓)' 버킷 한 줄. 6개월·VCP·수급·RS는 메시지 미표시.
"""
from __future__ import annotations

import sys
from datetime import datetime, date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")


def _results(**over):
    base = {
        "near_breakout_52w": [], "high_all": [], "high_52w": [], "high_26w": [],
        "vcp_breakout": [], "volume_surge": [], "rs_leaders": [],
    }
    base.update(over)
    return base


def test_smallcap_goes_to_bucket_not_main():
    from src.screener.formatter import format_results, display_items
    results = _results(high_all=[
        {"ticker": "BIG", "name": "대형주", "chg_pct": 3.0, "market_cap": 3.01e11},
        {"ticker": "SMALL", "name": "네오팜", "chg_pct": 5.0, "market_cap": 0.8e11},
    ])
    msg = format_results(results, datetime.now())
    assert "중소형 신호" not in msg
    assert "(기타) 대형주★" in msg
    assert "(소형주/1,000억↓) 네오팜" in msg
    d = display_items(results)
    assert [it["ticker"] for it in d["new_high"]] == ["BIG"]


def test_smallcap_cap_none_stays_in_main():
    from src.screener.formatter import format_results
    results = _results(high_52w=[{"ticker": "X", "name": "미상", "chg_pct": 1.0, "market_cap": None}])
    msg = format_results(results, datetime.now())
    assert "(기타) 미상" in msg


def test_min_cap_default_lowered():
    from src.screener import signals
    assert signals.DEFAULT_MIN_MARKET_CAP == 100_000_000_000
    assert signals.DEFAULT_SMALLCAP_MAX == 300_000_000_000


# ------------------------------------------------------------------
# validator 병렬화 — 순차와 동등한 판정 + NoFetch reject 유지
# ------------------------------------------------------------------
def _naver_row(d: str, close: int) -> list:
    return [None, d, None, None, None, close, None]


BASE = "2026-08-08"
RESULTS = {
    "high_all": [
        {"ticker": "005930", "name": "삼성전자", "close": 80_000, "chg_pct": 2.0},
        {"ticker": "000660", "name": "SK하이닉스", "close": 200_000, "chg_pct": 3.0},
        {"ticker": "035420", "name": "NAVER", "close": 250_000, "chg_pct": 1.0},
    ],
}


def test_parallel_validator_same_verdicts(monkeypatch):
    from src.screener import validator
    truth = {"005930": 80_000, "000660": 195_000, "035420": 250_000}  # 하이닉스 불일치

    def _fake_fetch(ticker, start, end):
        return [_naver_row(BASE, truth[ticker])]

    monkeypatch.setattr(validator.data_source,
                        "fetch_ohlcv_by_ticker_via_naver", _fake_fetch)
    validated, stats = validator.cross_validate(RESULTS, BASE)
    assert {it["ticker"] for it in validated["high_all"]} == {"005930", "035420"}
    assert stats["rejected"] == 1
    assert stats["skipped_timeout"] == 0


def test_parallel_validator_fetch_failure_rejects(monkeypatch):
    from src.screener import validator

    def _fail(ticker, start, end):
        raise RuntimeError("down")

    monkeypatch.setattr(validator.data_source,
                        "fetch_ohlcv_by_ticker_via_naver", _fail)
    validated, stats = validator.cross_validate(RESULTS, BASE)
    assert all(len(v) == 0 for v in validated.values())   # 보수적 전량 제거
    assert stats["fetch_failed"] == 3


# ------------------------------------------------------------------
# compute_all — 1000억대 종목 처리 확인
# ------------------------------------------------------------------
def test_compute_all_includes_1000억(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for m in list(sys.modules):
        if m.startswith("src.screener") or m == "src.state_store":
            del sys.modules[m]
    from src.screener import db
    db.DB_PATH = tmp_path / "screener.db"
    db._INITIALIZED = False
    db.ensure_schema()

    n = 100
    dates = [(date.today() - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
    base = dates[-1]
    closes = [10000] * (n - 1) + [11000]  # 신고가
    db.upsert_ohlcv_bulk([("MID", d, c, c, c, c, 1000, None) for d, c in zip(dates, closes)])
    db.upsert_tickers([("MID", "중형주", "KOSDAQ", 1, base, 150_000_000_000)])  # 1500억

    from src.screener import signals
    results, stats = signals.compute_all(base_date=base)
    assert stats["skipped_cap"] == 0                       # 1500억 통과
    assert any(it["ticker"] == "MID" for it in results.get("high_all", []))
