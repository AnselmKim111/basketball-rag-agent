"""12M Forward consensus 수집 (best-effort).

소스 우선순위:
  1. 네이버 금융 종목 페이지 - 추정 실적 테이블
  2. (TODO) wisereport 종목 페이지 — 향후 추가

DART에는 추정 데이터가 없으므로 외부 스크래핑.
실패 시 빈 dict 반환 — chart.py가 graceful 처리.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

log = logging.getLogger(__name__)

NAVER_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/131.0.0.0 Safari/537.36",
}


def fetch(ticker: str) -> dict[str, dict[str, Optional[float]]]:
    """ticker → {"2026Q2": {"revenue": ..., "op_profit": ..., "net_profit": ...}, ...}.

    네이버 금융 추정실적 테이블 best-effort 파싱.
    실패 시 빈 dict.
    """
    try:
        return _fetch_naver(ticker)
    except Exception:
        log.exception("네이버 forward 수집 실패: %s", ticker)
        return {}


def _fetch_naver(ticker: str) -> dict[str, dict[str, Optional[float]]]:
    """네이버 금융의 종목 분석 페이지에서 분기별 컨센서스 추출.

    URL: https://finance.naver.com/item/coinfo.naver?code={ticker}
    실제 분석 데이터는 iframe (https://navercomp.wisereport.co.kr/...) 안에 있음.
    여기서는 단순 fallback — 빈 dict 반환을 기본으로 하고,
    추후 실제 파싱 로직으로 확장.
    """
    # MVP: 네이버 페이지가 iframe + JS heavy라 정확 파싱은 별도 작업 필요.
    # 일단 빈 결과 반환 → chart.py가 forward 자리 비우고 캡션으로 안내.
    log.info("forward consensus 수집 (naver) — MVP에서는 미구현, 빈 dict 반환")
    return {}
