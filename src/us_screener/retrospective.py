"""신호 후속 수익률 회고 (미장) — KR `src/screener/retrospective.py`와 동일 로직, DB만 us_screener."""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from src.us_screener import db
from src.screener_common.sanity import US_DAILY_HI, US_DAILY_LO, series_is_continuous

log = logging.getLogger(__name__)


def signal_returns(
    days_back: int = 10,
    days_ahead: int = 5,
    categories: Optional[tuple] = None,
    today_iso: Optional[str] = None,
) -> dict:
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
            closes = db.closes_from_date(ticker, date_iso, days_ahead)
        except Exception:
            continue
        if len(closes) < days_ahead + 1:
            continue
        c0, cN = closes[0], closes[days_ahead]
        if not c0 or not cN or c0 <= 0:
            continue
        if not series_is_continuous(closes, US_DAILY_LO, US_DAILY_HI):
            log.warning("[retrospective] %s %s 스케일 브레이크 — 표본 제외", ticker, date_iso)
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
    if not retro:
        return ""
    parts = [f"🔁 <b>지난 회고</b> (신호일 기준 {days_ahead}영업일 후)"]
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
            from src.bot_helpers import html_escape
            line += f" — 최고 {html_escape(str(t[1]))} {t[2]:+}%"
        parts.append(line)
    return "\n".join(parts) + "\n"
