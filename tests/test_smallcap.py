"""코스닥 중소형 확장 — 표시 정책 + validator 병렬화 회귀 테스트.

2026-09-23 정책: 🧩 중소형 섹션 제거. 중소형(1000억~3000억)은 신고가 계열 핵심
3섹션(🎯🚀📈)에만 시총 표기로 합류, 나머지(📊💎🔥💪)에선 제외.
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


def test_no_smallcap_section_and_core_sections_tag_cap():
    from src.screener.formatter import format_results
    results = _results(high_all=[
        {"ticker": "BIG", "name": "대형주", "chg_pct": 3.0, "market_cap": 3.01e11},
        {"ticker": "SMALL", "name": "네오팜", "chg_pct": 5.0, "market_cap": 2.15e11},
    ])
    msg = format_results(results, datetime.now())
    assert "중소형 신호" not in msg              # 🧩 섹션 제거
    assert "역사적 신고가 (2)" in msg            # 중소형도 핵심 섹션에 합류
    assert "네오팜(2,150억)" in msg              # 시총 표기로 구분
    assert "대형주 /" in msg                     # 대형은 표기 없음


def test_smallcap_dropped_from_noise_sections():
    from src.screener.formatter import format_results, display_items
    small = {"ticker": "S1", "name": "아이디피", "chg_pct": 8.0, "market_cap": 1.5e11}
    results = _results(volume_surge=[dict(small)], rs_leaders=[dict(small)],
                       vcp_breakout=[dict(small)], high_26w=[dict(small)])
    msg = format_results(results, datetime.now())
    assert "아이디피" not in msg
    assert all(len(v) == 0 for v in display_items(results).values())


def test_smallcap_in_core_shown_once_with_dedup():
    from src.screener.formatter import format_results
    it = {"ticker": "S1", "name": "아이디피", "chg_pct": 8.0, "market_cap": 1.5e11}
    results = _results(high_all=[dict(it)], high_52w=[dict(it)], volume_surge=[dict(it)])
    msg = format_results(results, datetime.now())
    assert msg.count("아이디피") == 1            # 🚀가 먼저 claim, 📈·🔥엔 중복 없음


def test_smallcap_cap_none_stays_in_main_sections():
    from src.screener.formatter import format_results
    results = _results(high_52w=[{"ticker": "X", "name": "미상", "chg_pct": 1.0, "market_cap": None}])
    msg = format_results(results, datetime.now())
    assert "52주 신고가 (1)" in msg
    assert "미상 /" in msg


def test_eps_column_hidden_when_all_na():
    from src.screener.formatter import format_results
    results = _results(high_52w=[{"ticker": "A", "name": "에이", "chg_pct": 1.0, "market_cap": 5e11}])
    msg = format_results(results, datetime.now(), extra={"A": {"ytd": 12.0, "eps_yoy": None}})
    assert "(종목 / 당일 / 연초대비)" in msg
    assert "EPS" not in msg
    msg2 = format_results(results, datetime.now(), extra={"A": {"ytd": 12.0, "eps_yoy": 30.0}})
    assert "EPS YoY" in msg2 and "+30.0%" in msg2


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
