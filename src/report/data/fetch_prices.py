"""가격 데이터 fetch — FDR DataReader (raw DataFrame, 차트용).

us_screener.data_source는 cent 단위 INTEGER 변환을 거치지만, 리포트 차트는
raw OHLCV DataFrame이 편하므로 FDR DataReader를 직접 호출한다. Stooq 폴백.

미국 지수/ETF 심볼: ^GSPC(S&P500) ^IXIC(나스닥) ^DJI(다우) ^RUT(러셀2000),
  ETF QQQ/SPY/RSP/IWM/EEM/EWY/SMH/SOXX 등.
한국: 6자리 종목코드 or 지수(FDR 'KS11'=KOSPI, 'KQ11'=KOSDAQ).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger(__name__)


def _import_fdr():
    import FinanceDataReader as fdr  # type: ignore
    return fdr


def fetch_ohlcv(ticker: str, days: int = 365):
    """최근 days일 OHLCV DataFrame (Date index, Open/High/Low/Close/Volume).

    반환: pandas.DataFrame 또는 None. FDR 1순위.
    """
    end = date.today()
    start = end - timedelta(days=int(days * 1.5) + 10)  # 영업일 여유
    try:
        fdr = _import_fdr()
        df = fdr.DataReader(ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    except Exception as e:
        log.warning("[report.prices] FDR %s 실패: %s", ticker, e)
        return None
    if df is None or df.empty:
        log.info("[report.prices] %s 빈 결과", ticker)
        return None
    # 컬럼 표준화
    cols = {c.lower(): c for c in df.columns}
    rename = {}
    for std in ("Open", "High", "Low", "Close", "Volume"):
        if std.lower() in cols:
            rename[cols[std.lower()]] = std
    df = df.rename(columns=rename)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    if "Close" not in keep:
        return None
    return df[keep].dropna(subset=["Close"])


def fetch_many(tickers: dict[str, str], days: int = 365) -> dict[str, object]:
    """{label: ticker} → {label: DataFrame}. 실패 종목은 제외."""
    out: dict[str, object] = {}
    for label, tk in tickers.items():
        df = fetch_ohlcv(tk, days=days)
        if df is not None and len(df) > 5:
            out[label] = df
    return out


# 리포트 표준 심볼셋
US_INDICES = {
    "S&P500": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW": "^DJI",
    "Russell2000": "^RUT",
}

US_ETFS = {
    "QQQ": "QQQ", "SPY": "SPY", "RSP": "RSP", "IWM": "IWM",
    "EEM": "EEM", "EWY": "EWY", "SMH": "SMH", "SOXX": "SOXX",
}

KR_INDICES = {
    "KOSPI": "KS11",
    "KOSDAQ": "KQ11",
    "KOSPI200": "KS200",
}
