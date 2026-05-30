"""가격 데이터 fetch — FDR DataReader (raw DataFrame, 차트용).

us_screener.data_source는 cent 단위 INTEGER 변환을 거치지만, 리포트 차트는
raw OHLCV DataFrame이 편하므로 FDR DataReader를 직접 호출한다. Stooq 폴백.

미국 지수/ETF 심볼: ^GSPC(S&P500) ^IXIC(나스닥) ^DJI(다우) ^RUT(러셀2000),
  ETF QQQ/SPY/RSP/IWM/EEM/EWY/SMH/SOXX 등.
한국: 6자리 종목코드 or 지수(FDR 'KS11'=KOSPI, 'KQ11'=KOSDAQ).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

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


def fetch_many(tickers: dict[str, str], days: int = 365, workers: int = 8) -> dict[str, object]:
    """{label: ticker} → {label: DataFrame}. 실패 종목은 제외. FDR 병렬 fetch."""
    out: dict[str, object] = {}
    if not tickers:
        return out

    def _one(item):
        label, tk = item
        try:
            df = fetch_ohlcv(tk, days=days)
        except Exception:
            df = None
        return label, df

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tickers)))) as ex:
        for label, df in ex.map(_one, tickers.items()):
            if df is not None and len(df) > 5:
                out[label] = df
    return out


def pct_return(df, n: int) -> float | None:
    """최근 n 영업일 수익률(%). 데이터 부족 시 None."""
    if df is None or "Close" not in df or len(df) < n + 1:
        return None
    close = df["Close"]
    base = float(close.iloc[-(n + 1)])
    last = float(close.iloc[-1])
    if not base:
        return None
    return (last / base - 1) * 100


def day_change(df) -> float | None:
    """전일 대비 등락률(%)."""
    return pct_return(df, 1)


def normalize_100(df):
    """Close를 첫 값=100으로 리베이스한 Series 반환 (normalized 비교용)."""
    if df is None or "Close" not in df or len(df) < 2:
        return None
    close = df["Close"].dropna()
    base = float(close.iloc[0])
    if not base:
        return None
    return close / base * 100


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

# §2 쏠림 둔화 시그널 비교군 (시총가중 vs 동일가중 vs M7)
DECONCENTRATION = {
    "SPY": "SPY",      # 시총가중 S&P500
    "RSP": "RSP",      # 동일가중 S&P500 (메인 시그널)
    "QQQ": "QQQ",      # 나스닥100
    "MAGS": "MAGS",    # Magnificent 7 바스켓
}

# §3-1 미국 11개 GICS 섹터 ETF
US_SECTOR_ETFS = {
    "기술(XLK)": "XLK",
    "금융(XLF)": "XLF",
    "헬스케어(XLV)": "XLV",
    "에너지(XLE)": "XLE",
    "임의소비재(XLY)": "XLY",
    "필수소비재(XLP)": "XLP",
    "산업재(XLI)": "XLI",
    "유틸리티(XLU)": "XLU",
    "소재(XLB)": "XLB",
    "부동산(XLRE)": "XLRE",
    "커뮤니케이션(XLC)": "XLC",
}

# §3-1 테마/스타일 ETF (자금 확산처 추적)
US_THEME_ETFS = {
    "반도체(SMH)": "SMH",
    "반도체(SOXX)": "SOXX",
    "우주(UFO)": "UFO",
    "사이버보안(CIBR)": "CIBR",
    "로봇/AI(BOTZ)": "BOTZ",
    "양자컴(QTUM)": "QTUM",
    "방산(ITA)": "ITA",
    "방산(PPA)": "PPA",
    "태양광(TAN)": "TAN",
    "신재생(ICLN)": "ICLN",
    "원전/우라늄(URA)": "URA",
    "원자력(NLR)": "NLR",
    "리튬/2차전지(LIT)": "LIT",
    "바이오(XBI)": "XBI",
    "바이오(IBB)": "IBB",
    "M7(MAGS)": "MAGS",
    "사모펀드(PSP)": "PSP",
    "고배당(SCHD)": "SCHD",
    "고배당(DVY)": "DVY",
    "가치주(IWD)": "IWD",
    "성장주(IWF)": "IWF",
    "소매(XRT)": "XRT",
}

# §3-2 글로벌 지역 ETF
REGION_ETFS = {
    "미국(SPY)": "SPY",
    "유로존(EZU)": "EZU",
    "신흥국(EEM)": "EEM",
    "한국(EWY)": "EWY",
    "일본(EWJ)": "EWJ",
    "중국(MCHI)": "MCHI",
}

# §4 환율/달러
FX_SYMBOLS = {
    "달러인덱스(DXY)": "DX-Y.NYB",
    "USD/KRW": "USD/KRW",
}

# §6 스토리 개별 종목
HIGHLIGHT_STOCKS = {
    "Dell(DELL)": "DELL",
    "D-Wave(QBTS)": "QBTS",
    "Eli Lilly(LLY)": "LLY",
    "Ford(F)": "F",
    "Qualcomm(QCOM)": "QCOM",
    "MetaVia(MTVA)": "MTVA",
    "NVIDIA(NVDA)": "NVDA",
    "Micron(MU)": "MU",
    "AMD(AMD)": "AMD",
}

KR_INDICES = {
    "KOSPI": "KS11",
    "KOSDAQ": "KQ11",
    "KOSPI200": "KS200",
}
