"""매크로 지표 fetch — 금리/유가/달러/변동성 (FDR DataReader).

심볼 (Yahoo backed):
  금리: ^TNX(10Y) ^FVX(5Y) ^TYX(30Y) ^IRX(13주)
  유가: CL=F(WTI) BZ=F(Brent)
  달러: DX-Y.NYB (달러 인덱스)
  변동성: ^VIX (주식), ^OVX (유가 변동성, 가능 시), ^MOVE (채권, 대개 미제공)
  위험선호: BTC-USD, ETH-USD
미확보 심볼은 graceful skip.
"""
from __future__ import annotations

import logging

from src.report.data.fetch_prices import fetch_ohlcv

log = logging.getLogger(__name__)

MACRO_SYMBOLS = {
    "미국 10년물 금리": "^TNX",
    "미국 2년물 금리": "^IRX",
    "미국 30년물 금리": "^TYX",
    "WTI 유가": "CL=F",
    "Brent 유가": "BZ=F",
    "달러 인덱스": "DX-Y.NYB",
    "VIX": "^VIX",
    "유가 변동성(OVX)": "^OVX",
    "비트코인": "BTC-USD",
}


def fetch_macro(days: int = 180) -> dict[str, object]:
    """매크로 지표 {label: DataFrame}. 미확보 심볼은 자동 제외."""
    out: dict[str, object] = {}
    for label, sym in MACRO_SYMBOLS.items():
        df = fetch_ohlcv(sym, days=days)
        if df is not None and len(df) > 5:
            out[label] = df
        else:
            log.info("[report.macro] %s(%s) 미확보 — 생략", label, sym)
    log.info("[report.macro] %d/%d 지표 확보", len(out), len(MACRO_SYMBOLS))
    return out
