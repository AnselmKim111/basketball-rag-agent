"""Phase 2 — 유니버스 상대강도(RS) + near_breakout 확장 게이트 + 섹션 재구성 테스트.

백테스트 검증(2026-06) 기반: 52주 돌파 직전(edge 최고) 전면 배치, RS 리더 섹션,
확장 구간(90~95%)은 RS rank 게이트.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")


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


def _dates(n: int) -> list[str]:
    d0 = date.today() - timedelta(days=n - 1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _insert(db, ticker: str, closes: list[float], dates: list[str],
            volumes: list[float] | None = None, highs: list[float] | None = None):
    rows = []
    for i, (d, c) in enumerate(zip(dates, closes)):
        v = volumes[i] if volumes else 1000
        h = highs[i] if highs else c
        rows.append((ticker, d, int(c), int(h), int(c), int(c), int(v), None))
    db.upsert_ohlcv_bulk(rows)


def _mk_universe(db, base: str, tickers: list[str]):
    db.upsert_tickers([(t, f"종목{t}", "KOSPI", 1, base, 5_000_000_000_000)
                       for t in tickers])


# ------------------------------------------------------------------
# RS rank + 리더 선정
# ------------------------------------------------------------------
def test_rs_rank_and_leaders(fresh):
    db = fresh
    n = 300
    dates = _dates(n)
    base = dates[-1]
    # 리더: +50% 3M 추세 상승 (MA50 위, 신고가 근접)
    up = [10000 * (1.007 ** i) for i in range(n)]
    # 중립: 평탄
    flat = [10000.0] * n
    # 낙폭과대: 고점 대비 -40% (RS 하위)
    down = [30000 * (0.995 ** i) for i in range(n)]
    _insert(db, "UPUP", up, dates)
    _insert(db, "FLAT", flat, dates)
    _insert(db, "DOWN", down, dates)
    _mk_universe(db, base, ["UPUP", "FLAT", "DOWN"])

    from src.screener import signals
    results, stats = signals.compute_all(base_date=base)

    leaders = results.get("rs_leaders") or []
    leader_tickers = {it["ticker"] for it in leaders}
    assert "UPUP" in leader_tickers          # 상승 리더 포착
    assert "DOWN" not in leader_tickers      # 하락 종목 제외
    assert "FLAT" not in leader_tickers      # rank 중위 + 조건 미달
    lead = next(it for it in leaders if it["ticker"] == "UPUP")
    assert lead["rs_3m_rank"] == 100.0       # 3종목 중 최상위
    assert lead["r3m_pct"] > 30


def test_rs_rank_attached_to_signal_entries(fresh):
    db = fresh
    n = 300
    dates = _dates(n)
    base = dates[-1]
    up = [10000 * (1.007 ** i) for i in range(n)]     # 매일 신고가 → high_all 발화
    flat = [10000.0] * n
    _insert(db, "UPUP", up, dates)
    _insert(db, "FLAT", flat, dates)
    _mk_universe(db, base, ["UPUP", "FLAT"])

    from src.screener import signals
    results, _ = signals.compute_all(base_date=base)
    ha = results.get("high_all") or []
    assert ha, "high_all 발화해야 함"
    assert ha[0]["rs_3m_rank"] is not None


# ------------------------------------------------------------------
# near_breakout 확장 구간 RS 게이트
# ------------------------------------------------------------------
def _near_breakout_closes(n: int, proximity: float) -> tuple[list, list, list]:
    """52주 윈도 안 고점(spike) → 조정 → 현재 proximity 수준 + 최근 5일 거래량 급증."""
    hi = 20000.0
    spike_i = n - 70  # 52주 윈도(-252:-1) 안에 위치해야 hi52로 잡힘
    closes = []
    for i in range(n):
        if i == spike_i:
            closes.append(hi)
        elif i < spike_i:
            closes.append(hi * 0.85)
        else:
            closes.append(hi * proximity)
    volumes = [1000.0] * n
    for i in range(n - 5, n):
        volumes[i] = 2000.0   # vol_trend = 2.0 >= 1.3
    return closes, volumes, closes  # highs=closes


def test_near_breakout_extension_gated_by_rs(fresh):
    """proximity 92%(확장 구간) 종목: RS 하위면 제거, 코어(96%)는 무조건 유지."""
    db = fresh
    n = 300
    dates = _dates(n)
    base = dates[-1]

    # 확장 구간(92%) 종목 — 유니버스 내 RS 하위로 만들기 위해 3M 하락 배치
    c92, v92, h92 = _near_breakout_closes(n, 0.92)
    _insert(db, "EXT92", c92, dates, v92, h92)
    # 코어 구간(96%) 종목
    c96, v96, h96 = _near_breakout_closes(n, 0.96)
    _insert(db, "CORE96", c96, dates, v96, h96)
    # RS 상위권을 채울 강한 상승 종목 3개 (EXT92를 rank 하위로 밀어냄)
    for i, t in enumerate(["STRONG1", "STRONG2", "STRONG3"]):
        up = [10000 * (1.008 ** k) for k in range(n)]
        _insert(db, t, up, dates)
    _mk_universe(db, base, ["EXT92", "CORE96", "STRONG1", "STRONG2", "STRONG3"])

    from src.screener import signals
    results, _ = signals.compute_all(base_date=base)
    nb = {it["ticker"] for it in results.get("near_breakout_52w") or []}
    assert "CORE96" in nb, "코어 구간(95~99%)은 RS 무관 유지"
    # EXT92는 3M 수익률이 STRONG들보다 낮아 rank < 60 → 게이트 제거
    assert "EXT92" not in nb, "확장 구간 + RS 하위 → 게이트로 제거"


# ------------------------------------------------------------------
# composite score RS 항
# ------------------------------------------------------------------
def test_composite_score_rs_term():
    from src.screener.signals import _composite_score
    hi_rs = {"chg_pct": 5.0, "market_cap": 1e12, "rs_3m_rank": 95.0}
    lo_rs = {"chg_pct": 5.0, "market_cap": 1e12, "rs_3m_rank": 10.0}
    assert _composite_score(hi_rs, 1e13) < _composite_score(lo_rs, 1e13)  # 음수 정렬키


# ------------------------------------------------------------------
# formatter — 섹션 순서 + 💪 렌더
# ------------------------------------------------------------------
def test_formatter_section_order_and_rs_leaders():
    from datetime import datetime
    from src.screener.formatter import format_results
    results = {
        "near_breakout_52w": [{"ticker": "000001", "name": "돌파직전", "chg_pct": 1.0}],
        "high_all": [{"ticker": "000002", "name": "신고가", "chg_pct": 2.0}],
        "high_52w": [],
        "vcp_breakout": [],
        "rs_leaders": [{"ticker": "000003", "name": "리더", "chg_pct": 0.5,
                        "rs_3m_rank": 97.0, "r3m_pct": 22.0}],
    }
    msg = format_results(results, datetime.now(), base_date="2026-06-27")
    i_nb = msg.index("52주 돌파 직전")
    i_ha = msg.index("역사적 신고가")
    i_rs = msg.index("상대강도 리더")
    assert i_nb < i_ha < i_rs               # 🎯 맨 앞, 💪 맨 뒤
    assert "리더" in msg


def test_formatter_dedup_rs_leaders_after_breakout():
    """돌파 섹션에 이미 나온 종목은 💪에서 제외 (공용 seen)."""
    from datetime import datetime
    from src.screener.formatter import format_results
    dup = {"ticker": "000001", "name": "중복종목", "chg_pct": 3.0}
    results = {
        "near_breakout_52w": [],
        "high_all": [dup],
        "high_52w": [],
        "vcp_breakout": [],
        "rs_leaders": [dict(dup, rs_3m_rank=99.0)],
    }
    msg = format_results(results, datetime.now())
    assert msg.count("중복종목") == 1       # 한 번만 (🚀 섹션에서)
    assert "상대강도 리더 (시장 대비 상위 10%) (0)" in msg
