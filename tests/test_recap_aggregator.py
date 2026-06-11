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

    monkeypatch.setattr(db, "load_signals_in_range", lambda s, e, exclude_date=None: [])
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

    monkeypatch.setattr(db, "load_signals_in_range",
                        lambda s, e, exclude_date=None: fake_signals)
    monkeypatch.setattr(db, "load_ohlcv", lambda t, days=260: fake_ohlcv.get(t, []))
    # ticker name — get_ticker_name (전 conn 직접 접근 제거)
    monkeypatch.setattr(db, "get_ticker_name", lambda t: "")
    # horizon close — fake_ohlcv 인덱스 +n 위치 close
    def _fake_close_after(t: str, start: str, n: int = 5):
        rows = fake_ohlcv.get(t, [])
        dates = [r["date"] for r in rows]
        if start not in dates:
            return None
        idx = dates.index(start)
        target = idx + n
        if target >= len(rows):
            return None
        return int(rows[target]["close"])
    monkeypatch.setattr(db, "close_after_n_business_days", _fake_close_after)

    out = ag.collect_signal_section(date(2026, 6, 3), lookback_days=10, horizon_days=5)
    assert len(out["stats"]) == 2  # high_52w, volume_breakout
    by_sig = {s.signal: s for s in out["stats"]}
    h52 = by_sig["high_52w"]
    assert h52.hit_count == 2
    # AAA 100→115 = +15%, BBB 50→44 = -12% → avg 1.5%
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


def test_disclosure_section_empty(monkeypatch):
    """disclosure_log 비어있으면 빈 logs + 안내 (첫 주 케이스)."""
    from src.screener import db
    monkeypatch.setattr(db, "load_disclosures_in_range",
                        lambda s, e, chat_id=None: [])
    out = ag.collect_disclosure_section(today=date(2026, 6, 7))
    assert out["logs"] == []
    assert "empty_reason" in out


def test_disclosure_section_with_pnl(monkeypatch):
    """공시 로그 + baseline → pnl_since_alert 계산."""
    from src.screener import db
    fake_logs = [
        {"rcept_no": "20260601000123", "chat_id": "1", "ticker": "005930",
         "name": "삼성전자", "report_nm": "잠정실적공시", "category": "critical",
         "alert_date": "2026-06-01", "baseline_close": 100_000},
        {"rcept_no": "20260602000456", "chat_id": "1", "ticker": "999999",
         "name": "비유니버스", "report_nm": "일반공시", "category": "normal",
         "alert_date": "2026-06-02", "baseline_close": None},  # baseline 없음
    ]
    fake_latest = {"005930": [{"date": "2026-06-07", "close": 110_000}]}
    monkeypatch.setattr(db, "load_disclosures_in_range",
                        lambda s, e, chat_id=None: fake_logs)
    monkeypatch.setattr(db, "load_ohlcv",
                        lambda t, days=1: fake_latest.get(t, []))
    out = ag.collect_disclosure_section(today=date(2026, 6, 7))
    assert out["log_count"] == 2
    samsung = next(l for l in out["logs"] if l["ticker"] == "005930")
    assert samsung["pnl_since_alert"] == pytest.approx(10.0)
    no_base = next(l for l in out["logs"] if l["ticker"] == "999999")
    assert no_base["pnl_since_alert"] is None


def test_signal_section_ticker_filter(monkeypatch):
    """/recap_me — ticker_filter 밖 hit는 제외."""
    from src.screener import db
    fake_signals = [
        {"date": "2026-06-02", "ticker": "AAA", "signal": "high_52w",
         "payload": {"close": 100.0}},
        {"date": "2026-06-02", "ticker": "BBB", "signal": "high_52w",
         "payload": {"close": 50.0}},
    ]
    monkeypatch.setattr(db, "load_signals_in_range",
                        lambda s, e, exclude_date=None: fake_signals)
    monkeypatch.setattr(db, "get_ticker_name", lambda t: "")
    monkeypatch.setattr(db, "close_after_n_business_days",
                        lambda t, start, n=5: 110 if t == "AAA" else None)
    out = ag.collect_signal_section(
        date(2026, 6, 7), ticker_filter={"AAA"},
    )
    assert len(out["stats"]) == 1
    assert out["stats"][0].hit_count == 1
    # 전부 필터 밖 → watchlist 안내
    out2 = ag.collect_signal_section(date(2026, 6, 7), ticker_filter={"ZZZ"})
    assert out2["stats"] == []
    assert "watchlist" in out2["empty_reason"]


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
