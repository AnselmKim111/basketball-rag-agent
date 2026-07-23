"""회복·수급 신호 테스트 — high_26w(6개월 신고가) + volume_surge(강한 거래량 돌파).

GS건설류(52주 고점에선 멀지만 6개월 박스권을 거래량 실고 돌파하는 회복 국면 종목)
포함 요구에 대한 회귀 고정.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")

from src.screener import signals


def _make_rows(closes: list[float], volumes: list[float] | None = None,
               start: str = "2025-06-01") -> list[dict]:
    d0 = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        d = (d0 + timedelta(days=i)).isoformat()
        v = volumes[i] if volumes else 1000
        out.append({"date": d, "open": c, "high": c, "low": c, "close": c, "volume": v})
    return out


# ------------------------------------------------------------------
# high_26w — per-ticker 신호
# ------------------------------------------------------------------
def test_high_26w_fires_on_recovery():
    """GS건설 시나리오: 52주 고점(20000)에서 -35% 하락 후 6개월 박스(12000~13000)를
    돌파(13500) — 52주 계열 미발화, high_26w만 발화."""
    closes = [20000.0] * 60          # 옛 고점 구간
    closes += [12500.0] * 180        # 장기 하락 후 6개월 박스
    closes += [13500.0]              # 오늘: 6개월 박스 돌파 (52주 고점 20000엔 한참 못 미침)
    rows = _make_rows(closes)
    out = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "high_26w" in out
    assert out["high_26w"]["prev_high"] == 12500
    assert "high_52w" not in out     # 52주 고점(20000) 미달
    assert "high_all" not in out


def test_high_26w_skips_short_history():
    closes = [10000.0] * 120 + [11000.0]   # 121행 < 127
    rows = _make_rows(closes)
    out = signals.compute_signals_for_ticker(rows, base_date=rows[-1]["date"])
    assert "high_26w" not in out


def test_high_26w_no_future_leak():
    closes = [10000.0] * 200
    rows = _make_rows(closes)
    anchor = rows[-2]["date"]
    rows[-1] = {**rows[-1], "close": 99999, "high": 99999}  # 미래 폭등
    out = signals.compute_signals_for_ticker(rows, base_date=anchor)
    assert "high_26w" not in out


# ------------------------------------------------------------------
# volume_surge — compute_all 파생 게이트
# ------------------------------------------------------------------
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


def _surge_closes(n: int, chg_pct: float, vol_mult: float):
    closes = [10000.0] * (n - 1) + [10000.0 * (1 + chg_pct / 100)]
    volumes = [1000.0] * (n - 1) + [1000.0 * vol_mult]
    return closes, volumes


def test_volume_surge_gate(fresh):
    db = fresh
    n = 200
    dates = [(date.today() - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
    base = dates[-1]

    def _ins(t, closes, volumes):
        db.upsert_ohlcv_bulk([(t, d, int(c), int(c), int(c), int(c), int(v), None)
                              for d, c, v in zip(dates, closes, volumes)])

    # STRONG: 거래량 5배 + 종가 +8% → surge 통과 기대 (상승이라 RS 상위)
    c, v = _surge_closes(n, 8.0, 5.0)
    _ins("STRONG", c, v)
    # WEAK: 거래량 2.2배 + 종가 +1% → volume_breakout은 발화하나 surge 게이트 탈락
    c, v = _surge_closes(n, 1.0, 2.2)
    _ins("WEAK", c, v)
    # RS 하위를 채울 하락 종목 2개
    for t in ("DOWN1", "DOWN2"):
        closes = [20000 * (0.997 ** i) for i in range(n)]
        _ins(t, closes, [1000.0] * n)

    db.upsert_tickers([(t, t, "KOSPI", 1, base, 5_000_000_000_000)
                       for t in ("STRONG", "WEAK", "DOWN1", "DOWN2")])

    from src.screener import signals as sig
    results, _ = sig.compute_all(base_date=base)

    vb = {it["ticker"] for it in results.get("volume_breakout") or []}
    vs = {it["ticker"] for it in results.get("volume_surge") or []}
    assert "STRONG" in vb and "WEAK" in vb       # 원신호는 둘 다
    assert "STRONG" in vs                         # 강한 것만 surge
    assert "WEAK" not in vs


# ------------------------------------------------------------------
# formatter — 새 섹션 + dedup + 차트 계약
# ------------------------------------------------------------------
def test_formatter_new_sections_and_dedup():
    from datetime import datetime
    from src.screener.formatter import format_results
    dup = {"ticker": "000001", "name": "중복52주", "chg_pct": 5.0}
    results = {
        "near_breakout_52w": [], "high_all": [], "vcp_breakout": [], "rs_leaders": [],
        "high_52w": [dup],
        "high_26w": [dup, {"ticker": "000002", "name": "회복주", "chg_pct": 4.0}],
        "volume_surge": [{"ticker": "000003", "name": "수급주", "chg_pct": 9.0, "vol_ratio": 4.2}],
    }
    msg = format_results(results, datetime.now())
    assert "6개월 신고가 (회복 국면) (1)" in msg   # dup은 52주 섹션이 claim → 1개만
    assert "회복주" in msg
    assert "수급 유입" in msg and "수급주" in msg
    assert msg.count("중복52주") == 1


def test_display_categories_include_new(fresh):
    import src.screener_bot as kr_bot
    import src.us_screener_bot as us_bot
    for cat in ("high_26w", "volume_surge"):
        assert cat in kr_bot.DISPLAY_CATEGORIES
        assert cat in us_bot.DISPLAY_CATEGORIES
    assert kr_bot.DISPLAY_CATEGORIES == us_bot.DISPLAY_CATEGORIES
