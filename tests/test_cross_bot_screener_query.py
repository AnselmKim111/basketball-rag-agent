"""src/cross_bot/screener_query.py 회귀 테스트.

screener.db 미가용 환경에서도 graceful해야 함 (모든 함수가 빈 결과/None 반환).
실제 DB 채워진 상태 테스트는 screener_bot 자체 테스트 영역.
"""

from __future__ import annotations

import pytest


def test_is_available_returns_bool():
    from src.cross_bot import screener_query
    assert isinstance(screener_query.is_available(), bool)


def test_get_universe_returns_list():
    from src.cross_bot import screener_query
    result = screener_query.get_universe()
    assert isinstance(result, list)


def test_get_universe_filtered_handles_none():
    """필터 모두 None이면 universe 전체와 동일 — 빈 DB면 빈 리스트."""
    from src.cross_bot import screener_query
    full = screener_query.get_universe()
    filtered = screener_query.get_universe_filtered()
    assert len(filtered) == len(full)


def test_get_universe_filtered_market_cap_max():
    """market_cap_max=1조 필터링 — 1조 초과 종목 모두 제외."""
    from src.cross_bot import screener_query
    one_jo = 1_000_000_000_000  # 1조 원
    result = screener_query.get_universe_filtered(market_cap_max_won=one_jo)
    for r in result:
        cap = r.get("market_cap")
        assert cap is None or cap <= one_jo, f"잘못 통과: {r}"


def test_get_universe_filtered_min_and_max():
    """min과 max 동시 적용 (5천억 ~ 1조)."""
    from src.cross_bot import screener_query
    result = screener_query.get_universe_filtered(
        market_cap_min_won=500_000_000_000,
        market_cap_max_won=1_000_000_000_000,
    )
    for r in result:
        cap = r.get("market_cap")
        if cap is None:
            # cap=None은 min/max 둘 다 거부 (필터 활성 시).
            pytest.fail(f"cap=None인데 통과: {r}")
        assert 500_000_000_000 <= cap <= 1_000_000_000_000


def test_get_universe_filtered_sector_substring():
    """sectors=['반도체'] 필터 — 'sector'에 '반도체' 부분 매칭."""
    from src.cross_bot import screener_query
    result = screener_query.get_universe_filtered(sectors=["반도체"])
    for r in result:
        sector = (r.get("sector") or "").lower()
        assert "반도체" in sector, f"sector mismatch: {r}"


def test_get_universe_filtered_market():
    from src.cross_bot import screener_query
    result = screener_query.get_universe_filtered(markets=["KOSDAQ"])
    for r in result:
        mkt = (r.get("market") or "").lower()
        assert "kosdaq" in mkt


def test_get_recent_signals_returns_dict():
    from src.cross_bot import screener_query
    result = screener_query.get_recent_signals(days_back=14)
    assert isinstance(result, dict)


def test_get_signal_summary_empty_ticker():
    """존재하지 않는 ticker → hit_count 0."""
    from src.cross_bot import screener_query
    result = screener_query.get_signal_summary("999999", days_back=14)
    assert result["hit_count"] == 0
    assert result["signals"] == []
    assert result["latest_date"] is None
    assert result["latest_signal"] is None


def test_get_eps_yoy_missing_ticker():
    """없는 ticker → None."""
    from src.cross_bot import screener_query
    assert screener_query.get_eps_yoy("999999") is None


def test_get_ticker_name_missing():
    from src.cross_bot import screener_query
    assert screener_query.get_ticker_name("999999") is None
