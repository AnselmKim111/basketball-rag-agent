"""미국 시장 OHLCV/유니버스 fetch — FDR 1순위, Stooq 폴백.

한국 src/screener/data_source.py와 **동일한 함수 시그니처**를 제공해 us_screener의
incremental/validator/backfill 모듈이 무수정 재사용 가능하게 함. 단 내부 구현은
미국 데이터 소스(FDR DataReader, Stooq CSV).

데이터 소스 우선순위 (종목별 OHLCV):
  1. FDR DataReader (Yahoo/Stooq backed)
  2. Stooq CSV 직접 (https://stooq.com/q/d/l/?s={sym}.us&i=d)
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_RETRIES = 3

# 클래스 주식 심볼 매핑 (FDR StockListing 형식 → Yahoo 형식)
_SYMBOL_FIX = {
    "BRKB": "BRK-B",  # Berkshire Hathaway B
    "BFB": "BF-B",    # Brown-Forman B
}


def _import_fdr():
    import FinanceDataReader as fdr  # type: ignore
    return fdr


# ------------------------------------------------------------------
# 종목별 OHLCV
# ------------------------------------------------------------------
def fetch_ohlcv_by_ticker_via_fdr(ticker: str, start_iso: str, end_iso: str) -> list[tuple]:
    """단일 종목 일별 OHLCV — FDR DataReader.

    반환: (ticker, date_iso, o, h, l, c, v, value=None) — 한국과 동일 형식.
    """
    # 클래스 주식 심볼 정규화 → Yahoo 형식 (BRKB/BFB는 점도 없이 와서 특수 매핑)
    fetch_sym = _SYMBOL_FIX.get(ticker, ticker.replace(".", "-"))
    try:
        fdr = _import_fdr()
        df = fdr.DataReader(fetch_sym, start_iso, end_iso)
    except Exception as e:
        log.warning("[us_data] FDR fetch %s 실패: %s", ticker, e)
        return []
    if df is None or df.empty:
        return []
    cols = list(df.columns)
    o_c = next((c for c in ("Open", "open") if c in cols), None)
    h_c = next((c for c in ("High", "high") if c in cols), None)
    l_c = next((c for c in ("Low", "low") if c in cols), None)
    c_c = next((c for c in ("Close", "close") if c in cols), None)
    v_c = next((c for c in ("Volume", "volume") if c in cols), None)
    if not all([o_c, h_c, l_c, c_c, v_c]):
        log.warning("[us_data] FDR %s 알 수 없는 컬럼: %s", ticker, cols)
        return []
    rows: list[tuple] = []
    for idx, row in df.iterrows():
        try:
            iso = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            o = float(row[o_c]); h = float(row[h_c]); l = float(row[l_c])
            c = float(row[c_c]); v = float(row[v_c])
        except Exception:
            continue
        # 미국 주가는 소수점 — 정수 변환 대신 반올림 보존 (cent 단위 ×100 안 함, float 그대로 int화 시 손실)
        # DB ohlcv는 INTEGER 컬럼 → cent 단위로 저장 (×100) 하여 정밀도 유지
        if c <= 0 or v < 0:
            continue
        rows.append((str(ticker), iso, int(round(o * 100)), int(round(h * 100)),
                     int(round(l * 100)), int(round(c * 100)), int(v), None))
    return rows


def fetch_ohlcv_by_ticker_via_stooq(ticker: str, start_iso: str, end_iso: str) -> list[tuple]:
    """Stooq CSV 직접 — FDR 폴백 + cross-validation 독립 소스.

    URL: https://stooq.com/q/d/l/?s={sym}.us&i=d  (Date,Open,High,Low,Close,Volume)
    반환: cent 단위 정수 (한국 data_source와 동일하게 INTEGER 저장).
    """
    import requests
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.text.strip()
    except Exception as e:
        log.warning("[us_data] Stooq fetch %s 실패: %s", ticker, e)
        return []
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        return []
    rows: list[tuple] = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        d_iso = parts[0]
        if d_iso < start_iso or d_iso > end_iso:
            continue
        try:
            o = float(parts[1]); h = float(parts[2]); l = float(parts[3])
            c = float(parts[4]); v = float(parts[5])
        except Exception:
            continue
        if c <= 0 or v < 0:
            continue
        rows.append((str(ticker), d_iso, int(round(o * 100)), int(round(h * 100)),
                     int(round(l * 100)), int(round(c * 100)), int(v), None))
    return rows


def fetch_ohlcv_by_ticker_via_naver(ticker: str, start_iso: str, end_iso: str) -> list[tuple]:
    """한국 모듈 호환 진입점 (이름 유지). 미국은 Naver 없음 → FDR 1순위, Stooq 폴백.

    incremental/validator/backfill 복사본이 이 함수명을 호출하므로 시그니처 유지.
    """
    rows = fetch_ohlcv_by_ticker_via_fdr(ticker, start_iso, end_iso)
    if not rows:
        rows = fetch_ohlcv_by_ticker_via_stooq(ticker, start_iso, end_iso)
    return rows


def fetch_market_ohlcv_by_date(date_str: str, market: str = "ALL") -> list[tuple]:
    """미국은 date-batch 미지원 (종목별 fetch만). 빈 리스트 반환 → 호출자가 ticker-batch."""
    return []


# ------------------------------------------------------------------
# 유니버스 (S&P500 + Nasdaq100)
# ------------------------------------------------------------------
def _fdr_listing(name: str):
    try:
        fdr = _import_fdr()
        return fdr.StockListing(name)
    except Exception:
        log.exception("[us_data] FDR.StockListing(%s) 실패", name)
        return None


# NASDAQ100 종목 (2025 기준). FDR StockListing('NASDAQ100') 미지원 환경 대비 하드코딩.
# S&P500과 합집합 → 중복 자동 제거. 대부분 S&P500 포함, 고유 종목(외국계 ADR 등) 보강.
_NASDAQ100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR",
    "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO",
    "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DLTR", "DXCM", "EA", "EXC",
    "FANG", "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX",
    "ILMN", "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU",
    "MAR", "MCHP", "MDB", "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MU",
    "NFLX", "NVDA", "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD",
    "PEP", "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM", "TMUS",
    "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]


def fetch_us_tickers() -> list[tuple]:
    """S&P500(FDR) + Nasdaq100(하드코딩) 합집합. 반환: (symbol, name, index_label).

    index_label = 'S&P500' | 'NASDAQ100' (둘 다면 S&P500 우선).
    """
    out: dict[str, tuple] = {}
    # 1) S&P500 — FDR StockListing
    df = _fdr_listing("S&P500")
    if df is not None and not df.empty:
        cols = list(df.columns)
        log.info("[us_data] S&P500 StockListing 컬럼: %s", cols)  # 시총 컬럼명 진단
        sym_c = next((c for c in ("Symbol", "Code", "Ticker") if c in cols), None)
        name_c = next((c for c in ("Name", "name") if c in cols), None)
        if sym_c:
            for _, row in df.iterrows():
                try:
                    sym = str(row[sym_c]).strip().upper()
                    nm = str(row[name_c]).strip() if name_c else sym
                except Exception:
                    continue
                if sym and sym != "NAN" and sym not in out:
                    out[sym] = (sym, nm, "S&P500")
    # 2) NASDAQ100 — 하드코딩 (S&P500 미포함 종목만 추가)
    for sym in _NASDAQ100:
        if sym not in out:
            out[sym] = (sym, sym, "NASDAQ100")
    log.info("[us_data] 유니버스 fetch: %d종목 (S&P500 FDR + NASDAQ100 하드코딩)", len(out))
    return list(out.values())


def fetch_market_caps() -> dict[str, int]:
    """{symbol: market_cap(USD)} — FDR StockListing의 시총 컬럼."""
    out: dict[str, int] = {}
    for idx_name in ("S&P500",):  # NASDAQ100 StockListing 미지원 → S&P500만
        df = _fdr_listing(idx_name)
        if df is None or df.empty:
            continue
        cols = list(df.columns)
        sym_c = next((c for c in ("Symbol", "Code", "Ticker") if c in cols), None)
        cap_c = next(
            (c for c in ("MarketCap", "Marcap", "market_cap", "Market Cap", "Cap", "marketcap")
             if c in cols), None
        )
        if not sym_c or not cap_c:
            log.warning("[us_data] 시총 컬럼 없음 — cols=%s", cols)
            continue
        for _, row in df.iterrows():
            try:
                sym = str(row[sym_c]).strip().upper()
                cap = int(float(row[cap_c]))
            except Exception:
                continue
            if sym and cap > 0 and sym not in out:
                out[sym] = cap
    if out:
        log.info("[us_data] market_cap fetch: %d종목", len(out))
    return out


def fetch_sectors() -> dict[str, str]:
    """{symbol: sector} — FDR StockListing의 Sector/Industry 컬럼 (미국은 GICS 제공)."""
    out: dict[str, str] = {}
    for idx_name in ("S&P500",):  # NASDAQ100 StockListing 미지원 → S&P500만
        df = _fdr_listing(idx_name)
        if df is None or df.empty:
            continue
        cols = list(df.columns)
        sym_c = next((c for c in ("Symbol", "Code", "Ticker") if c in cols), None)
        sec_c = next((c for c in ("Sector", "Industry", "sector", "industry") if c in cols), None)
        if not sym_c or not sec_c:
            continue
        for _, row in df.iterrows():
            try:
                sym = str(row[sym_c]).strip().upper()
                sec = str(row[sec_c]).strip()
            except Exception:
                continue
            if sym and sec and sec.lower() != "nan" and sym not in out:
                out[sym] = sec
    if out:
        log.info("[us_data] sector fetch: %d종목", len(out))
    return out


def apply_sector_keywords(name: str) -> Optional[str]:
    """미국은 FDR이 GICS 섹터를 제공하므로 keyword 휴리스틱 불필요."""
    return None


# ------------------------------------------------------------------
# 거래일 helper
# ------------------------------------------------------------------
def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def last_n_business_days(end: date, n: int) -> list[str]:
    out: list[str] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    out.reverse()
    return out
