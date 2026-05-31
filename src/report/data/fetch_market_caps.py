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

    key = os.getenv("FMP_API_KEY")
    if not key:
        log.info("[market_caps.us] FMP_API_KEY 없음 — 캐시만 반환")
        return out

    # FMP /stable/quote는 단일 symbol 호출. batch 위해 ?symbol= 콤마 미지원 →
    # 종목별 호출. 14개 정도면 ~5초.
    for sym in miss[:50]:  # 안전 cap
        try:
            r = requests.get(
                f"{_FMP_BASE}/quote",
                params={"symbol": sym, "apikey": key},
                timeout=10,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            if not data or not isinstance(data, list):
                continue
            cap = data[0].get("marketCap")
            if cap:
                cap_int = int(cap)
                out[sym] = cap_int
                _US_CACHE[sym] = cap_int
        except Exception:
            log.exception("[market_caps.us] %s fetch 실패", sym)
    log.info("[market_caps.us] %d종목 시총 확보 (miss=%d)", len(out), len(miss) - len(out))
    return out


def _is_nan(x) -> bool:
    try:
        return x != x
    except Exception:
        return False
