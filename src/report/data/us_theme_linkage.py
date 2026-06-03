"""미국 테마 → 핵심 종목 hardcode 매핑.

us_screener.db sector_leader 카테고리 폴백. 시뮬환경에서 cron 데이터 없을 때
watchlist가 hot 테마와 매핑된 핵심 종목으로 후보 풀 보강.

매핑 기준: 미국 대형주 (Yahoo OHLCV 안정 fetch, FMP/Finnhub 시총 fetch 가능).
ETF가 아닌 개별 종목 — 사용자가 매매 가능한 단위.
"""
from __future__ import annotations

# 테마 키워드(label substring 매칭) → 핵심 종목 list
US_THEME_TICKERS: dict[str, list[str]] = {
    "반도체": ["NVDA", "AMD", "AVGO", "MU"],
    "사이버": ["CRWD", "PANW", "ZS"],
    "양자": ["IBM", "IONQ", "RGTI"],
    "우주": ["BA", "LMT", "NOC", "RTX"],
    "태양광": ["FSLR", "ENPH", "SEDG"],
    "신재생": ["FSLR", "NEE", "ENPH"],
    "방산": ["LMT", "NOC", "RTX", "GD"],
    "로봇": ["ABB", "TER", "ROK"],
    "AI": ["NVDA", "MSFT", "GOOGL", "META"],
    "기술": ["MSFT", "AAPL", "NVDA", "GOOGL"],
    "바이오": ["MRNA", "REGN", "VRTX"],
    "M7": ["MSFT", "AAPL", "NVDA", "GOOGL", "AMZN", "META", "TSLA"],
    "MAGS": ["MSFT", "AAPL", "NVDA", "GOOGL", "AMZN", "META", "TSLA"],
}


def match_theme_keyword(label: str) -> str | None:
    """theme label (예: '반도체(SOXX)') → 매칭되는 테마 키워드."""
    if not label:
        return None
    for kw in US_THEME_TICKERS.keys():
        if kw in label:
            return kw
    return None


def core_tickers_for_label(label: str) -> list[str]:
    """label → 핵심 미국 종목 ticker list (최대 4개)."""
    kw = match_theme_keyword(label)
    if not kw:
        return []
    return US_THEME_TICKERS[kw][:4]
