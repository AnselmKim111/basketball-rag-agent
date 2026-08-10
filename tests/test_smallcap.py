"""코스닥 중소형 확장 — 시총 분리 섹션 + validator 병렬화 회귀 테스트."""
from __future__ import annotations

import sys
from datetime import datetime, date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")


# ------------------------------------------------------------------
# formatter — 3000억 경계 분리 + 시총 표기 + 신호 뱃지
# ------------------------------------------------------------------
def _results(**over):
    base = {
        "near_breakout_52w": [], "high_all": [], "high_52w": [], "high_26w": [],
        "vcp_breakout": [], "volume_surge": [], "rs_leaders": [],
    }
    base.update(over)
    return base


def test_smallcap_split_boundary():
    from src.screener.formatter import format_results
    results = _results(high_all=[
        {"ticker": "BIG", "name": "대형주", "chg_pct": 3.0, "market_cap": 3.01e11},
        {"ticker": "SMALL", "name": "네오팜", "chg_pct": 5.0, "market_cap": 2.15e11},
    ])
    msg = format_results(results, datetime.now())
    # 대형은 기존 🚀 섹션, 중소형은 🧩 섹션
    assert "역사적 신고가 (1)" in msg
    assert "중소형 신호 (1000억~3000억) (1)" in msg
    assert "네오팜(2,150억)" in msg          # 시총 표기
    assert "/ 🚀" in msg                     # 발화 신호 뱃지


def test_smallcap_multi_signal_emojis_merged():
    from src.screener.formatter import format_results
    it = {"ticker": "S1", "name": "아이디피", "chg_pct": 8.0, "market_cap": 1.5e11}
    results = _results(high_all=[dict(it)], high_52w=[dict(it)], volume_surge=[dict(it)])
    msg = format_results(results, datetime.now())
    assert msg.count("아이디피") == 1        # 복수 신호도 1줄
    assert "🚀📈🔥" in msg                   # 이모지 병기


def test_smallcap_cap_none_stays_in_main_sections():
    # 시총 미상(None)은 필터 불가 → 기존 섹션 유지 (숨기지 않음)
    from src.screener.formatter import format_results
    results = _results(high_52w=[{"ticker": "X", "name": "미상", "chg_pct": 1.0, "market_cap": None}])
    msg = format_results(results, datetime.now())
    assert "52주 신고가 (1)" in msg
    assert "중소형 신호 (1000억~3000억) (0)" in msg


def test_header_says_1000억():
    from src.screener.formatter import format_results
    msg = format_results(_results(), datetime.now(), stats={"processed": 1500})
    assert "시총 1000억+" in msg


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
