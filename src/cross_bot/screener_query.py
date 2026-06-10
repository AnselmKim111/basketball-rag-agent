"""ScreenerBot DB read-only 헬퍼 — IdeaBot/RecapBot 등에서 안전 import.

ScreenerBot의 SQLite (`/data/screener.db`, 280일 retention)에 들어있는:
  - tickers: 시총·섹터·시장(KOSPI/KOSDAQ) — universe 정확
  - fundamentals: ticker → eps_yoy
  - signals: 최근 N일 기술적 신호 (52w 신고가/VCP/거래량 돌파 등)

호출자는 ScreenerBot이 가동 안 됐어도, DB 파일 없어도, 가입 전이어도 graceful —
빈 결과 반환 + 로그만. import 자체는 lazy (모듈 로드 시 screener 의존 안 함).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger(__name__)


def is_available() -> bool:
    """ScreenerBot DB가 활용 가능한지 빠른 체크. False면 호출자는 skip."""
    try:
        from src.screener import db as _db
        _db.ensure_schema()
        return _db.row_count() > 0
    except Exception:
        return False


def get_universe() -> list[dict]:
    """KOSPI/KOSDAQ 활성 종목 dict 리스트.

    각 항목: {ticker, name, market, market_cap, sector}. market_cap은 원 단위(int) 또는 None.
    DB 없으면 빈 리스트 (대량 fetch 없이 즉시 반환).
    """
    try:
        from src.screener import db as _db
        return _db.get_active_tickers()
    except Exception:
        log.info("[cross_bot] screener.db 미가용 — universe 빈 리스트")
        return []


def _sector_match(filter_s: str, row_s: str) -> bool:
    """양방향 + 첫 단어 substring 매칭.

    sect 필터링은 IdeaBot에서 "반도체 장비"/"반도체 테스트" 같은 sub-sector
    label을 보내는 반면, screener.db의 sector는 보통 "반도체" 같은 generic.
    단방향 substring("반도체 테스트" in "반도체"=False)이면 0건이 나옴.

    매칭 룰 (대소문자 무시 가정 — 호출자가 lower로 전달):
      1) filter ⊆ row  ("반도체" ⊆ "반도체/IT부품")
      2) row ⊆ filter  ("반도체" ⊆ "반도체 테스트")     ← 역방향
      3) filter의 첫 단어 ⊆ row  ("반도체 테스트" → "반도체")  ← head 매칭
    """
    if not filter_s or not row_s:
        return False
    if filter_s in row_s:
        return True
    if row_s in filter_s:
        return True
    head = filter_s.split()[0] if filter_s.split() else filter_s
    if head and head in row_s:
        return True
    return False


def get_universe_filtered(
    market_cap_max_won: Optional[int] = None,
    market_cap_min_won: Optional[int] = None,
    sectors: Optional[list[str]] = None,
    markets: Optional[list[str]] = None,
) -> list[dict]:
    """universe를 시총·섹터·시장으로 필터링.

    인자는 모두 옵션 (None 또는 빈 리스트면 해당 필터 무시).
    sectors/markets는 substring 매칭 (대소문자 무시).
    """
    rows = get_universe()
    if not rows:
        return []
    out: list[dict] = []
    sect_lower = [s.lower() for s in (sectors or []) if s]
    mkt_lower = [m.lower() for m in (markets or []) if m]
    for r in rows:
        cap = r.get("market_cap")
        if market_cap_max_won is not None and (cap is None or cap > market_cap_max_won):
            continue
        if market_cap_min_won is not None and (cap is None or cap < market_cap_min_won):
            continue
        if sect_lower:
            sect = (r.get("sector") or "").lower()
            if not any(_sector_match(s, sect) for s in sect_lower):
                continue
        if mkt_lower:
            mkt = (r.get("market") or "").lower()
            if not any(m in mkt for m in mkt_lower):
                continue
        out.append(r)
    return out


def get_recent_signals(days_back: int = 14) -> dict[str, list[dict]]:
    """ticker → 그 종목의 최근 N일 signal hit 리스트.

    각 hit: {date, signal, payload}. signal 종류: high_all/high_52w/volume_breakout/
    near_breakout_52w/vcp_breakout (screener.signals.compute_all 출력).
    DB 없으면 빈 dict.
    """
    try:
        from src.screener import db as _db
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=days_back)).isoformat()
        rows = _db.load_signals_in_range(start, end)
    except Exception:
        log.info("[cross_bot] screener.db signals 미가용")
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        t = r.get("ticker")
        if not t:
            continue
        out.setdefault(t, []).append({
            "date": r.get("date"),
            "signal": r.get("signal"),
            "payload": r.get("payload") or {},
        })
    return out


def get_signal_summary(ticker: str, days_back: int = 14) -> dict:
    """단일 ticker의 최근 N일 신호 요약.

    반환: {
      hit_count: int — 신호 hit 총 횟수,
      signals: list[str] — 발생한 신호 종류 (unique),
      latest_date: str|None — 가장 최근 hit 날짜,
      latest_signal: str|None,
    }
    """
    by_t = get_recent_signals(days_back)
    hits = by_t.get(ticker, [])
    if not hits:
        return {"hit_count": 0, "signals": [], "latest_date": None, "latest_signal": None}
    sigs: list[str] = sorted({h["signal"] for h in hits if h.get("signal")})
    # date는 ISO 형식이라 lexical sort = chronological
    latest = max(hits, key=lambda h: h.get("date") or "")
    return {
        "hit_count": len(hits),
        "signals": sigs,
        "latest_date": latest.get("date"),
        "latest_signal": latest.get("signal"),
    }


def get_eps_yoy(ticker: str) -> Optional[float]:
    """fundamentals 테이블의 eps_yoy. 없으면 None."""
    try:
        from src.screener import db as _db
        row = _db.fundamentals_get(ticker, max_age_days=365 * 5)  # idea 분석은 stale도 허용
        if row:
            return row.get("eps_yoy")
        return None
    except Exception:
        return None


def get_ticker_name(ticker: str) -> Optional[str]:
    """screener.db tickers 테이블 회사명. DART corp_map과 보조 lookup용."""
    try:
        from src.screener import db as _db
        return _db.get_ticker_name(ticker)
    except Exception:
        return None
