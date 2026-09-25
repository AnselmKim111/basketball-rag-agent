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
    assert calls == [("2026-09-12", "2026-09-22")]       # 넓은 창(좁으면 최신 행 누락 실측)

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
    monkeypatch.setattr(ds, "_otherlisted_rows", lambda max_age_s=3600: [])
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
    # FULL: 1000행 이상 + 최신일이 최근(공백 아님)
    end = datetime.now().date()
    usdb.upsert_ohlcv_bulk([("FULL", (end - timedelta(days=i)).isoformat(), 1, 1, 1, 1, 1, None)
                            for i in range(1000)])
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    usdb.meta_set(backfill.ATTEMPTS_META_KEY, json.dumps({"TRIED": today}))
    assert backfill.pending_tickers() == ["SHORT"]


def test_refresh_universe_deactivates_only_on_sane_response(usdb, monkeypatch):
    universe = importlib.import_module("src.us_screener.universe")
    monkeypatch.setattr(ds, "fetch_us_tickers", lambda: [("AAPL", "Apple", "S&P500")])
    monkeypatch.setattr(ds, "fetch_sectors", lambda: {"AAPL": "Information Technology"})
    usdb.upsert_tickers([("ANSS", "Ansys", "S&P500", 1, "x", None)])

    # 부분 응답(원본 행 부족) → 폴백, 비활성화 안 함
    monkeypatch.setattr(ds, "fetch_nasdaq_universe", lambda c, always_include=(): [
        {"ticker": "AAPL", "name": "Apple Inc", "market_cap": 3e12, "sector": "Technology"}])
    monkeypatch.setattr(ds, "fetch_market_caps", lambda: {"AAPL": 3e12})
    monkeypatch.setattr(ds, "nasdaq_screener_raw_count", lambda: 120)
    universe.refresh_universe()
    assert {t["ticker"] for t in usdb.get_active_tickers()} == {"AAPL", "ANSS"}

    # 정상 응답 → 목록 밖(ANSS) 비활성화, 지수 라벨·GICS 유지
    big = [{"ticker": f"T{i:04d}", "name": f"T{i}", "market_cap": 2e9, "sector": "X"} for i in range(1600)]
    big.append({"ticker": "AAPL", "name": "Apple Inc", "market_cap": 3e12, "sector": "Technology"})
    monkeypatch.setattr(ds, "fetch_nasdaq_universe", lambda c, always_include=(): big)
    monkeypatch.setattr(ds, "nasdaq_screener_raw_count", lambda: 7000)
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


# ------------------------------------------------------------------
# 리뷰 워크플로 확정 지적 회귀 (2026-09-25)
# ------------------------------------------------------------------
def test_nasdaq_symbol_variants_for_class_shares():
    assert ds._nasdaq_symbol_variants("HEI.A") == ["HEI%25sl%25A", "HEI.A"]
    assert ds._nasdaq_symbol_variants("BRKB") == ["BRK%25sl%25B", "BRK.B"]
    assert ds._nasdaq_symbol_variants("AAPL") == ["AAPL"]


def test_unfinished_session_bar_is_dropped():
    et = timezone(timedelta(hours=-4))
    now = datetime(2026, 9, 25, 10, 30, tzinfo=et)                 # 금 장중
    assert ds._is_unfinished_session("2026-09-25", now) is True
    assert ds._is_unfinished_session("2026-09-24", now) is False
    assert ds._is_unfinished_session("2026-09-25", now.replace(hour=16, minute=45)) is False


def test_session_fill_used_when_target_gap_and_nasdaq_lags(monkeypatch):
    monkeypatch.setattr(ds, "_yahoo_chart", lambda t, s, e: (
        [("A", "2026-09-23", 1, 1, 1, 16532, 1, None)], ["2026-09-24"]))
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_nasdaq", lambda *a: [])   # 1일 lag
    monkeypatch.setattr(ds, "_yahoo_session_bar",
                        lambda t, d: ("A", d, 16395, 17477, 16362, 17284, 3028845, None))
    rows = ds.fetch_ohlcv_by_ticker_via_naver("A", "2026-09-15", "2026-09-24")
    assert rows[-1][1] == "2026-09-24" and rows[-1][5] == 17284


def test_fdr_not_in_chain(monkeypatch):
    monkeypatch.setattr(ds, "_yahoo_chart", lambda t, s, e: ([], []))
    called = []
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_fdr", lambda *a: called.append(1) or [])
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_stooq", lambda *a: [])
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_nasdaq", lambda *a: [])
    assert ds.fetch_ohlcv_by_ticker_via_naver("A", "2026-09-15", "2026-09-24") == []
    assert called == []                     # timeout 없는 FDR(같은 Yahoo)로 풀을 붙잡지 않음


def test_detect_split():
    inc = importlib.import_module("src.us_screener.incremental")
    fetched = [("X", "2026-09-22", 0, 0, 0, 1000, 0, None), ("X", "2026-09-23", 0, 0, 0, 1010, 0, None)]
    assert inc.detect_split(fetched, {"2026-09-22": 10000, "2026-09-23": 10100}) == pytest.approx(0.1)
    assert inc.detect_split(fetched, {"2026-09-22": 1000, "2026-09-23": 1010}) is None   # 동일 스케일
    assert inc.detect_split(fetched[:1], {"2026-09-22": 10000}) is None                   # 근거 1개
    assert inc.detect_split(fetched, {"2026-09-22": 10000, "2026-09-23": 1010}) is None   # 비일관


def test_backfill_failure_retried_next_day_success_waits_30(usdb):
    import json
    backfill = importlib.import_module("src.us_screener.backfill")
    usdb.upsert_tickers([("OK", "O", "US", 1, "x", 3e9), ("BAD", "B", "US", 1, "x", 2e9)])
    today = datetime.now(timezone(timedelta(hours=9))).date()
    yday = (today - timedelta(days=1)).isoformat()
    usdb.meta_set(backfill.ATTEMPTS_META_KEY,
                  json.dumps({"OK": today.isoformat(), "BAD": f"fail:{yday}"}))
    assert backfill.pending_tickers() == ["BAD"]      # 실패는 다음날 재시도, 성공은 30일 대기


def test_pending_includes_stale_history(usdb):
    backfill = importlib.import_module("src.us_screener.backfill")
    usdb.upsert_tickers([("GAP", "G", "US", 1, "x", 3e9)])
    old_end = datetime.now().date() - timedelta(days=60)          # 재활성화 공백
    usdb.upsert_ohlcv_bulk([("GAP", (old_end - timedelta(days=i)).isoformat(), 1, 1, 1, 1, 1, None)
                            for i in range(1100)])
    assert backfill.pending_tickers() == ["GAP"]


def test_latest_date_with_coverage_ignores_thin_dates(usdb):
    usdb.upsert_tickers([(f"T{i}", "t", "US", 1, "x", 2e9) for i in range(10)])
    rows = [(f"T{i}", "2026-09-24", 1, 1, 1, 1, 1, None) for i in range(10)]
    rows += [("T0", "2026-09-25", 1, 1, 1, 1, 1, None)]           # 장중 봉이 1종목에만
    usdb.upsert_ohlcv_bulk(rows)
    assert usdb.latest_date() == "2026-09-25"
    assert usdb.latest_date_with_coverage(0.5) == "2026-09-24"
    assert usdb.latest_date_with_coverage(0.5, max_date="2026-09-23") is None


def test_replace_ticker_history_atomic(usdb):
    usdb.upsert_ohlcv_bulk([("S", "2026-09-2%d" % i, 1, 1, 1, 10000, 1, None) for i in range(1, 5)])
    n = usdb.replace_ticker_history("S", [("S", "2026-09-24", 1, 1, 1, 1000, 1, None)])
    assert n == 1 and usdb.closes_for_ticker("S", "2026-01-01") == {"2026-09-24": 1000}


def test_upsert_tickers_keeps_cap_when_null(usdb):
    usdb.upsert_tickers([("A", "A", "US", 1, "x", 5e10)])
    usdb.upsert_tickers([("A", "A", "S&P500", 1, "y", None)])   # 폴백 경로
    assert usdb.get_active_tickers()[0]["market_cap"] == 5e10


def test_us_target_date_skips_nyse_holidays():
    inc = importlib.import_module("src.us_screener.incremental")
    et = timezone(timedelta(hours=-5))
    # 추수감사절(11-26) 다음날 07:00 KST = 11-26 17:00 ET → 대상일은 11-25
    assert inc.us_target_date(datetime(2026, 11, 26, 17, 0, tzinfo=et)).isoformat() == "2026-11-25"
    # 노동절(09-07) 다음날 화 07:00 KST = 09-07 18:00 ET → 09-04(금)
    assert inc.us_target_date(datetime(2026, 9, 7, 18, 0, tzinfo=timezone(timedelta(hours=-4)))).isoformat() == "2026-09-04"


def test_formatter_header_shows_short_history_and_cap():
    from src.us_screener.formatter import format_results
    msg = format_results({}, datetime(2026, 9, 25, 7, 2), base_date="2026-09-24",
                         stats={"processed": 2540, "skipped_no_base": 0, "skipped_short": 12,
                                "min_cap": 1e9})
    assert "2540종목 신호 계산 (미국 보통주 시총 $1B+)" in msg
    assert "신규상장 12종목 이력 부족" in msg and "base_date 데이터 누락" not in msg


def test_no_trade_day_recorded_as_flat_bar(usdb, monkeypatch):
    inc = importlib.import_module("src.us_screener.incremental")
    usdb.upsert_tickers([("AAPL", "Apple", "S&P500", 1, "x", 3e12), ("SENEB", "Seneca", "US", 1, "x", 1.3e9)])
    usdb.upsert_ohlcv_bulk([("SENEB", "2026-09-23", 18742, 18742, 18742, 18742, 406, None)])

    def fake_chain(t, s, e, known_dates=None):
        if t == "AAPL":
            return [("AAPL", "2026-09-24", 1, 1, 1, 33592, 1, None)]
        return [("SENEB", "2026-09-23", 18742, 18742, 18742, 18742, 406, None)]
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_naver", fake_chain)
    monkeypatch.setattr(ds, "last_trade_date", lambda t: "2026-09-23")
    monkeypatch.setenv("US_SCREENER_RETRY_PASS_SLEEP_S", "0")
    res = inc.update_specific_date("2026-09-24")
    assert res["coverage"]["miss"] == 0 and res["coverage"]["no_trade"] == 1
    bar = usdb.load_ohlcv("SENEB", days=1)[-1]
    assert bar["date"] == "2026-09-24" and bar["close"] == 18742 and bar["volume"] == 0


# ------------------------------------------------------------------
# 2차 리뷰 확정 지적 회귀
# ------------------------------------------------------------------
def test_detect_split_with_late_yahoo_adjustment():
    inc = importlib.import_module("src.us_screener.incremental")
    # D-3..D-1은 옛 스케일(비율 0.5), 최신 D는 이미 새 스케일(1.0) — 예전엔 None
    fetched = [("X", f"2026-09-2{i}", 0, 0, 0, 10000, 0, None) for i in range(1, 5)]
    db_closes = {"2026-09-21": 20000, "2026-09-22": 20000, "2026-09-23": 20000, "2026-09-24": 10000}
    assert inc.detect_split(fetched, db_closes) == pytest.approx(0.5)


def test_session_bar_requires_official_close(monkeypatch):
    import requests
    et = 1790256600   # 2026-09-24 13:30 UTC = 09:30 EDT
    payload = {"chart": {"result": [{"meta": {"exchangeTimezoneName": "America/New_York",
        "regularMarketTime": et + 6.5 * 3600, "regularMarketPrice": 172.84,
        "regularMarketDayHigh": 174.77, "regularMarketDayLow": 163.62, "regularMarketVolume": 3028845},
        "timestamp": [et, et + 1800],
        "indicators": {"quote": [{"open": [163.95, 165.0], "high": [1, 1], "low": [1, 1],
                                  "close": [165.0, 166.0], "volume": [1, 1]}]}}]}}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    r = ds._yahoo_session_bar("A", "2026-09-24")
    assert r[2:7] == (16395, 17477, 16362, 17284, 3028845)      # 공식 종가·고저·거래량
    assert ds._yahoo_session_bar("A", "2026-09-23") is None      # 과거일은 재구성 안 함


def test_formatter_displayed_items_matches_section_dedup(monkeypatch):
    monkeypatch.setenv("US_SCREENER_PER_CATEGORY_TOP", "2")
    fmt = importlib.import_module("src.us_screener.formatter")
    mk = lambda t, c: {"ticker": t, "chg_pct": c}   # noqa: E731
    res = {"high_all": [mk("A", 5), mk("B", 4), mk("C", 3)],       # C는 상한 밖
           "high_52w": [mk("A", 5), mk("B", 4), mk("C", 3), mk("D", 2)]}
    shown = [it["ticker"] for it in fmt.displayed_items(res)]
    assert shown == ["A", "B", "D"]            # C는 앞 섹션 상한 밖이라 어디에도 표시 안 됨
    msg = fmt.format_results(res, datetime(2026, 9, 25, 7, 0))
    assert "D" in msg.split("52주 신고가")[1]


def test_validator_stats_are_per_ticker(monkeypatch):
    validator = importlib.import_module("src.us_screener.validator")
    res = {"high_all": [{"ticker": "X", "close": 100}], "high_52w": [{"ticker": "X", "close": 100},
           {"ticker": "Y", "close": 200}]}
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_naver",
                        lambda t, s, e, known_dates=None: [(t, "2026-09-24", 0, 0, 0, 999 if t == "X" else 200, 0, None)])
    v, st = validator.cross_validate(res, "2026-09-24")
    assert st["rejected"] == 1 and st["validated"] == 1          # 예전: rejected=2, validated=0
    assert [it["ticker"] for it in v["high_52w"]] == ["Y"]


def test_universe_sanity_uses_last_sane_basis(usdb, monkeypatch):
    universe = importlib.import_module("src.us_screener.universe")
    monkeypatch.setattr(ds, "fetch_us_tickers", lambda: [("AAPL", "Apple", "S&P500")])
    monkeypatch.setattr(ds, "fetch_sectors", lambda: {})
    monkeypatch.setattr(ds, "nasdaq_screener_raw_count", lambda: 7000)
    big = lambda n: [{"ticker": f"T{i:04d}", "name": "t", "market_cap": 3e9, "sector": ""}  # noqa: E731
                     for i in range(n)] + [{"ticker": "AAPL", "name": "Apple", "market_cap": 3e12, "sector": ""}]
    monkeypatch.setattr(ds, "fetch_nasdaq_universe", lambda c, always_include=(): big(2500))
    assert universe.refresh_universe() == 2501
    # 시총 기준 상향 → 목록 급감해도 기준이 바뀌었으므로 정상 처리 (영구 폴백 금지)
    monkeypatch.setenv("US_SCREENER_MIN_MARKET_CAP", "2e9")
    monkeypatch.setattr(ds, "fetch_nasdaq_universe", lambda c, always_include=(): big(1800))
    assert universe.refresh_universe() == 1801
    assert len(usdb.get_active_tickers()) == 1801
    # 같은 기준에서 급감(부분 응답) → 폴백, 비활성화 없음, 비활성 종목 부활 없음
    monkeypatch.setattr(ds, "fetch_nasdaq_universe", lambda c, always_include=(): big(900))
    monkeypatch.setattr(ds, "fetch_market_caps", lambda: {})
    usdb.upsert_tickers([("ANSS", "Ansys", "S&P500", 0, "x", None)])
    monkeypatch.setattr(ds, "fetch_us_tickers", lambda: [("AAPL", "Apple", "S&P500"), ("ANSS", "Ansys", "NASDAQ100")])
    universe.refresh_universe()
    act = {t["ticker"] for t in usdb.get_active_tickers()}
    assert len(act) == 1801 and "ANSS" not in act


def test_update_today_fetches_only_missing(usdb, monkeypatch):
    inc = importlib.import_module("src.us_screener.incremental")
    usdb.upsert_tickers([("AAPL", "a", "US", 1, "x", 3e12), ("THIN", "t", "US", 1, "x", 1e9)])
    monkeypatch.setattr(inc, "us_target_date", lambda now_et=None: datetime(2026, 9, 24).date())
    usdb.upsert_ohlcv_bulk([("AAPL", "2026-09-24", 1, 1, 1, 1, 1, None)])
    seen = []
    monkeypatch.setattr(ds, "fetch_ohlcv_by_ticker_via_naver",
                        lambda t, s, e, known_dates=None: seen.append(t) or [(t, "2026-09-24", 1, 1, 1, 1, 1, None)])
    monkeypatch.setenv("US_SCREENER_RETRY_PASS_SLEEP_S", "0")
    out = inc.update_today()
    assert seen == ["THIN"] and out["coverage"]["miss"] == 0     # probe 없이 누락분만
    assert inc.update_today()["coverage"]["miss"] == 0            # 전부 보유 → 수집 생략


def test_stale_sec_facts_rejected():
    f = importlib.import_module("src.us_screener.fundamentals")
    facts = {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"val": 941481, "end": "2011-04-29"}]}}}}}
    assert f._latest_shares(facts) is None


def test_screener_rows_disk_cache_on_live_failure(monkeypatch, tmp_path):
    """2026-09-25 Railway 실측: Nasdaq screener 40초+ 응답 → timeout 3연속 → 유니버스 폴백.
    라이브 성공 시 디스크 저장, 실패 시 72h 이내 마지막 정상 응답 사용, 만료면 []."""
    import requests
    from src.us_screener import data_source as ds
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    live = [{"symbol": "AAPL", "name": "Apple Inc. Common Stock", "marketCap": "3e12",
             "industry": "Computer Manufacturing", "sector": "Technology", "lastsale": "$1"}]

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"rows": live}}
    calls = []

    def _ok(url, **kw):
        calls.append(kw.get("timeout"))
        return _R()
    monkeypatch.setattr(requests, "get", _ok)
    ds._NASDAQ_SCREENER_CACHE.update(at=0, rows=None)
    assert ds._nasdaq_screener_rows()[0]["symbol"] == "AAPL"
    assert calls[0][1] >= 90                                  # read timeout 30→90
    assert (tmp_path / "us_nasdaq_screener.json").exists()

    def _fail(url, **kw):
        raise requests.exceptions.ReadTimeout("slow")
    monkeypatch.setattr(requests, "get", _fail)
    ds._NASDAQ_SCREENER_CACHE.update(at=0, rows=None)
    rows = ds._nasdaq_screener_rows()
    assert [r["symbol"] for r in rows] == ["AAPL"] and rows[0]["marketCap"] == "3e12"
    assert "lastsale" not in rows[0]                          # slim 저장

    monkeypatch.setenv("US_SCREENER_ROWS_MAX_AGE_H", "0")     # 만료 → 캐시 미사용
    ds._NASDAQ_SCREENER_CACHE.update(at=0, rows=None)
    assert ds._nasdaq_screener_rows() == []
    ds._NASDAQ_SCREENER_CACHE.update(at=0, rows=None)
