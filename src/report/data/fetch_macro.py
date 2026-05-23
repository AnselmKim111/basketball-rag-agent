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
    from src.report.data.fetch_prices import fetch_many
    out = fetch_many(MACRO_SYMBOLS, days=days)
    log.info("[report.macro] %d/%d 지표 확보", len(out), len(MACRO_SYMBOLS))
    return out


# FRED 시계열 (API 키 없이 fredgraph.csv 직접). 미국 egress 불확실 → best-effort.
FRED_SERIES = {
    "미시간 1년 기대인플레": "MICH",
    "신규 실업수당청구(주)": "ICSA",
}


def fetch_fred(series_id: str, days: int = 1825):
    """FRED fredgraph.csv → DataFrame(Date index, Close). 실패 시 None.

    egress가 느릴 수 있어 타임아웃 30s + 재시도(backoff).
    """
    import io
    import time
    from datetime import date, timedelta
    last_err = None
    for attempt in range(3):
        try:
            import httpx
            import pandas as pd
            start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
            url = (f"https://fred.stlouisfed.org/graph/fredgraph.csv"
                   f"?id={series_id}&cosd={start}")
            r = httpx.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            # 컬럼: observation_date(또는 DATE), <series_id>
            date_col = df.columns[0]
            val_col = df.columns[1]
            df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
            df = df.dropna(subset=[val_col])
            df.index = pd.to_datetime(df[date_col])
            out = df[[val_col]].rename(columns={val_col: "Close"})
            if len(out) < 2:
                return None
            return out
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    log.info("[report.macro] FRED %s 미확보: %s", series_id, last_err)
    return None


def fetch_fred_many() -> dict[str, object]:
    """FRED_SERIES 일괄 fetch. 미확보 자동 제외."""
    out: dict[str, object] = {}
    for label, sid in FRED_SERIES.items():
        df = fetch_fred(sid)
        if df is not None:
            out[label] = df
    log.info("[report.macro] FRED %d/%d 확보", len(out), len(FRED_SERIES))
    return out
