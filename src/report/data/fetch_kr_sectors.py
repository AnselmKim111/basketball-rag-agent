"""한국 섹터 ETF — 1M·5D 상대강도. FDR로 6자리 ticker fetch.

대표 ETF (시뮬레이션 환경 안정 작동 확인된 종목):
  091160 = KODEX 반도체
  449450 = TIGER K-방위산업
  305540 = TIGER 2차전지테마
  442320 = KODEX K-신재생에너지액티브
  091180 = KODEX 자동차
  144200 = KODEX 헬스케어
  152100 = KODEX 코스피200 (벤치마크)
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

KR_SECTOR_ETFS: dict[str, str] = {
    "반도체": "091160",
    "방산": "449450",
    "2차전지": "305540",
    "신재생": "442320",
    "자동차": "091180",
    "헬스케어": "144200",
    "코스피200": "152100",
}


def _pct_return(df, n: int) -> Optional[float]:
    if df is None or len(df) <= n:
        return None
    try:
        return (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-1 - n]) - 1) * 100
    except Exception:
        return None


def fetch_kr_sector_strength() -> list[dict]:
    """[{label, ticker, r1d, r5d, r1m, r3m}] — fetch 실패 종목은 skip."""
    from src.report.data.fetch_prices import fetch_ohlcv
    out: list[dict] = []
    for label, code in KR_SECTOR_ETFS.items():
        try:
            df = fetch_ohlcv(code, days=120)
        except Exception:
            log.warning("[kr_sectors] fetch %s 실패", code)
            continue
        if df is None or len(df) < 25:
            continue
        out.append({
            "label": f"{label}({code})",
            "ticker": code,
            "r1d": _pct_return(df, 1),
            "r5d": _pct_return(df, 5),
            "r1m": _pct_return(df, 20),
            "r3m": _pct_return(df, 60),
        })
    log.info("[kr_sectors] %d섹터 확보", len(out))
    return out
