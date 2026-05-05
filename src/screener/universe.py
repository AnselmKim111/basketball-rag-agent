"""KOSPI/KOSDAQ 보통주 유니버스 빌드.

필터 정책:
  - 우선주 제외: 일반적으로 끝자리가 5/7/9 (보통주는 0). 100% 정확하진 않으나
    pykrx의 stock_name으로 '우' 또는 '우선' 포함 종목 같이 정규식 보강.
  - SPAC 제외: 이름에 '스팩' 또는 'SPAC' 포함
  - ETF 제외: KOSPI/KOSDAQ 일반주 리스트만 받으므로 자동 제외 (pykrx)
  - 관리종목/거래정지: pykrx api로 별도 추출 가능하나 빈도 낮아 생략.
    신호 계산 시 len(df) < 60 가드로 자연 제외됨.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

from src.screener import data_source, db

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 제외 패턴
_EXCLUDE_NAME_PAT = re.compile(
    r"(스팩|SPAC|우선주|\b우\b|리츠|REIT|인프라|선박투자|특수목적|기업인수목적)",
    re.IGNORECASE,
)

# 우선주 휴리스틱 (끝자리 5/7/9이고 보통주가 같은 prefix로 존재할 가능성 높음)
_PREFERRED_SUFFIX = re.compile(r"[579]$")


def _is_preferred_or_special(ticker: str, name: str, all_prefixes: set[str]) -> bool:
    if _EXCLUDE_NAME_PAT.search(name or ""):
        return True
    # 우선주: 005935 같은 패턴 — 005930(보통주) 존재 시 5번째 자리 5/7/9 종목 제외
    if len(ticker) == 6 and ticker[5] in ("5", "7", "9"):
        prefix = ticker[:5] + "0"
        if prefix in all_prefixes and prefix != ticker:
            return True
    return False


def refresh_universe() -> int:
    """KRX에서 ticker 리스트 fetch → 필터 → DB upsert.
    반환: 활성 종목 수.
    """
    raw = data_source.fetch_kospi_kosdaq_tickers()
    if not raw:
        log.error("[universe] ticker list 비어있음 — 네트워크/KRX 점검")
        return 0

    # 보통주 prefix 인덱스 (005930, 000660, ...)
    all_codes = {t for t, _, _ in raw}
    prefixes = {t[:5] + "0" for t in all_codes if len(t) == 6}

    today = datetime.now(KST).strftime("%Y-%m-%d")
    rows: list[tuple] = []
    for ticker, name, market in raw:
        if _is_preferred_or_special(ticker, name, prefixes):
            continue
        rows.append((ticker, name or ticker, market, 1, today))

    db.upsert_tickers(rows)
    log.info(
        "[universe] %d/%d 종목 활성화 (KOSPI+KOSDAQ 보통주)",
        len(rows), len(raw),
    )
    return len(rows)


def get_universe_tickers() -> list[dict]:
    """DB에서 활성 종목 dict 리스트. 비어있으면 refresh 후 다시."""
    items = db.get_active_tickers()
    if not items:
        refresh_universe()
        items = db.get_active_tickers()
    return items
