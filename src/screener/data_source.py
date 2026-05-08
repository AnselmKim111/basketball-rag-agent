"""KRX 일별 OHLCV/종목리스트 fetch — pykrx 우선, FinanceDataReader 폴백.

설계:
  - 모든 fetch는 retry + sleep으로 rate limit 회피
  - blocking I/O이므로 호출자가 run_in_executor로 감쌀 것
  - pykrx 실패 시 FinanceDataReader로 폴백 (KRX endpoint 변경/차단 대응)
  - 오늘 날짜에 데이터 없으면 빈 결과 반환 (휴장일 시그널)
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

log = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_S = 1.5
DEFAULT_RETRIES = 3


def _import_pykrx():
    """무거운 import는 함수 안에서 — orchestrator 부팅 실패 방지."""
    from pykrx import stock  # type: ignore
    return stock


def _import_fdr():
    import FinanceDataReader as fdr  # type: ignore
    return fdr


def fetch_market_ohlcv_by_date(date_str: str, market: str = "ALL") -> list[tuple]:
    """date_str(YYYYMMDD)에 해당하는 모든 종목 OHLCV 반환.

    반환 row: (ticker, date_iso, open, high, low, close, volume, value)
    빈 결과 = 휴장일 또는 데이터 소스 실패. FDR은 종목별이라 여기서는 pykrx만.
    pykrx 일관 실패시 backfill에서 ticker-batch 모드로 자동 전환.
    """
    stock = _import_pykrx()
    last_err = None
    for attempt in range(DEFAULT_RETRIES):
        try:
            df = stock.get_market_ohlcv(date_str, market=market)
            if df is None or df.empty:
                return []
            iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            cols = list(df.columns)
            # pykrx 버전별 컬럼 호환 (한글 또는 영어)
            col_map = {
                "open": next((c for c in ("시가", "Open", "open") if c in cols), None),
                "high": next((c for c in ("고가", "High", "high") if c in cols), None),
                "low":  next((c for c in ("저가", "Low", "low") if c in cols), None),
                "close": next((c for c in ("종가", "Close", "close") if c in cols), None),
                "volume": next((c for c in ("거래량", "Volume", "volume") if c in cols), None),
                "value": next((c for c in ("거래대금", "Value", "value") if c in cols), None),
            }
            if not all(col_map[k] for k in ("open", "high", "low", "close", "volume")):
                log.warning("[data_source] %s 알 수 없는 컬럼 구조: %s", date_str, cols)
                return []
            rows: list[tuple] = []
            for ticker, row in df.iterrows():
                try:
                    o = int(row[col_map["open"]])
                    h = int(row[col_map["high"]])
                    l = int(row[col_map["low"]])
                    c = int(row[col_map["close"]])
                    v = int(row[col_map["volume"]])
                    val = int(row[col_map["value"]]) if col_map["value"] else None
                except Exception:
                    continue
                if c <= 0 or v < 0:
                    continue
                rows.append((str(ticker), iso, o, h, l, c, v, val))
            return rows
        except Exception as e:
            last_err = e
            log.warning("[data_source] fetch %s 실패 attempt=%d err=%s", date_str, attempt + 1, e)
            time.sleep(2 ** attempt)
    log.error("[data_source] fetch %s 최종 실패 err=%s", date_str, last_err)
    return []


def fetch_ohlcv_by_ticker_via_fdr(
    ticker: str, start_iso: str, end_iso: str
) -> list[tuple]:
    """단일 종목 OHLCV 1년치 (FDR — Naver/Yahoo backed).

    반환: (ticker, date_iso, open, high, low, close, volume, value).
    실패 시 빈 리스트.
    """
    try:
        fdr = _import_fdr()
        df = fdr.DataReader(ticker, start_iso, end_iso)
    except Exception as e:
        log.warning("[data_source] FDR fetch %s 실패 err=%s", ticker, e)
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
        log.warning("[data_source] FDR %s 알 수 없는 컬럼: %s", ticker, cols)
        return []
    rows: list[tuple] = []
    for idx, row in df.iterrows():
        try:
            iso = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            o = int(row[o_c]); h = int(row[h_c]); l = int(row[l_c])
            c = int(row[c_c]); v = int(row[v_c])
        except Exception:
            continue
        if c <= 0 or v < 0:
            continue
        rows.append((str(ticker), iso, o, h, l, c, v, None))
    return rows


def fetch_market_caps(date_str: str | None = None) -> dict[str, int]:
    """KOSPI+KOSDAQ 전 종목 시가총액 (원 단위) — pykrx 사용.

    date_str=None이면 최근 영업일 기준. 빈 dict면 fetch 실패.
    """
    stock = _import_pykrx()
    if date_str is None:
        # 오늘 또는 최근 영업일 — pykrx는 휴장일에 빈 결과
        d = date.today()
        for _ in range(7):
            ds = d.strftime("%Y%m%d")
            try:
                df = stock.get_market_cap(ds, market="ALL")
                if df is not None and not df.empty:
                    date_str = ds
                    break
            except Exception:
                pass
            d -= timedelta(days=1)
        else:
            return {}
    else:
        try:
            df = stock.get_market_cap(date_str, market="ALL")
        except Exception as e:
            log.warning("[data_source] market_cap fetch %s 실패: %s", date_str, e)
            return {}
        if df is None or df.empty:
            return {}
    cols = list(df.columns)
    cap_c = next((c for c in ("시가총액", "MarketCap", "market_cap") if c in cols), None)
    if cap_c is None:
        log.warning("[data_source] market_cap 컬럼 인식 실패: %s", cols)
        return {}
    out: dict[str, int] = {}
    for ticker, row in df.iterrows():
        try:
            cap = int(row[cap_c])
        except Exception:
            continue
        if cap <= 0:
            continue
        out[str(ticker)] = cap
    log.info("[data_source] market_cap fetched %d종목 (date=%s)", len(out), date_str)
    return out


def fetch_kospi_kosdaq_tickers() -> list[tuple]:
    """KOSPI+KOSDAQ 보통주 ticker 리스트. pykrx → FDR 자동 폴백.

    반환: (ticker, name, market). ETF/SPAC/우선주/관리종목 제외 (universe.py에서 한 번 더 필터).
    """
    out = _fetch_tickers_via_pykrx()
    if out:
        return out
    log.warning("[data_source] pykrx ticker list 빈 결과 → FDR 폴백 시도")
    out = _fetch_tickers_via_fdr()
    if out:
        log.info("[data_source] FDR ticker list 성공: %d종목", len(out))
    else:
        log.error("[data_source] pykrx + FDR 모두 ticker list 실패")
    return out


def _fetch_tickers_via_pykrx() -> list[tuple]:
    try:
        stock = _import_pykrx()
    except Exception:
        log.exception("[data_source] pykrx import 실패")
        return []
    out: list[tuple] = []
    today_str = datetime.now().strftime("%Y%m%d")
    for market in ("KOSPI", "KOSDAQ"):
        last_err = None
        for attempt in range(DEFAULT_RETRIES):
            try:
                tickers = stock.get_market_ticker_list(today_str, market=market)
                if not tickers:
                    raise RuntimeError("empty ticker list")
                for t in tickers:
                    try:
                        name = stock.get_market_ticker_name(t)
                    except Exception:
                        name = t
                    out.append((str(t), str(name), market))
                break
            except Exception as e:
                last_err = e
                log.warning(
                    "[data_source] pykrx %s ticker list 실패 attempt=%d err=%s",
                    market, attempt + 1, e,
                )
                time.sleep(2 ** attempt)
        else:
            log.error("[data_source] pykrx %s ticker list 최종 실패 err=%s", market, last_err)
    return out


def _fetch_tickers_via_fdr() -> list[tuple]:
    try:
        fdr = _import_fdr()
    except Exception:
        log.exception("[data_source] FDR import 실패")
        return []
    out: list[tuple] = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
        except Exception:
            log.exception("[data_source] FDR.StockListing(%s) 실패", market)
            continue
        if df is None or df.empty:
            continue
        # FDR 컬럼은 버전에 따라 'Symbol'/'Code'/'Ticker', 'Name'/'name'
        cols = list(df.columns)
        code_c = next((c for c in ("Code", "Symbol", "ticker", "Ticker") if c in cols), None)
        name_c = next((c for c in ("Name", "name") if c in cols), None)
        if not code_c or not name_c:
            log.warning("[data_source] FDR %s 컬럼 인식 실패: %s", market, cols)
            continue
        for _, row in df.iterrows():
            t = str(row[code_c]).zfill(6)
            n = str(row[name_c])
            # 6자리 숫자 ticker만 (예: ETF/ETN 코드 제외)
            if not (len(t) == 6 and t.isdigit()):
                continue
            out.append((t, n, market))
    return out


def last_n_business_days(end: date, n: int) -> list[str]:
    """end 포함 과거 n개 영업일(월-금)을 YYYYMMDD 문자열 리스트로 반환 (asc).

    KRX 공휴일은 fetch 단계에서 빈 결과로 자연스럽게 걸러짐.
    """
    out: list[str] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:  # 월-금
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    out.reverse()
    return out


def is_business_day(d: date) -> bool:
    return d.weekday() < 5
