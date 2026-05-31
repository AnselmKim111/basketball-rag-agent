"""§5 어닝 모멘텀 — FMP earnings-calendar 기반 (무료 stable 엔드포인트).

이 플랜에서 사용 가능한 FMP stable 엔드포인트:
  - earnings-calendar?from&to : 과거 구간은 epsActual/epsEstimated(서프라이즈),
    미래 구간은 epsEstimated/revenueEstimated(컨센서스). symbol별 analyst-estimates는 유료.

그래서 어닝 모멘텀을 (1) 최근 발표 실적의 beat율·평균 서프라이즈, (2) 다가올 주요 실적 일정+컨센서스
두 축으로 구성한다. 대형주만(매출 규모 필터) 추려 노이즈 제거.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

log = logging.getLogger(__name__)

_BASE = "https://financialmodelingprep.com/stable"


def _calendar_window(start: str, end: str, key: str) -> list[dict]:
    """단일 ≤14일 구간 fetch (FMP 무료 플랜은 범위 캡이 있어 더 넓으면 빈 응답)."""
    import httpx
    url = f"{_BASE}/earnings-calendar?from={start}&to={end}&apikey={key}"
    last_err = None
    for _ in range(2):
        try:
            r = httpx.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            return []
        except Exception as e:
            last_err = e
    log.info("[report.earnings] 캘린더 fetch 실패(%s~%s): %s", start, end, last_err)
    return []


def _calendar(start_d: "date", end_d: "date", key: str, step_days: int = 14) -> list[dict]:
    """[start_d, end_d]를 step_days 이하 윈도로 쪼개 합침 (FMP 범위 캡 회피)."""
    out: list[dict] = []
    cur = start_d
    while cur <= end_d:
        win_end = min(cur + timedelta(days=step_days - 1), end_d)
        out.extend(_calendar_window(cur.isoformat(), win_end.isoformat(), key))
        cur = win_end + timedelta(days=1)
    return out


def fetch_earnings_momentum(days_back: int = 21, days_fwd: int = 12,
                            min_rev: float = 2e9) -> dict | None:
    """어닝 모멘텀 요약 dict. FMP 키 없거나 전부 실패면 None."""
    key = os.getenv("FMP_API_KEY")
    if not key:
        log.info("[report.earnings] FMP_API_KEY 없음 — 스킵")
        return None

    today = date.today()
    past = _calendar(today - timedelta(days=days_back), today, key)
    fwd = _calendar(today, today + timedelta(days=days_fwd), key)
    if not past and not fwd:
        return None

    # 1) 최근 발표 실적 → beat/miss
    reported = []
    for r in past:
        act, est = r.get("epsActual"), r.get("epsEstimated")
        rev = r.get("revenueActual") or r.get("revenueEstimated") or 0
        if act is None or est is None or not est or rev < min_rev:
            continue
        surprise = (act - est) / abs(est) * 100
        reported.append({"symbol": r.get("symbol"), "date": r.get("date"),
                         "eps_actual": act, "eps_est": est,
                         "surprise_pct": round(surprise, 1), "revenue": rev})
    reported.sort(key=lambda x: -x["surprise_pct"])
    n = len(reported)
    beats = sum(1 for r in reported if r["surprise_pct"] > 0)
    avg_surprise = round(sum(r["surprise_pct"] for r in reported) / n, 1) if n else None
    recent = {
        "reported": n,
        "beats": beats,
        "beat_rate": round(beats / n * 100, 1) if n else None,
        "avg_surprise_pct": avg_surprise,
        "top_beats": reported[:5],
        "top_misses": reported[-5:][::-1] if n else [],
    }

    # 2) 다가올 주요 실적 일정 + 컨센서스
    upcoming_by_sym: dict[str, dict] = {}
    # FMP fwd 우선 (대형주만, revenueEstimated ≥ min_rev)
    for r in fwd:
        est = r.get("epsEstimated")
        rev = r.get("revenueEstimated") or 0
        sym = (r.get("symbol") or "").upper()
        if not sym or est is None or rev < min_rev:
            continue
        upcoming_by_sym[sym] = {"symbol": sym, "date": r.get("date"),
                                "eps_est": est, "revenue_est": rev, "source": "FMP"}
    # Finnhub /calendar/earnings 폴백 — FMP 시뮬레이션 환경에서 1개만 오는 경우 대비.
    # eps_est 있는 종목만 추가, revenue_est 5e8 이상(중대형).
    try:
        import requests
        fh_key = os.getenv("FINNHUB_API_KEY")
        if fh_key:
            fh_url = "https://finnhub.io/api/v1/calendar/earnings"
            r = requests.get(fh_url, params={
                "from": today.isoformat(),
                "to": (today + timedelta(days=days_fwd)).isoformat(),
                "token": fh_key,
            }, timeout=15)
            if r.status_code == 200:
                items = (r.json() or {}).get("earningsCalendar") or []
                for it in items:
                    sym = (it.get("symbol") or "").upper()
                    est = it.get("epsEstimate")
                    rev = it.get("revenueEstimate") or 0
                    if not sym or est is None or rev < 5e8:
                        continue
                    if sym not in upcoming_by_sym:
                        upcoming_by_sym[sym] = {
                            "symbol": sym, "date": it.get("date"),
                            "eps_est": est, "revenue_est": rev, "source": "Finnhub",
                        }
    except Exception:
        log.exception("[report.earnings] Finnhub /calendar/earnings 폴백 실패")

    upcoming = sorted(upcoming_by_sym.values(), key=lambda x: -x["revenue_est"])[:15]

    # 3) upcoming 종목에 시총 + analyst revision 부착 (forward-looking 신호 강화)
    if upcoming:
        try:
            from src.report.data import fetch_market_caps, fetch_estimate_revisions
            syms = [u["symbol"] for u in upcoming if u.get("symbol")]
            caps = fetch_market_caps.fetch_us_market_caps(syms)
            revs = fetch_estimate_revisions.batch_revisions(syms, cap=15)
            for u in upcoming:
                sym = (u.get("symbol") or "").upper()
                if sym in caps:
                    u["market_cap"] = caps[sym]
                if sym in revs:
                    u["revision"] = revs[sym]
        except Exception:
            log.exception("[report.earnings] upcoming 보강 실패")

    log.info("[report.earnings] 최근 %d종목(beat율 %s%%) · 예정 %d종목",
             n, recent["beat_rate"], len(upcoming))
    return {"recent": recent, "upcoming": upcoming, "asof": today.isoformat()}
