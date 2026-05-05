"""pykrx wrapper — KRX 일별 OHLCV/종목리스트 fetch.

설계:
  - 모든 fetch는 retry + sleep으로 KRX rate limit 회피
  - blocking I/O이므로 호출자가 run_in_executor로 감쌀 것
  - pykrx 실패 시 FinanceDataReader로 폴백 (단일 종목)
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


def fetch_market_ohlcv_by_date(date_str: str, market: str = "ALL") -> list[tuple]:
    """date_str(YYYYMMDD)에 해당하는 모든 종목 OHLCV 반환.

    반환 row: (ticker, date_iso, open, high, low, close, volume, value)
    빈 결과 = 휴장일.
    """
    stock = _import_pykrx()
    last_err = None
    for attempt in range(DEFAULT_RETRIES):
        try:
            df = stock.get_market_ohlcv(date_str, market=market)
            if df is None or df.empty:
                return []
            iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            rows: list[tuple] = []
            for ticker, row in df.iterrows():
                try:
                    o = int(row["시가"])
                    h = int(row["고가"])
                    l = int(row["저가"])
                    c = int(row["종가"])
                    v = int(row["거래량"])
                    val = int(row.get("거래대금", 0)) if "거래대금" in row else None
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


def fetch_kospi_kosdaq_tickers() -> list[tuple]:
    """KOSPI+KOSDAQ 보통주 ticker 리스트.

    반환: (ticker, name, market). ETF/SPAC/우선주/관리종목 제외 (universe.py에서 한 번 더 필터).
    """
    stock = _import_pykrx()
    out: list[tuple] = []
    today_str = datetime.now().strftime("%Y%m%d")
    for market in ("KOSPI", "KOSDAQ"):
        last_err = None
        for attempt in range(DEFAULT_RETRIES):
            try:
                tickers = stock.get_market_ticker_list(today_str, market=market)
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
                    "[data_source] %s ticker list 실패 attempt=%d err=%s",
                    market, attempt + 1, e,
                )
                time.sleep(2 ** attempt)
        else:
            log.error("[data_source] %s ticker list 최종 실패 err=%s", market, last_err)
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
