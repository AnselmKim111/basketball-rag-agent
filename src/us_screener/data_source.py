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
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_RETRIES = 3

# 비보통주 제외 — name 키워드 (ETF는 exchange 엔드포인트에서 이미 빠지나 안전망)
_NON_COMMON_RE = re.compile(
    r"\b(ETF|ETN|Warrant|Warrants|Unit|Units|Preferred|Right|Rights|Notes?|"
    r"Trust|Fund|Depositary)\b",
    re.IGNORECASE,
)
# 비보통주 심볼 마커 (우선주 ^, 워런트/유닛 접미 .W/.U, when-issued 등)
_NON_COMMON_SYM_RE = re.compile(r"[\^/$]|\.(W|U|R|WS|RT|UN)$", re.IGNORECASE)

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


_EXCHANGES = ("NASDAQ", "NYSE", "AMEX")


def _normalize_symbol(raw: str) -> str:
    """Nasdaq screener 심볼 → Yahoo/FDR 형식 정규화. 'BRK/B' → 'BRK.B'."""
    return str(raw).strip().upper().replace("/", ".")


def _is_common_stock(sym: str, name: str) -> bool:
    """보통주만 통과. ETF/우선주/워런트/유닛/권리 제외."""
    if not sym or sym == "NAN":
        return False
    if _NON_COMMON_SYM_RE.search(sym):
        return False
    if name and _NON_COMMON_RE.search(name):
        return False
    return True


def _parse_cap(raw) -> Optional[int]:
    """'$1,234,567,890' / '1234567890' / '' → int USD 또는 None."""
    if raw is None:
        return None
    s = str(raw).strip().replace("$", "").replace(",", "")
    if not s or s.upper() in ("NAN", "N/A", "--"):
        return None
    try:
        cap = int(float(s))
    except (ValueError, TypeError):
        return None
    return cap if cap > 0 else None


def _fetch_nasdaq_screener() -> list[tuple]:
    """1순위: Nasdaq screener API — 전 거래소 보통주 + 시총·섹터 직접 제공.

    반환: [(sym, name, cap|None, sector|None, exch), ...]. 실패 시 [].
    """
    import requests

    out: dict[str, tuple] = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for exch in _EXCHANGES:
        url = (
            "https://api.nasdaq.com/api/screener/stocks"
            f"?tableonly=true&limit=10000&exchange={exch}"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("[us_data] Nasdaq screener %s 실패: %s", exch, e)
            continue
        # 응답 구조 방어적 파싱: data.table.rows 또는 data.rows
        d = data.get("data") or {}
        rows = (d.get("table") or {}).get("rows") or d.get("rows") or []
        added = 0
        for r in rows:
            try:
                sym = _normalize_symbol(r.get("symbol", ""))
                name = str(r.get("name", "")).strip()
            except Exception:
                continue
            if not _is_common_stock(sym, name) or sym in out:
                continue
            cap = _parse_cap(r.get("marketCap"))
            sector = (str(r.get("sector", "")).strip() or None)
            if sector and sector.lower() in ("nan", "n/a", ""):
                sector = None
            out[sym] = (sym, name or sym, cap, sector, exch)
            added += 1
        log.info("[us_data] Nasdaq screener %s: %d종목 (누적 %d)", exch, added, len(out))
    return list(out.values())


def _fetch_fdr_exchange() -> list[tuple]:
    """2순위: FDR StockListing(거래소별) 합집합. 시총/섹터 컬럼 있으면 함께.

    반환: [(sym, name, cap|None, sector|None, exch), ...]. 실패 시 [].
    """
    out: dict[str, tuple] = {}
    for exch in _EXCHANGES:
        df = _fdr_listing(exch)
        if df is None or getattr(df, "empty", True):
            continue
        cols = list(df.columns)
        log.info("[us_data] FDR StockListing(%s) 컬럼: %s", exch, cols)
        sym_c = next((c for c in ("Symbol", "Code", "Ticker") if c in cols), None)
        name_c = next((c for c in ("Name", "name") if c in cols), None)
        cap_c = next(
            (c for c in ("MarketCap", "Marcap", "market_cap", "Market Cap", "Cap", "marketcap")
             if c in cols), None
        )
        sec_c = next((c for c in ("Sector", "Industry", "sector", "industry") if c in cols), None)
        if not sym_c:
            continue
        for _, row in df.iterrows():
            try:
                sym = _normalize_symbol(row[sym_c])
                name = str(row[name_c]).strip() if name_c else sym
            except Exception:
                continue
            if not _is_common_stock(sym, name) or sym in out:
                continue
            cap = _parse_cap(row[cap_c]) if cap_c else None
            sector = None
            if sec_c:
                s = str(row[sec_c]).strip()
                sector = s if s and s.lower() != "nan" else None
            out[sym] = (sym, name or sym, cap, sector, exch)
    if out:
        log.info("[us_data] FDR exchange listing 합집합: %d종목", len(out))
    return list(out.values())


def _fetch_sp500_nasdaq100() -> list[tuple]:
    """3순위 안전망: S&P500(FDR) + NASDAQ100(하드코딩). 시총/섹터는 S&P500만.

    반환: [(sym, name, cap|None, sector|None, exch), ...].
    """
    out: dict[str, tuple] = {}
    df = _fdr_listing("S&P500")
    if df is not None and not getattr(df, "empty", True):
        cols = list(df.columns)
        log.info("[us_data] S&P500 StockListing 컬럼: %s", cols)
        sym_c = next((c for c in ("Symbol", "Code", "Ticker") if c in cols), None)
        name_c = next((c for c in ("Name", "name") if c in cols), None)
        cap_c = next(
            (c for c in ("MarketCap", "Marcap", "market_cap", "Market Cap", "Cap", "marketcap")
             if c in cols), None
        )
        sec_c = next((c for c in ("Sector", "Industry", "sector", "industry") if c in cols), None)
        if sym_c:
            for _, row in df.iterrows():
                try:
                    sym = _normalize_symbol(row[sym_c])
                    nm = str(row[name_c]).strip() if name_c else sym
                except Exception:
                    continue
                if not sym or sym == "NAN" or sym in out:
                    continue
                cap = _parse_cap(row[cap_c]) if cap_c else None
                sector = None
                if sec_c:
                    s = str(row[sec_c]).strip()
                    sector = s if s and s.lower() != "nan" else None
                out[sym] = (sym, nm, cap, sector, "S&P500")
    for sym in _NASDAQ100:
        if sym not in out:
            out[sym] = (sym, sym, None, None, "NASDAQ100")
    log.info("[us_data] 안전망 유니버스: %d종목 (S&P500+NASDAQ100)", len(out))
    return list(out.values())


# 모듈 캐시 — 한 실행에서 fetch_us_tickers/caps/sectors가 소스를 1회만 호출
_listings_cache: list[tuple] | None = None


def fetch_us_listings(force: bool = False) -> list[tuple]:
    """광역 미국 유니버스 — 보통주 + 시총·섹터. 3단 소스 체인.

    반환: [(sym, name, cap|None, sector|None, exch), ...].
    Nasdaq screener(1) → FDR 거래소 listing(2) → S&P500+NASDAQ100 안전망(3).
    같은 실행에서는 모듈 캐시 사용 (caps/sectors/tickers wrapper가 재호출해도 1회).
    """
    global _listings_cache
    if _listings_cache is not None and not force:
        return _listings_cache

    listings = _fetch_nasdaq_screener()
    if len(listings) >= 1000:
        log.info("[us_data] 유니버스 소스=Nasdaq screener (%d종목)", len(listings))
    else:
        log.warning("[us_data] Nasdaq screener 부족(%d) → FDR 거래소 listing 시도", len(listings))
        fdr_listings = _fetch_fdr_exchange()
        if len(fdr_listings) > len(listings):
            listings = fdr_listings
        if len(listings) < 600:
            log.warning("[us_data] 광역 소스 모두 부족(%d) → S&P500+NASDAQ100 안전망", len(listings))
            listings = _fetch_sp500_nasdaq100()

    _listings_cache = listings
    return listings


def fetch_us_tickers() -> list[tuple]:
    """유니버스 ticker 목록. 반환: (symbol, name, exchange_label). (시그니처 유지)"""
    return [(sym, name, exch) for sym, name, _cap, _sec, exch in fetch_us_listings()]


def fetch_market_caps() -> dict[str, int]:
    """{symbol: market_cap(USD)} — fetch_us_listings에서 분해. (시그니처 유지)"""
    out = {sym: cap for sym, _n, cap, _s, _e in fetch_us_listings() if cap and cap > 0}
    if out:
        log.info("[us_data] market_cap: %d종목", len(out))
    return out


def fetch_sectors() -> dict[str, str]:
    """{symbol: sector} — fetch_us_listings에서 분해. (시그니처 유지)"""
    out = {sym: sec for sym, _n, _c, sec, _e in fetch_us_listings() if sec}
    if out:
        log.info("[us_data] sector: %d종목", len(out))
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
