"""us_screener.earnings_proximity — ⚡ 어닝 임박 마커 단위 테스트.

graceful degrade(키 없음/fetch 실패 → 빈 dict)가 핵심 계약 — 깨지면
스크리너 메시지 발송 자체가 막힐 수 있음.
"""
from __future__ import annotations

import pytest

from src.us_screener import earnings_proximity as ep


def test_empty_tickers_returns_empty(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "dummy")
    assert ep.fetch_upcoming_earnings(set(), days_fwd=7) == {}


def test_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    assert ep.fetch_upcoming_earnings({"NVDA"}, days_fwd=7) == {}


def test_calendar_failure_returns_empty(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "dummy")

    def _boom(*a, **kw):
        raise RuntimeError("FMP down")

    monkeypatch.setattr(ep, "_calendar", _boom)
    assert ep.fetch_upcoming_earnings({"NVDA"}, days_fwd=7) == {}


def test_filters_to_requested_tickers_and_earliest_date(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "dummy")
    rows = [
        {"symbol": "NVDA", "date": "2026-06-15"},
        {"symbol": "NVDA", "date": "2026-06-12"},   # 더 빠른 날짜 → 이게 선택돼야 함
        {"symbol": "AAPL", "date": "2026-06-14"},
        {"symbol": "TSLA", "date": "2026-06-13"},   # 요청 안 한 티커 → 제외
        {"symbol": "", "date": "2026-06-13"},        # 빈 심볼 → 제외
        {"symbol": "MSFT", "date": None},            # date 없음 → 제외
    ]
    monkeypatch.setattr(ep, "_calendar", lambda *a, **kw: rows)
    out = ep.fetch_upcoming_earnings({"NVDA", "AAPL", "MSFT"}, days_fwd=7)
    assert out == {"NVDA": "2026-06-12", "AAPL": "2026-06-14"}


def test_case_insensitive_ticker_match(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "dummy")
    rows = [{"symbol": "nvda", "date": "2026-06-12"}]
    monkeypatch.setattr(ep, "_calendar", lambda *a, **kw: rows)
    out = ep.fetch_upcoming_earnings({"NVDA"}, days_fwd=7)
    assert out == {"NVDA": "2026-06-12"}
