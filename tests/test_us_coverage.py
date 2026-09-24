"""미국 스크리너 커버리지 회귀 — 2026-09-25 "458종목 base_date 누락" 사고.

사고 원인과 고정하는 불변식:
  1. Yahoo 빈 봉(NaN) 한 행 → 파서 ValueError → 종목 전체 유실  ⇒ 행 단위 스킵
  2. 빈 봉 날짜는 Nasdaq으로 보충, DB 보유 날짜는 재요청 안 함
  3. 대상일을 KST로 잡아 미국 미개장일 검색 ⇒ 미국 동부시간 기준 마감 거래일
  4. 유니버스 = S&P500+NDX100(516, 시총 없음) ⇒ 미국 보통주 시총≥$1B 전체(Nasdaq screener)
  5. 우선주·채권·워런트·CEF는 발행사 시총이 붙어 와도 제외, ADR·클래스주·MLP는 포함
  6. 백필은 이력 부족 종목만, 시도 기록으로 매일 재트리거 방지
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("pandas")

from src.us_screener import data_source as ds


# ------------------------------------------------------------------
# 1) 행 단위 결측 처리
# ------------------------------------------------------------------
def test_cents_row_rejects_nan_none_and_nonpositive():
    nan = float("nan")
    assert ds._cents_row("A", "2026-09-22", nan, nan, nan, nan, nan) is None
    assert ds._cents_row("A", "2026-09-22", None, 1, 1, 1, 1) is None
    assert ds._cents_row("A", "2026-09-22", 1, 1, 1, 0, 1) is None
    assert ds._cents_row("A", "2026-09-23", 166.54, 168.94, 163.27, 165.32, 3238300) == (
        "A", "2026-09-23", 16654, 16894, 16327, 16532, 3238300, None)


def test_fdr_parser_keeps_other_rows_when_one_row_is_nan(monkeypatch):
    import pandas as pd
    nan = float("nan")
    df = pd.DataFrame(
        {"Open": [161.0, nan, 166.54], "High": [162.9, nan, 168.94], "Low": [160.0, nan, 163.27],
         "Close": [161.94, nan, 165.32], "Volume": [1e6, nan, 3238300.0]},
        index=pd.to_datetime(["2026-09-21", "2026-09-22", "2026-09-23"]))

    class FakeFdr:
        @staticmethod
        def DataReader(sym, s, e):
            return df
    monkeypatch.setattr(ds, "_import_fdr", lambda: FakeFdr)
    rows = ds.fetch_ohlcv_by_ticker_via_fdr("A", "2026-09-15", "2026-09-25")
    assert [r[1] for r in rows] == ["2026-09-21", "2026-09-23"]      # 예전엔 [] (종목 전체 유실)


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _yahoo_payload():
    # 2026-09-21~24, 09-22는 빈 봉 (실측 Yahoo 응답 형태). EDT gmtoffset -14400.
    ts = [1789997400, 1790083800, 1790170200, 1790256600]
    return {"chart": {"result": [{
        "meta": {"gmtoffset": -14400},
        "timestamp": ts,
        "indicators": {"quote": [{
            "open": [160.0, None, 166.54, 163.95], "high": [162.89, None, 168.94, 174.77],
            "low": [159.0, None, 163.27, 163.62], "close": [161.94, None, 165.32, 172.84],
            "volume": [1000, None, 3238300, 3028845]}]},
    }]}}


def test_yahoo_chart_reports_gap_and_uses_exchange_local_date(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(_yahoo_payload()))
    rows, gaps = ds._yahoo_chart("A", "2026-09-15", "2026-09-25")
    assert [r[1] for r in rows] == ["2026-09-21", "2026-09-23", "2026-09-24"]
    assert gaps == ["2026-09-22"]
    assert rows[-1][5] == 17284


def test_gap_filled_from_nasdaq_only_for_dates_not_in_db(monkeypatch):
    calls = []
    monkeypatch.setattr(ds, "_yahoo_chart", lambda t, s, e: (
        [("A", "2026-09-21", 1, 1, 1, 16194, 1, None), ("A", "2026-09-23", 1, 1, 1, 16532, 1, None)],
        ["2026-09-22"]))

    def fake_nasdaq(t, s, e):
        calls.append((s, e))
        return [("A", "2026-09-22", 1, 1, 1, 16726, 1, None)]
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_nasdaq", fake_nasdaq)

    rows = ds.fetch_ohlcv_by_ticker_via_naver("A", "2026-09-15", "2026-09-25")
    assert [r[1] for r in rows] == ["2026-09-21", "2026-09-22", "2026-09-23"]
    assert calls == [("2026-09-22", "2026-09-22")]

    calls.clear()
    rows = ds.fetch_ohlcv_by_ticker_via_naver("A", "2026-09-15", "2026-09-25",
                                              known_dates={"2026-09-22"})
    assert calls == []                                        # DB에 있으면 재요청 안 함
    assert [r[1] for r in rows] == ["2026-09-21", "2026-09-23"]


def test_chain_survives_source_exceptions(monkeypatch):
    monkeypatch.setattr(ds, "_yahoo_chart", lambda t, s, e: ([], []))

    def boom(*a):
        raise ValueError("cannot convert float NaN to integer")
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_fdr", boom)
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_stooq", lambda *a: [])
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_nasdaq",
                        lambda *a: [("A", "2026-09-24", 1, 1, 1, 17284, 1, None)])
    assert ds.fetch_ohlcv_by_ticker_via_naver("A", "2026-09-15", "2026-09-25")[0][5] == 17284


def test_nasdaq_history_widens_single_day_range(monkeypatch):
    import requests
    seen = {}

    def fake_get(url, params=None, **k):
        seen.update(params)
        return _Resp({"data": {"tradesTable": {"rows": [
            {"date": "09/23/2026", "open": "$166.54", "high": "$168.94", "low": "$163.27",
             "close": "$165.32", "volume": "3,238,300"},
            {"date": "09/22/2026", "open": "$165.00", "high": "$167.59", "low": "$164.10",
             "close": "$167.26", "volume": "2,000,000"},
            {"date": "09/21/2026", "open": "$160.00", "high": "$162.89", "low": "$159.00",
             "close": "$161.94", "volume": "1,000,000"}]}}})
    monkeypatch.setattr(requests, "get", fake_get)
    rows = ds.fetch_ohlcv_by_ticker_via_nasdaq("BRKB", "2026-09-22", "2026-09-22")
    assert seen["fromdate"] == "2026-09-21" and seen["todate"] == "2026-09-23"
    assert [(r[1], r[5]) for r in rows] == [("2026-09-22", 16726)]
    assert ds._nasdaq_symbol("BRKB") == "BRK.B" and ds._yahoo_symbol("BRKB") == "BRK-B"


# ------------------------------------------------------------------
# 3) 대상일 = 미국 마감 거래일
# ------------------------------------------------------------------
@pytest.mark.parametrize("et,expected", [
    ("2026-09-24 18:00", "2026-09-24"),   # 목 장마감 후 (= 금 07:00 KST cron)
    ("2026-09-25 10:00", "2026-09-24"),   # 금 장중 → 목
    ("2026-09-26 12:00", "2026-09-25"),   # 토 → 금
    ("2026-09-28 09:00", "2026-09-25"),   # 월 개장 전 → 금
    ("2026-09-28 16:45", "2026-09-28"),   # 월 마감 후
])
def test_us_target_date(et, expected):
    from src.us_screener import incremental
    now = datetime.strptime(et, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=-4)))
    assert incremental.us_target_date(now).isoformat() == expected


# ------------------------------------------------------------------
# 4·5) 유니버스·보통주 필터·시총 상속
# ------------------------------------------------------------------
@pytest.mark.parametrize("sym,name,industry,ok", [
    ("WBD", "Warner Bros. Discovery Inc. Series A Common Stock", "", True),
    ("PFBC", "Preferred Bank Common Stock", "Major Banks", True),
    ("ITUB", "Itau Unibanco Banco Holding SA American Depositary Shares (Each repstg 500 Preferred shares)", "", True),
    ("AMX", "America Movil S.A.B. de C.V. American Depositary Shares (each representing the right to receive twenty (20) Series B Shares", "", True),
    ("AB", "AllianceBernstein Holding L.P.  Units", "", True),
    ("BRK/B", "Berkshire Hathaway Inc.", "", True),
    ("STWD", "STARWOOD PROPERTY TRUST INC. Starwood Property Trust Inc.", "Real Estate Investment Trusts", True),
    ("ACGLN", "Arch Capital Group Ltd. Depositary Shares each Representing a 1/1000th Interest in a 4.550% Non-Cumulative Preferred", "", False),
    ("AFGB", "American Financial Group Inc. 5.875% Subordinated Debentures due 2059", "", False),
    ("STRK", "Strategy Inc 8.00% Series A Perpetual Strike Preferred Stock", "", False),
    ("CDZIP", "Cadiz Inc. Depositary Shares", "Water Supply", False),
    ("BULLW", "Webull Corporation Warrants", "", False),
    ("GENVR", "Gen Digital Inc. Contingent Value Rights", "", False),
    ("PPLC", "PPL Corporation Corporate Units", "", False),
    ("CCZ", "Comcast Holdings ZONES", "", False),
    ("BDJ", "Blackrock Enhanced Equity Dividend Trust", "Finance Companies", False),
    ("PDI", "PIMCO Dynamic Income Fund Common Stock", "Trusts Except Educational Religious and Charitable", False),
    ("BFH^A", "Bread Financial Holdings Inc. Depositary Shares", "", False),
])
def test_is_common_equity(sym, name, industry, ok):
    assert ds.is_common_equity(name, sym, industry) is ok


def _screener_rows():
    return [
        {"symbol": "HEI", "name": "Heico Corporation Common Stock", "marketCap": "43404457271.00",
         "sector": "Industrials", "industry": "Aerospace"},
        {"symbol": "HEI/A", "name": "Heico Corporation", "marketCap": "", "sector": "", "industry": ""},
        {"symbol": "BF/B", "name": "Brown Forman Corporation", "marketCap": "", "sector": "", "industry": ""},
        {"symbol": "BF/A", "name": "Brown Forman Corporation", "marketCap": "", "sector": "", "industry": ""},
        {"symbol": "TINY", "name": "Tiny Inc. Common Stock", "marketCap": "500000000.00", "sector": "", "industry": ""},
        {"symbol": "ADAML", "name": "Adamas Trust Inc. 6.875% Series F Preferred Stock",
         "marketCap": "2000000000.00", "sector": "", "industry": ""},
    ]


def test_universe_cap_inheritance_and_member_siblings(monkeypatch):
    monkeypatch.setattr(ds, "_nasdaq_screener_rows", lambda max_age_s=900: _screener_rows())
    u = {x["ticker"]: x for x in ds.fetch_nasdaq_universe(1e9, always_include={"BFB"})}
    assert set(u) == {"HEI", "HEI.A", "BFB", "BF.A"}         # TINY(<$1B)·우선주 제외
    assert u["HEI.A"]["market_cap"] == 43404457271            # 클래스주 시총 상속
    assert ds.fetch_market_caps()["HEI.A"] == 43404457271


# ------------------------------------------------------------------
# DB 의존 — 비활성화·백필 대상·유니버스 폴백
# ------------------------------------------------------------------
@pytest.fixture
def usdb(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for m in list(sys.modules):
        if m.startswith("src.us_screener") and m != "src.us_screener.data_source":
            del sys.modules[m]
    db = importlib.import_module("src.us_screener.db")
    db.DB_PATH = tmp_path / "us_screener.db"
    db._INITIALIZED = False
    db.ensure_schema()
    return db


def test_deactivate_missing(usdb):
    usdb.upsert_tickers([("A", "A", "US", 1, "2026-09-25", 1), ("ANSS", "ANSS", "S&P500", 1, "2026-09-25", 1)])
    assert usdb.deactivate_missing({"A"}) == 1
    assert [t["ticker"] for t in usdb.get_active_tickers()] == ["A"]


def test_pending_tickers_respects_rows_and_attempts(usdb):
    import json
    backfill = importlib.import_module("src.us_screener.backfill")
    usdb.upsert_tickers([("FULL", "F", "US", 1, "x", 3e9), ("SHORT", "S", "US", 1, "x", 2e9),
                         ("TRIED", "T", "US", 1, "x", 1e9)])
    usdb.upsert_ohlcv_bulk([("FULL", f"2020-01-{i:02d}", 1, 1, 1, 1, 1, None) for i in range(1, 2)])
    # FULL을 1000행 이상으로
    base = datetime(2019, 1, 1)
    usdb.upsert_ohlcv_bulk([("FULL", (base + timedelta(days=i)).date().isoformat(), 1, 1, 1, 1, 1, None)
                            for i in range(1000)])
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    usdb.meta_set(backfill.ATTEMPTS_META_KEY, json.dumps({"TRIED": today}))
    assert backfill.pending_tickers() == ["SHORT"]


def test_refresh_universe_deactivates_only_on_sane_response(usdb, monkeypatch):
    universe = importlib.import_module("src.us_screener.universe")
    monkeypatch.setattr(ds, "fetch_us_tickers", lambda: [("AAPL", "Apple", "S&P500")])
    monkeypatch.setattr(ds, "fetch_sectors", lambda: {"AAPL": "Information Technology"})
    usdb.upsert_tickers([("ANSS", "Ansys", "S&P500", 1, "x", None)])

    # 부분 응답(<1500) → 폴백, 비활성화 안 함
    monkeypatch.setattr(ds, "fetch_nasdaq_universe", lambda c, always_include=(): [
        {"ticker": "AAPL", "name": "Apple Inc", "market_cap": 3e12, "sector": "Technology"}])
    monkeypatch.setattr(ds, "fetch_market_caps", lambda: {"AAPL": 3e12})
    universe.refresh_universe()
    assert {t["ticker"] for t in usdb.get_active_tickers()} == {"AAPL", "ANSS"}

    # 정상 응답 → 목록 밖(ANSS) 비활성화, 지수 라벨·GICS 유지
    big = [{"ticker": f"T{i:04d}", "name": f"T{i}", "market_cap": 2e9, "sector": "X"} for i in range(1600)]
    big.append({"ticker": "AAPL", "name": "Apple Inc", "market_cap": 3e12, "sector": "Technology"})
    monkeypatch.setattr(ds, "fetch_nasdaq_universe", lambda c, always_include=(): big)
    n = universe.refresh_universe()
    act = {t["ticker"]: t for t in usdb.get_active_tickers()}
    assert n == 1601 and "ANSS" not in act
    assert act["AAPL"]["market"] == "S&P500" and act["AAPL"]["sector"] == "Information Technology"


def test_retention_covers_backfill_horizon():
    backfill = importlib.import_module("src.us_screener.backfill")
    incremental = importlib.import_module("src.us_screener.incremental")
    assert incremental.RETENTION_DAYS >= int(backfill.DEFAULT_DAYS * 1.5) + 15


def test_us_min_cap_env_isolated_from_kr(monkeypatch):
    signals = importlib.import_module("src.us_screener.signals")
    monkeypatch.setenv("SCREENER_MIN_MARKET_CAP", "100000000000")      # KR 1000억(원)
    monkeypatch.delenv("US_SCREENER_MIN_MARKET_CAP", raising=False)
    assert signals.min_market_cap() == 1_000_000_000                     # 미국은 영향 없음
