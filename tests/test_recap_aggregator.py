"""src/recap/aggregator.py 단위 테스트.

DB·wisereport·idea_cache 호출은 monkeypatch로 격리 — 실제 외부 호출 없음.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.recap import aggregator as ag


def test_horizon_close_basic():
    ohlcv = [
        {"date": "2026-05-26", "close": 100.0},
        {"date": "2026-05-27", "close": 102.0},
        {"date": "2026-05-28", "close": 104.0},
        {"date": "2026-05-29", "close": 106.0},
        {"date": "2026-06-01", "close": 108.0},  # +3영업일
        {"date": "2026-06-02", "close": 110.0},  # +4영업일
    ]
    close, dstr = ag._horizon_close(ohlcv, "2026-05-26", horizon_days=3)
    assert close == 106.0
    assert dstr == "2026-05-29"


def test_horizon_close_beyond_clamped_to_last():
    """horizon이 데이터 끝 너머면 마지막 close (graceful)."""
    ohlcv = [
        {"date": "2026-06-01", "close": 100.0},
        {"date": "2026-06-02", "close": 105.0},
    ]
    close, dstr = ag._horizon_close(ohlcv, "2026-06-01", horizon_days=5)
    assert close == 105.0
    assert dstr == "2026-06-02"


def test_horizon_close_missing_hit_date():
    ohlcv = [{"date": "2026-06-01", "close": 100.0}]
    close, dstr = ag._horizon_close(ohlcv, "2026-05-01", horizon_days=5)
    assert close == 0.0
    assert dstr == ""


def test_collect_signal_section_empty(monkeypatch):
    """signals 비어있을 때 안내 dict + scorer summary 0."""
    from src.screener import db

    monkeypatch.setattr(db, "load_signals_in_range", lambda s, e: [])
    out = ag.collect_signal_section(date(2026, 6, 3))
    assert out["stats"] == []
    assert out["summary"]["total_hits"] == 0
    assert "empty_reason" in out


def test_collect_signal_section_with_data(monkeypatch):
    """가상 signals + ohlcv → scorer 결과 정상 통합."""
    from src.screener import db

    fake_signals = [
        {"date": "2026-05-26", "ticker": "AAA", "signal": "high_52w",
         "payload": {"close": 100.0}},
        {"date": "2026-05-26", "ticker": "BBB", "signal": "high_52w",
         "payload": {"close": 50.0}},
        {"date": "2026-05-27", "ticker": "CCC", "signal": "volume_breakout",
         "payload": {"close": 200.0}},
    ]
    fake_ohlcv = {
        "AAA": [
            {"date": "2026-05-26", "close": 100.0},
            {"date": "2026-05-27", "close": 102.0},
            {"date": "2026-05-28", "close": 105.0},
            {"date": "2026-05-29", "close": 108.0},
            {"date": "2026-06-01", "close": 110.0},
            {"date": "2026-06-02", "close": 115.0},
        ],
        "BBB": [
            {"date": "2026-05-26", "close": 50.0},
            {"date": "2026-05-27", "close": 49.0},
            {"date": "2026-05-28", "close": 47.0},
            {"date": "2026-05-29", "close": 46.0},
            {"date": "2026-06-01", "close": 45.0},
            {"date": "2026-06-02", "close": 44.0},
        ],
        "CCC": [
            {"date": "2026-05-27", "close": 200.0},
            {"date": "2026-05-28", "close": 205.0},
            {"date": "2026-05-29", "close": 210.0},
            {"date": "2026-06-01", "close": 215.0},
            {"date": "2026-06-02", "close": 220.0},
        ],
    }

    monkeypatch.setattr(db, "load_signals_in_range", lambda s, e: fake_signals)
    monkeypatch.setattr(db, "load_ohlcv", lambda t, days=260: fake_ohlcv.get(t, []))

    # tickers 매핑은 conn 컨텍스트 — 빈 list로 우회
    class _NoTickerConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **kw):
            class _Cur:
                def fetchall(self_inner): return []
            return _Cur()
    monkeypatch.setattr(db, "_conn", lambda: _NoTickerConn())

    out = ag.collect_signal_section(date(2026, 6, 3), lookback_days=10, horizon_days=5)
    assert len(out["stats"]) == 2  # high_52w, volume_breakout
    by_sig = {s.signal: s for s in out["stats"]}
    h52 = by_sig["high_52w"]
    assert h52.hit_count == 2
    # AAA +15%, BBB -12% → avg 1.5%
    assert h52.avg_pnl == pytest.approx(1.5, abs=0.01)
    assert h52.win_rate == 0.5


def test_collect_ideas_section(monkeypatch):
    """idea_cache.find_recent_in_range 결과를 picks 메타로 변환."""
    from src import idea_cache

    fake_entries = [
        {
            "id": "20260530-100000",
            "created_at": "2026-05-30T10:00:00",
            "idea_text": "AI 데이터센터 cap-ex 사이클",
            "synthesis": {
                "top5": [
                    {"name": "솔브레인", "ticker6": "357780", "thesis": "HBM 식각 소재 핵심"},
                    {"name": "원익IPS", "ticker6": "240810", "thesis": "전공정 식각·증착"},
                ]
            },
        },
    ]
    monkeypatch.setattr(idea_cache, "find_recent_in_range", lambda days_back=7: fake_entries)
    out = ag.collect_ideas_section(days_back=7)
    assert out["entry_count"] == 1
    assert out["entries"][0]["picks"][0]["name"] == "솔브레인"
    assert out["entries"][0]["picks"][0]["ticker"] == "357780"


def test_collect_ideas_section_empty(monkeypatch):
    from src import idea_cache
    monkeypatch.setattr(idea_cache, "find_recent_in_range", lambda days_back=7: [])
    out = ag.collect_ideas_section()
    assert out["entry_count"] == 0


def test_disclosure_section_placeholder():
    """현재는 빈 dict + 안내."""
    out = ag.collect_disclosure_section()
    assert out["logs"] == []
    assert "empty_reason" in out


def test_extract_industry_hits_basic():
    """제목에서 산업 alias 매칭 (industry_catalog 재사용)."""
    hits = ag._extract_industry_hits("반도체 소부장 전망 2026")
    assert "반도체 소재·부품·장비" in hits

    hits2 = ag._extract_industry_hits("2차전지 양극재 사이클")
    # 2차전지·배터리 또는 2차전지 소재 둘 다 alias 매칭 가능 — 적어도 하나
    assert any("2차전지" in h for h in hits2)


def test_build_recap_input_skip_themes(monkeypatch):
    """skip_themes=True → wisereport 호출 없이 통합 dict 반환."""
    from src.screener import db
    from src import idea_cache

    monkeypatch.setattr(db, "load_signals_in_range", lambda s, e: [])
    monkeypatch.setattr(idea_cache, "find_recent_in_range", lambda days_back=7: [])

    out = ag.build_recap_input(date(2026, 6, 3), lookback_days=7, skip_themes=True)
    assert set(out.keys()) >= {"today", "lookback_days", "signal", "ideas", "disclosures", "themes"}
    assert out["themes"]["skipped"] is True
    assert out["today"] == "2026-06-03"
