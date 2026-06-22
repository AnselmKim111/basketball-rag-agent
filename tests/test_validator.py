"""validator.cross_validate — 이중확인 2단계(독립 fetch cross-check) 회귀 테스트.

불변식:
  - DB close == 독립 fetch close (±tolerance) → 통과
  - 불일치 → 그 종목 모든 카테고리에서 제거 (rejected)
  - fetch 실패 → 보수적으로 제거 (잘못된 데이터보다 누락이 낫다)
"""
from __future__ import annotations

import pytest

from src.screener import validator


def _naver_row(d: str, close: int) -> list:
    # data_source.fetch_ohlcv_by_ticker_via_naver의 row 형태:
    # index 1 = date, index 5 = close (validator가 이 두 인덱스만 사용)
    return [None, d, None, None, None, close, None]


BASE = "2026-06-10"

RESULTS = {
    "high_all": [
        {"ticker": "005930", "name": "삼성전자", "close": 80_000, "chg_pct": 2.0},
        {"ticker": "000660", "name": "SK하이닉스", "close": 200_000, "chg_pct": 3.0},
    ],
    "high_52w": [
        {"ticker": "005930", "name": "삼성전자", "close": 80_000, "chg_pct": 2.0},
        {"ticker": "035420", "name": "NAVER", "close": 250_000, "chg_pct": 1.0},
    ],
}


def test_mismatch_rejected_from_all_categories(monkeypatch):
    truth = {
        "005930": 80_000,    # 일치 → 통과
        "000660": 195_000,   # 불일치 (DB 200,000) → reject
        "035420": 250_000,   # 일치 → 통과
    }

    def _fake_fetch(ticker, start, end):
        return [_naver_row(BASE, truth[ticker])]

    monkeypatch.setattr(validator.data_source,
                        "fetch_ohlcv_by_ticker_via_naver", _fake_fetch)
    validated, stats = validator.cross_validate(RESULTS, BASE)

    tickers_high_all = {it["ticker"] for it in validated["high_all"]}
    assert tickers_high_all == {"005930"}          # 하이닉스 reject
    tickers_52w = {it["ticker"] for it in validated["high_52w"]}
    assert tickers_52w == {"005930", "035420"}
    assert stats["rejected"] == 1


def test_fetch_failure_conservatively_rejected(monkeypatch):
    def _fail_fetch(ticker, start, end):
        raise RuntimeError("Naver down")

    monkeypatch.setattr(validator.data_source,
                        "fetch_ohlcv_by_ticker_via_naver", _fail_fetch)
    validated, stats = validator.cross_validate(RESULTS, BASE)
    # 전부 fetch 실패 → 전부 제거 (잘못된 chg_pct 발송 방지)
    assert all(len(v) == 0 for v in validated.values())
    assert stats["fetch_failed"] == 3


def test_tolerance_allows_rounding_diff(monkeypatch):
    def _fake_fetch(ticker, start, end):
        # DB 값과 1원 차이 — 기본 tolerance ±1원이면 통과
        db_close = {"005930": 80_000, "000660": 200_000, "035420": 250_000}[ticker]
        return [_naver_row(BASE, db_close + 1)]

    monkeypatch.setattr(validator.data_source,
                        "fetch_ohlcv_by_ticker_via_naver", _fake_fetch)
    validated, stats = validator.cross_validate(RESULTS, BASE)
    assert stats["rejected"] == 0
    assert {it["ticker"] for it in validated["high_all"]} == {"005930", "000660"}


def test_empty_results_passthrough():
    validated, stats = validator.cross_validate({}, BASE)
    assert validated == {}
    assert stats["rejected"] == 0
