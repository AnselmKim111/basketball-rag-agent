"""주간 회고 데이터 수집 — RecapBot의 4섹션(+1 sonnet 합성) 입력 빌더.

각 collect_* 함수는 LLM 없이 DB·캐시·기존 헬퍼만 사용. sonnet 합성은 호출자
(recap_bot._run_recap)에서 한 번 호출.

섹션 키 (sonnet 입력 dict):
  signal_stats   — scorer.compute_signal_stats 결과
  signal_summary — scorer.overall_summary
  ideas          — 지난 주 idea_cache entry들의 Top 5 picks + 사후 가격
  themes         — 산업 키워드 빈도 (지난 7일 vs 전 7일 비교)
  disclosures    — 빈 placeholder (DisclosureBot 데이터 영속 안 되어 follow-up)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# §1. 시그널 사후 추적 (scorer 입력 빌드)
# ------------------------------------------------------------------
def _close_lookup(ohlcv: list[dict]) -> dict[str, float]:
    """load_ohlcv 결과 (ascending) → {date: close} map."""
    return {row["date"]: float(row["close"]) for row in ohlcv if row.get("close")}


def _horizon_close(ohlcv: list[dict], hit_date: str, horizon_days: int = 5) -> tuple[float, str]:
    """ohlcv ascending에서 hit_date 인덱스 +horizon_days 위치 close.

    horizon이 데이터 끝 너머면 가장 최근 close 반환 (graceful — D+5 데이터 없는 신규
    hit는 D+今 사용해 부분 평가 가능).
    반환: (close, date_str). 못 찾으면 (0.0, "").
    """
    if not ohlcv:
        return 0.0, ""
    dates = [r["date"] for r in ohlcv]
    if hit_date not in dates:
        return 0.0, ""
    idx = dates.index(hit_date)
    target = min(idx + horizon_days, len(ohlcv) - 1)
    if target <= idx:
        return 0.0, ""  # hit_date가 데이터 마지막일 — horizon 0
    row = ohlcv[target]
    return float(row.get("close") or 0.0), row.get("date", "")


def collect_signal_section(
    today: date, lookback_days: int = 7, horizon_days: int = 5,
    ticker_filter: set[str] | None = None,
) -> dict[str, Any]:
    """signals 테이블 lookback 기간 hit → TickerHit → SignalStats.

    오늘 기준 lookback_days 전까지의 signals 행을 모두 가져와 ticker별 ohlcv에서
    hit_date close + (hit_date + horizon_days영업일) close 룩업해 PnL 계산.

    ticker_filter 지정 시 그 종목들만 (/recap_me — 사용자 watchlist 한정).
    """
    from src.recap.scorer import TickerHit, compute_signal_stats, overall_summary
    from src.screener import db
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = today.isoformat()
    try:
        rows = db.load_signals_in_range(start, end)
    except Exception:
        log.exception("[recap] signals load 실패")
        rows = []

    if ticker_filter is not None:
        rows = [r for r in rows if r["ticker"] in ticker_filter]

    if not rows:
        reason = (
            "watchlist 종목 중 최근 7일 signal hit 0건"
            if ticker_filter is not None
            else "최근 7일 signal hit 0건 — screener_daily 실행 여부 확인"
        )
        return {
            "stats": [],
            "summary": overall_summary([]),
            "lookback_start": start,
            "lookback_end": end,
            "horizon_days": horizon_days,
            "empty_reason": reason,
        }

    # ticker → name 매핑: 헤비 ticker는 get_ticker_name (caching) 활용. 매번 SELECT
    # 한 번이지만 RLock + SQLite이라 빠르고, 풀로드 시 메모리 비용도 작음.
    ticker_to_name: dict[str, str] = {}

    def _name_of(t: str) -> str:
        if t in ticker_to_name:
            return ticker_to_name[t]
        try:
            ticker_to_name[t] = db.get_ticker_name(t) or ""
        except Exception:
            ticker_to_name[t] = ""
        return ticker_to_name[t]

    hits: list[TickerHit] = []
    for row in rows:
        ticker = row["ticker"]
        signal = row["signal"]
        hit_date = row["date"]
        payload = row.get("payload") or {}
        hit_close = float(payload.get("close") or 0.0)
        # payload에 close 없으면 ohlcv lookup 보강 — hit 시점 close
        if not hit_close:
            try:
                latest = db.load_ohlcv(ticker, days=lookback_days + horizon_days + 5)
                hit_close = _close_lookup(latest).get(hit_date, 0.0)
            except Exception:
                log.exception("[recap] hit_close load 실패: %s", ticker)
        # horizon close — 전용 SQL 헬퍼 사용 (ohlcv 풀로드 회피)
        horizon_close = 0.0
        horizon_date = ""
        try:
            hc = db.close_after_n_business_days(ticker, hit_date, horizon_days)
            if hc is not None:
                horizon_close = float(hc)
                # horizon_date는 hit_date + N영업일 — DB 함수에 노출 안 됨, 빈 string 유지.
        except Exception:
            log.exception("[recap] horizon_close 조회 실패: %s", ticker)
        hits.append(TickerHit(
            ticker=ticker,
            name=_name_of(ticker),
            signal=signal,
            hit_date=hit_date,
            hit_close=hit_close,
            horizon_close=horizon_close,
            horizon_date=horizon_date,
        ))

    stats = compute_signal_stats(hits)
    return {
        "stats": stats,
        "summary": overall_summary(stats),
        "lookback_start": start,
        "lookback_end": end,
        "horizon_days": horizon_days,
        "total_hits_raw": len(rows),
    }


# ------------------------------------------------------------------
# §2. IdeaBot picks 후속
# ------------------------------------------------------------------
def collect_ideas_section(days_back: int = 7) -> dict[str, Any]:
    """idea_cache.find_recent_in_range 호출 → 각 entry의 synthesis.top5 종목 메타.

    가격 fetch는 호출자(또는 Phase 2 작업)가 수행. 본 함수는 entry 메타·picks 목록만
    구성 — 메타·picks 둘 다 sonnet 합성 입력에 그대로 들어감.
    """
    from src import idea_cache
    try:
        entries = idea_cache.find_recent_in_range(days_back=days_back)
    except Exception:
        log.exception("[recap] idea_cache 로드 실패")
        entries = []

    summarized: list[dict] = []
    for ent in entries:
        top5 = (ent.get("synthesis") or {}).get("top5") or []
        picks = [
            {
                "name": pick.get("name") or "?",
                "ticker": pick.get("ticker6") or pick.get("ticker") or "",
                "thesis_brief": (pick.get("thesis") or pick.get("reason") or "")[:200],
            }
            for pick in top5[:5]
        ]
        summarized.append({
            "id": ent.get("id", ""),
            "created_at": ent.get("created_at", ""),
            "idea_text": (ent.get("idea_text") or "")[:200],
            "picks": picks,
        })
    return {
        "entries": summarized,
        "entry_count": len(summarized),
        "days_back": days_back,
    }


# ------------------------------------------------------------------
# §3. 공시 트리거 — disclosure_log (DisclosureBot이 알림마다 영속)
# ------------------------------------------------------------------
def collect_disclosure_section(
    today: date | None = None, days_back: int = 7,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """지난 N일 공시 알림 로그 + 공시 이후 가격 변동.

    chat_id 지정 시 그 사용자가 받은 알림만 (/recap_me). 미지정 시 전체 공시
    rcept_no 단위 dedup.

    각 로그에 pnl_since_alert(%) 부가 — baseline_close(알림 시점) vs 최신 close.
    baseline 없거나 ohlcv 미보유면 None (메시지에서 'n/a' 표시).
    """
    from src.screener import db
    if today is None:
        from src.bot_helpers import KST
        from datetime import datetime
        today = datetime.now(KST).date()
    start = (today - timedelta(days=days_back)).isoformat()
    end = today.isoformat()
    try:
        logs = db.load_disclosures_in_range(start, end, chat_id=chat_id)
    except Exception:
        log.exception("[recap] disclosure_log 로드 실패")
        return {"logs": [], "empty_reason": "disclosure_log 조회 실패 — 로그 확인"}

    if not logs:
        return {
            "logs": [],
            "empty_reason": (
                "지난 7일 공시 알림 0건 — DisclosureBot /watch 등록 종목이 없거나 "
                "공시가 없었음. 데이터는 알림마다 자동 누적 중."
            ),
        }

    # 공시 이후 가격 변동 — baseline_close vs 최신 close (best effort)
    enriched: list[dict] = []
    latest_close_cache: dict[str, float | None] = {}
    for row in logs:
        ticker = row["ticker"]
        if ticker not in latest_close_cache:
            try:
                ohlcv = db.load_ohlcv(ticker, days=1)
                latest_close_cache[ticker] = float(ohlcv[-1]["close"]) if ohlcv else None
            except Exception:
                latest_close_cache[ticker] = None
        latest = latest_close_cache[ticker]
        baseline = row.get("baseline_close")
        pnl = None
        if baseline and latest and baseline > 0:
            pnl = (latest - baseline) / baseline * 100.0
        enriched.append({**row, "pnl_since_alert": pnl})

    return {
        "logs": enriched,
        "log_count": len(enriched),
        "days_back": days_back,
    }


# ------------------------------------------------------------------
# §4. 테마 사이클 (산업 키워드 빈도)
# ------------------------------------------------------------------
def _extract_industry_hits(text: str) -> set[str]:
    """제목·종목명 텍스트에서 industry_catalog의 정식 산업명·alias 매칭 발견.

    한 텍스트에 여러 산업이 잡힐 수 있어 set 반환. industry_catalog._norm
    (공백·구분자 제거 소문자)을 그대로 사용해 줄임말·신조어도 같이 잡힘.
    """
    from src import industry_catalog as ic
    if not text:
        return set()
    norm_text = ic._norm(text)
    if not norm_text:
        return set()
    hit_names: set[str] = set()
    for entry in ic.load_catalog():
        name = entry.get("name_ko", "")
        aliases = entry.get("aliases") or []
        for token in [name] + list(aliases):
            tn = ic._norm(token)
            if tn and tn in norm_text:
                hit_names.add(name)
                break
    return hit_names


def collect_theme_section(days_back: int = 14) -> dict[str, Any]:
    """curator로 시황·전략 리포트 메타 수집 → 산업 키워드 빈도 집계.

    days_back=14: 지난 7일 vs 그 전 7일 비교로 떠오르는·식는 테마 도출.
    wisereport 접근이라 PIPELINE_LOCK 안에서 호출자가 가져야 함.
    """
    from datetime import datetime
    from src.bot_helpers import KST
    from src.curator import MARKET_MODE, _collect_pool_blocking

    try:
        pool = _collect_pool_blocking(MARKET_MODE, days_back=days_back)
    except Exception:
        log.exception("[recap] curator pool 수집 실패")
        return {"recent": {}, "previous": {}, "rising": [], "fading": [], "empty_reason": "wisereport pool 수집 실패"}

    today = datetime.now(KST).date()
    mid_cutoff = today - timedelta(days=days_back // 2)

    recent: dict[str, int] = {}
    previous: dict[str, int] = {}
    for c in pool:
        title = (getattr(c.item, "title", "") or "") + " " + (getattr(c.item, "stock_name", "") or "")
        date_str = getattr(c.item, "date_short", "") or getattr(c.item, "date", "") or ""
        # date_str 추정 (YYYY-MM-DD 또는 YYYYMMDD)
        is_recent = True
        if date_str:
            try:
                if len(date_str) == 10 and "-" in date_str:
                    d = datetime.strptime(date_str, "%Y-%m-%d").date()
                elif len(date_str) == 8:
                    d = datetime.strptime(date_str, "%Y%m%d").date()
                else:
                    d = mid_cutoff  # fallback recent
                is_recent = d >= mid_cutoff
            except ValueError:
                pass
        bucket = recent if is_recent else previous
        for ind in _extract_industry_hits(title):
            bucket[ind] = bucket.get(ind, 0) + 1

    # 떠오르는: recent count - previous count 가장 큰 순.
    rising = sorted(
        ({"industry": k, "recent": recent.get(k, 0), "previous": previous.get(k, 0),
          "delta": recent.get(k, 0) - previous.get(k, 0)}
         for k in set(recent) | set(previous)),
        key=lambda x: x["delta"], reverse=True,
    )[:8]
    fading = sorted(rising, key=lambda x: x["delta"])[:5]

    return {
        "recent": recent,
        "previous": previous,
        "rising": rising,
        "fading": fading,
        "days_back": days_back,
    }


# ------------------------------------------------------------------
# 통합 — sonnet 합성 입력 dict
# ------------------------------------------------------------------
def build_recap_input(
    today: date, lookback_days: int = 7,
    skip_themes: bool = False,
) -> dict[str, Any]:
    """4섹션 수집 → sonnet 입력 dict 한 번에.

    skip_themes=True: 테스트 / wisereport unavailable 시 §4 빈 dict로.
    """
    sig_section = collect_signal_section(today, lookback_days=lookback_days)
    ideas_section = collect_ideas_section(days_back=lookback_days)
    disc_section = collect_disclosure_section()
    if skip_themes:
        theme_section = {"recent": {}, "previous": {}, "rising": [], "fading": [], "skipped": True}
    else:
        theme_section = collect_theme_section(days_back=lookback_days * 2)
    return {
        "today": today.isoformat(),
        "lookback_days": lookback_days,
        "signal": sig_section,
        "ideas": ideas_section,
        "disclosures": disc_section,
        "themes": theme_section,
    }
