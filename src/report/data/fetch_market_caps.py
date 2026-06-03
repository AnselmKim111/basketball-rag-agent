"""시총 batch fetch — watchlist enrichment용.

전략:
- KR: screener_adapter.load_active_tickers(KR) 우선 → 비어있으면 FDR.StockListing("KRX") 폴백.
  in-memory 30분 캐시 (모듈 레벨).
- US: FMP /stable/quote?symbol=A,B,C batch (무료 키 작동, marketCap 필드).
  실패 시 0 반환 (graceful — watchlist에서 norm_cap=0 처리).

호출 예:
  caps = fetch_kr_market_caps()                # {"005930": 5.2e14, ...}
  caps = fetch_us_market_caps(["AAPL","MSFT"]) # {"AAPL": 3.5e12, ...}
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_KR_CACHE: dict[str, int] = {}
_KR_CACHE_TS: float = 0.0
_KR_CACHE_TTL = 1800  # 30분

_US_CACHE: dict[str, int] = {}
_US_CACHE_TS: float = 0.0
_US_CACHE_TTL = 1800

_FMP_BASE = "https://financialmodelingprep.com/stable"


def fetch_kr_market_caps() -> dict[str, int]:
    """6자리 ticker → 시총(원). screener_adapter 우선 → FDR 폴백."""
    global _KR_CACHE, _KR_CACHE_TS
    now = time.time()
    if _KR_CACHE and (now - _KR_CACHE_TS) < _KR_CACHE_TTL:
        return _KR_CACHE

    out: dict[str, int] = {}

    # 1차: screener_adapter (screener.db에 시총 캐시된 경우 — 16:00 cron으로 채워짐)
    try:
        from src.report.data import screener_adapter
        active = screener_adapter.load_active_tickers("KR")
        for row in active:
            tkr = (row.get("ticker") or "").strip()
            cap = row.get("market_cap") or 0
            if tkr and cap:
                out[tkr] = int(cap)
    except Exception:
        log.exception("[market_caps.kr] screener_adapter 실패")

    # 2차: FDR StockListing (screener.db 비어있을 때)
    if not out:
        try:
            import FinanceDataReader as fdr
            for market in ("KOSPI", "KOSDAQ"):
                try:
                    df = fdr.StockListing(market)
                except Exception:
                    log.warning("[market_caps.kr] FDR.StockListing(%s) 실패", market)
                    continue
                if df is None or len(df) == 0:
                    continue
                cols = list(df.columns)
                code_c = next((c for c in ("Code", "Symbol", "ticker") if c in cols), None)
                cap_c = next((c for c in ("Marcap", "MarketCap", "market_cap") if c in cols), None)
                if not code_c or not cap_c:
                    continue
                for _, r in df.iterrows():
                    code = str(r[code_c]).zfill(6)
                    cap = r[cap_c]
                    try:
                        cap_int = int(cap) if cap and not _is_nan(cap) else 0
                    except Exception:
                        cap_int = 0
                    if cap_int > 0:
                        out[code] = cap_int
        except ImportError:
            log.warning("[market_caps.kr] FinanceDataReader 미설치")
        except Exception:
            log.exception("[market_caps.kr] FDR 폴백 실패")

    _KR_CACHE = out
    _KR_CACHE_TS = now
    log.info("[market_caps.kr] %d종목 시총 확보", len(out))
    return out


def fetch_us_market_caps(tickers: list[str]) -> dict[str, int]:
    """티커 list → 시총(USD). FMP /stable/quote batch.

    무료 키 작동. 단일 호출당 ~50종목 권장 (URL 길이 안전).
    """
    global _US_CACHE, _US_CACHE_TS
    now = time.time()
    # 캐시 hit 분리
    out: dict[str, int] = {}
    miss: list[str] = []
    if (now - _US_CACHE_TS) >= _US_CACHE_TTL:
        _US_CACHE = {}
        _US_CACHE_TS = now
    for t in tickers:
        tu = (t or "").upper().strip()
        if not tu:
            continue
        if tu in _US_CACHE:
            out[tu] = _US_CACHE[tu]
        else:
            miss.append(tu)
    if not miss:
        return out

    fmp_key = os.getenv("FMP_API_KEY")
    fh_key = os.getenv("FINNHUB_API_KEY")
    if not fmp_key and not fh_key:
        log.info("[market_caps.us] FMP·Finnhub 키 모두 없음 — 캐시만 반환")
        return out

    # FMP /stable/quote 1차 시도. 무료 키는 소형/신생주에 402 Premium 차단 →
    # 그 경우 Finnhub /stock/profile2 폴백 (marketCapitalization in millions USD).
    fmp_hits = 0
    fh_hits = 0
    for sym in miss[:50]:
        cap_int = 0
        if fmp_key:
            try:
                r = requests.get(
                    f"{_FMP_BASE}/quote",
                    params={"symbol": sym, "apikey": fmp_key},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data and isinstance(data, list):
                        cap = data[0].get("marketCap")
                        if cap:
                            cap_int = int(cap)
                            fmp_hits += 1
            except Exception:
                log.exception("[market_caps.us] FMP %s fetch 실패", sym)
        # FMP miss → Finnhub 폴백
        if not cap_int and fh_key:
            try:
                r = requests.get(
                    "https://finnhub.io/api/v1/stock/profile2",
                    params={"symbol": sym, "token": fh_key},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json() or {}
                    mc_m = data.get("marketCapitalization")  # millions USD
                    if mc_m:
                        cap_int = int(float(mc_m) * 1e6)
                        fh_hits += 1
            except Exception:
                log.exception("[market_caps.us] Finnhub %s fetch 실패", sym)
        if cap_int:
            out[sym] = cap_int
            _US_CACHE[sym] = cap_int
    log.info("[market_caps.us] %d종목 확보 (FMP=%d · Finnhub=%d · miss=%d)",
             len(out), fmp_hits, fh_hits, len(miss) - len(out) + (len(tickers) - len(miss)))
    return out


def _is_nan(x) -> bool:
    try:
        return x != x
    except Exception:
        return False
