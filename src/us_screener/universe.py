"""미국 유니버스 빌드 — 미국 상장 보통주 중 시총 ≥ US_SCREENER_MIN_MARKET_CAP 전체.

2026-09-25 재설계 (사용자: "기준 이상 시총 종목은 빠짐없이"):
  - 본체: Nasdaq screener (NYSE/NASDAQ/AMEX 전 종목 + 시총·섹터) → 보통주 필터 → 시총 ≥ $1B
    (~2,550종목). 이전 S&P500+NASDAQ100(516, 시총 컬럼 없음·상장폐지 종목 잔존)은 폴백으로 강등.
  - 지수 라벨: S&P500/NASDAQ100 멤버는 market 컬럼에 라벨, 나머지는 "US".
  - 섹터: S&P500은 FDR GICS 유지(기존 표시와 일관), 나머지는 Nasdaq sector.
  - 목록에서 빠진 종목은 is_active=0 (ANSS·AVB 등 상장폐지가 매일 '누락'으로 잡히던 문제).
    단 Nasdaq 응답이 비정상적으로 작으면(부분 응답) 비활성화하지 않는다.
한국 src/screener/universe.py와 동일 인터페이스(refresh_universe / refresh_market_caps /
get_universe_tickers).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

from src.us_screener import data_source, db

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# Nasdaq 응답 정상성 기준 (미달이면 폴백 + 비활성화 금지)
MIN_SANE_RAW_ROWS = 5000      # screener 원본 행 수 (평소 ~7,000)
MIN_SANE_UNIVERSE = 1500      # 직전 활성 수가 이 이상일 때만 급감 비율 검사
MAX_DAILY_SHRINK = 0.85       # 하루에 활성 종목이 15% 넘게 줄면 부분 응답으로 간주


def _min_cap() -> float:
    for key in ("US_SCREENER_MIN_MARKET_CAP",):
        try:
            v = os.getenv(key, "")
            if v:
                return float(v)
        except ValueError:
            pass
    return 1_000_000_000.0


def _index_members() -> tuple[dict[str, tuple], dict[str, str]]:
    """(S&P500+NASDAQ100 {symbol: (symbol, name, label)}, S&P500 GICS {symbol: sector})."""
    members = {sym: (sym, nm, label) for sym, nm, label in data_source.fetch_us_tickers()}
    try:
        gics = data_source.fetch_sectors()
    except Exception:
        log.exception("[us_universe] GICS sector fetch 실패")
        gics = {}
    return members, gics


def refresh_universe() -> int:
    """유니버스 갱신 → DB upsert (+시총·섹터) + 이탈 종목 비활성화. 반환: 활성 종목 수."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    min_cap = _min_cap()
    members, gics = _index_members()
    nasdaq = data_source.fetch_nasdaq_universe(min_cap, always_include=set(members))
    raw_n = data_source.nasdaq_screener_raw_count()   # 같은 캐시 응답 기준 (재요청 없음)
    # 정상 판정: 원본 응답 규모(평소 ~7,000행) + '마지막 정상 갱신'(같은 시총 기준) 대비 급감 없음.
    # 현재 활성 수와 비교하면 폴백 후 활성 수가 굳어 영구 폴백되고, 시총 기준을 올리면 다시는
    # 통과하지 못한다 (리뷰 지적) → meta에 저장한 마지막 정상 {min_cap, count}와 비교.
    import json as _json
    try:
        last = _json.loads(db.meta_get("us_universe_last_sane") or "{}")
    except (ValueError, TypeError):
        last = {}
    same_basis = last.get("min_cap") == min_cap and last.get("count", 0) >= MIN_SANE_UNIVERSE
    sane = raw_n >= MIN_SANE_RAW_ROWS and bool(nasdaq) and (
        not same_basis or len(nasdaq) >= last["count"] * MAX_DAILY_SHRINK)
    if not sane and nasdaq:
        log.warning("[us_universe] Nasdaq 응답 비정상 (raw=%d, 필터후=%d, 마지막 정상=%s) — 폴백",
                    raw_n, len(nasdaq), last)
    if sane and not members:
        # 지수 멤버 목록(FDR) 실패 시 시총 공란 클래스주(BF.A 등)가 always_include를 잃는다 →
        # 비활성화는 건너뛰고 upsert만 (리뷰 지적)
        log.warning("[us_universe] 지수 멤버 목록 실패 — 이번 갱신은 비활성화 생략")

    if sane:
        rows: list[tuple] = []
        secs: dict[str, str] = {}
        for it in nasdaq:
            t = it["ticker"]
            label = members[t][2] if t in members else "US"
            # 이름: S&P500(FDR)은 짧고 깔끔한 이름 보유 → 우선
            name = members[t][1] if t in members and members[t][1] != t else it["name"]
            rows.append((t, name or t, label, 1, today, it["market_cap"]))
            sec = gics.get(t) or it.get("sector") or ""
            if sec:
                secs[t] = sec
        db.upsert_tickers(rows)
        if secs:
            db.update_sectors(secs)
        active = {r[0] for r in rows}
        deact = db.deactivate_missing(active) if members else 0
        db.meta_set("us_universe_last_sane", _json.dumps({"min_cap": min_cap, "count": len(rows)}))
        dropped_members = sorted(set(members) - active)
        log.info(
            "[us_universe] %d종목 활성화 (Nasdaq 보통주 시총≥$%.1fB, 지수멤버 %d 포함, 비활성화 %d)",
            len(rows), min_cap / 1e9, len(set(members) & active), deact,
        )
        if dropped_members:
            # 지수 멤버인데 목록에 없음 = 상장폐지·합병·심볼변경 (또는 시총 기준 미달)
            log.info("[us_universe] 지수 멤버 중 제외 %d: %s", len(dropped_members),
                     ", ".join(dropped_members[:40]))
        return len(rows)

    # ---- 폴백: S&P500 + NASDAQ100 (Nasdaq screener 실패/부분 응답)
    log.warning("[us_universe] Nasdaq 유니버스 사용 불가(%d종목) — 기존 활성 유지 + S&P500+NASDAQ100 "
                "보강, 비활성화 생략", len(nasdaq))
    if not members:
        log.error("[us_universe] ticker list 비어있음 — FDR/네트워크 점검")
        return 0
    caps: dict[str, int] = {}
    try:
        caps = data_source.fetch_market_caps()
    except Exception:
        log.exception("[us_universe] market_cap fetch 실패 — 시총 없이 진행")
    rows = [(sym, nm or sym, label, 1, today, caps.get(sym)) for sym, nm, label in members.values()]
    # 이미 비활성화된 종목(상장폐지 등)은 되살리지 않음 — 신규만 추가, 기존은 시총만 갱신 (리뷰 지적)
    db.upsert_tickers_keep_active(rows)
    if gics:
        db.update_sectors(gics)
    log.info("[us_universe] %d종목 활성화 (폴백 S&P500+NASDAQ100, 시총 %d, 섹터 %d)",
             len(rows), sum(1 for r in rows if r[5]), len(gics))
    return len(rows)


def refresh_market_caps() -> int:
    """시총 + 섹터만 갱신 (universe rebuild 없이)."""
    updated = 0
    try:
        caps = data_source.fetch_market_caps()
        if caps:
            updated = db.update_market_caps(caps)
    except Exception:
        log.exception("[us_universe] market_cap fetch 실패")
    try:
        secs = data_source.fetch_sectors()
        if secs:
            db.update_sectors(secs)
            log.info("[us_universe] sector 갱신 %d종목", len(secs))
    except Exception:
        log.exception("[us_universe] sector fetch 실패")
    return updated


def get_universe_tickers() -> list[dict]:
    items = db.get_active_tickers()
    if not items:
        refresh_universe()
        items = db.get_active_tickers()
    return items
