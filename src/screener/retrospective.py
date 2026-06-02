"""신호 후속 수익률 회고 — DB의 signals 히스토리로 "지난주 신호 종목 평균 수익률" 집계.

목적: 사용자가 신호의 신뢰도를 체감할 수 있게 매일 메시지 상단에 회고 한 줄 표시.
예: "🔁 지난주 회고: 역사적 신고가 12종목 평균 +4.2% (8승/4패, 최고 OO +47%)".

캐시 안 함(매일 1회, 비용 작음). 외부 의존 없이 SQLite만.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from src.screener import db

log = logging.getLogger(__name__)


def signal_returns(
    days_back: int = 10,
    days_ahead: int = 5,
    categories: Optional[tuple] = None,
    today_iso: Optional[str] = None,
) -> dict:
    """days_back 영업일 전 신호 종목들의 days_ahead 후 수익률 집계.

    Args:
        days_back: 회고 대상 신호의 최대 경과 영업일. 너무 길면 그동안 신호 많아 노이즈,
            짧으면 표본 부족 — 10이 기본.
        days_ahead: 신호일 기준 N영업일 후의 종가로 수익률 계산. 5(=1주).
        categories: ("high_all", "high_52w") 등 집계할 카테고리 필터. None이면 전체.
        today_iso: 오늘 날짜(YYYY-MM-DD) — 오늘 발생한 신호는 N영업일 미경과라 제외.

    Returns:
        {category: {n, beats, avg_return_pct, top: [(ticker, name, ret), ...], bottom: [...]}}
        실패·표본 0이면 빈 dict.
    """
    try:
        sigs = db.recent_signals(days_back=days_back + days_ahead + 5,
                                 exclude_date=today_iso)
    except Exception:
        log.exception("[retrospective] recent_signals 실패")
        return {}

    by_cat: dict[str, list[tuple]] = defaultdict(list)
    for s in sigs:
        sig = s.get("signal") or ""
        if categories and sig not in categories:
            continue
        date_iso = s.get("date")
        ticker = s.get("ticker")
        if not date_iso or not ticker:
            continue
        try:
            c0 = db.close_after_n_business_days(ticker, date_iso, 0)
            cN = db.close_after_n_business_days(ticker, date_iso, days_ahead)
        except Exception:
            continue
        if not c0 or not cN or c0 <= 0:
            continue
        ret = (cN - c0) / c0 * 100.0
        name = (s.get("payload") or {}).get("name") or ticker
        by_cat[sig].append((ticker, name, round(ret, 1)))

    out: dict[str, dict] = {}
    for cat, items in by_cat.items():
        if not items:
            continue
        rets = [r for _, _, r in items]
        beats = sum(1 for r in rets if r > 0)
        items_sorted = sorted(items, key=lambda x: -x[2])
        out[cat] = {
            "n": len(items),
            "beats": beats,
            "avg_return_pct": round(sum(rets) / len(rets), 1),
            "top": items_sorted[:3],
            "bottom": items_sorted[-3:][::-1] if len(items_sorted) > 3 else [],
        }
    return out


_CAT_LABEL = {
    "high_all": "역사적 신고가",
    "high_52w": "52주 신고가",
    "vcp_breakout": "VCP 돌파",
    "near_breakout_52w": "52주 돌파 직전",
}


def format_retrospective_line(retro: dict, days_ahead: int = 5) -> str:
    """retro dict → 메시지에 박을 한·두 줄 요약 문자열. 빈 dict면 빈 문자열."""
    if not retro:
        return ""
    parts = [f"🔁 <b>지난 회고</b> (신호일 기준 {days_ahead}영업일 후)"]
    # 카테고리 표시 순서
    order = ("high_all", "high_52w", "vcp_breakout", "near_breakout_52w")
    for cat in order:
        d = retro.get(cat)
        if not d or d["n"] == 0:
            continue
        label = _CAT_LABEL.get(cat, cat)
        avg = d["avg_return_pct"]
        sign = "+" if avg >= 0 else ""
        line = f"  · {label} {d['n']}종목 평균 {sign}{avg}% ({d['beats']}승/{d['n']-d['beats']}패)"
        if d.get("top"):
            t = d["top"][0]
            line += f" — 최고 {t[1]} {t[2]:+}%"
        parts.append(line)
    return "\n".join(parts) + "\n"
