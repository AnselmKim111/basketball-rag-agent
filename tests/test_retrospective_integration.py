"""retrospective.signal_returns 통합 — 스케일 브레이크 표본이 집계에서 제외되는지.

임시 SQLite에 정상 종목 + 글리치(분할 조정 불일치) 종목 신호를 넣고,
글리치 종목의 비현실적 수익률이 평균/최고에 섞이지 않음을 검증.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

import pytest

pytest.importorskip("pandas")


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for m in list(sys.modules):
        if m in ("src.screener.db", "src.screener.retrospective", "src.state_store"):
            del sys.modules[m]
    from src.screener import db as fresh_db
    from pathlib import Path
    fresh_db.DB_PATH = Path(tmp_path) / "screener.db"
    fresh_db._INITIALIZED = False
    fresh_db.ensure_schema()
    from src.screener import retrospective as fresh_retro
    return fresh_db, fresh_retro


def _dates(start: date, n: int) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def test_glitch_sample_excluded(fresh):
    db, retro = fresh
    sig_date = date.today() - timedelta(days=8)
    days = _dates(sig_date, 6)  # signal_date .. +5

    # 정상 종목: 10000 → 매일 +3% (5영업일 후 약 +15.9%)
    good = [10000]
    for _ in range(5):
        good.append(int(good[-1] * 1.03))
    # 글리치 종목: 10000 유지하다 3배 점프(분할 조정 불일치) → +98%처럼 보임
    glitch = [10000, 10000, 30000, 30000, 30000, 30000]

    ohlcv = []
    for d, c in zip(days, good):
        ohlcv.append(("GOOD", d, c, c, c, c, 1000, None))
    for d, c in zip(days, glitch):
        ohlcv.append(("GLITCH", d, c, c, c, c, 1000, None))
    db.upsert_ohlcv_bulk(ohlcv)

    db.save_signals(days[0], {"high_all": [
        {"ticker": "GOOD", "name": "정상종목"},
        {"ticker": "GLITCH", "name": "글리치종목"},
    ]})

    out = retro.signal_returns(days_back=10, days_ahead=5, today_iso=date.today().isoformat())
    assert "high_all" in out
    # 글리치 제외 → 정상 1종목만 집계
    assert out["high_all"]["n"] == 1
    assert out["high_all"]["top"][0][0] == "GOOD"
    # 평균이 정상 범위(+15~16%)이고 글리치의 +200%가 섞이지 않음
    assert out["high_all"]["avg_return_pct"] < 30
