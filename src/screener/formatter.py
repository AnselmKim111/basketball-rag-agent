"""신호 결과 → 텔레그램 메시지.

길이 제어:
  - 카테고리당 Top N (기본 30, env SCREENER_PER_CATEGORY_TOP)
  - 20일/60일 신고가는 종목명 콤마 구분 요약
  - 최종 텍스트는 send_text_chunked가 4000자 청크로 분할
"""
from __future__ import annotations

import os
from datetime import datetime


def _per_category_top() -> int:
    try:
        return max(1, int(os.getenv("SCREENER_PER_CATEGORY_TOP", "30")))
    except ValueError:
        return 30


def _fmt_money(v: float | int) -> str:
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _fmt_pct(v: float, signed: bool = True) -> str:
    sign = "+" if v > 0 and signed else ""
    return f"{sign}{v:.1f}%"


def _fmt_label(item: dict) -> str:
    return f"{item.get('name', item['ticker'])}({item['ticker']})"


def _section_high(items: list[dict], emoji: str, title: str, top_n: int) -> str:
    if not items:
        return f"━━━ {emoji} {title} (0) ━━━\n해당 없음\n"
    head = f"━━━ {emoji} {title} ({len(items)}) ━━━\n"
    lines = []
    for i, it in enumerate(items[:top_n], 1):
        lines.append(
            f"{i}. {_fmt_label(it)} — {_fmt_money(it['close'])}원 "
            f"({_fmt_pct(it['pct'])}, 직전고점 {_fmt_money(it['prev_high'])})"
        )
    rest = len(items) - top_n
    if rest > 0:
        lines.append(f"... 외 {rest}종목")
    return head + "\n".join(lines) + "\n"


def _section_ichimoku(items: list[dict], top_n: int) -> str:
    if not items:
        return "━━━ 📊 일목 구름 상방 돌파 (0) ━━━\n해당 없음\n"
    head = f"━━━ 📊 일목 구름 상방 돌파 ({len(items)}) ━━━\n"
    lines = []
    for i, it in enumerate(items[:top_n], 1):
        lines.append(
            f"{i}. {_fmt_label(it)} — {_fmt_money(it['close'])}원 "
            f"(구름상단 {_fmt_money(it['cloud_top'])}, {_fmt_pct(it['pct_above'])} 위)"
        )
    rest = len(items) - top_n
    if rest > 0:
        lines.append(f"... 외 {rest}종목")
    return head + "\n".join(lines) + "\n"


def _section_volume(items: list[dict], top_n: int) -> str:
    if not items:
        return "━━━ 🔥 거래량 돌파 (0) ━━━\n해당 없음\n"
    head = f"━━━ 🔥 거래량 돌파 ≥2배 ({len(items)}) ━━━\n"
    lines = []
    for i, it in enumerate(items[:top_n], 1):
        lines.append(
            f"{i}. {_fmt_label(it)} — {_fmt_money(it['close'])}원 "
            f"({_fmt_pct(it['chg_pct'])}, 거래량 {it['vol_ratio']:.1f}배)"
        )
    rest = len(items) - top_n
    if rest > 0:
        lines.append(f"... 외 {rest}종목")
    return head + "\n".join(lines) + "\n"


def _section_near(items: list[dict], top_n: int) -> str:
    if not items:
        return "━━━ 🎯 52주 돌파 직전 (0) ━━━\n해당 없음\n"
    head = f"━━━ 🎯 52주 돌파 직전 95-99% ({len(items)}) ━━━\n"
    lines = []
    for i, it in enumerate(items[:top_n], 1):
        lines.append(
            f"{i}. {_fmt_label(it)} — {_fmt_money(it['close'])}원 "
            f"(52주고점 {_fmt_money(it['hi52'])}의 {it['proximity_pct']:.1f}%, "
            f"vol↑{it['vol_trend']:.1f}배)"
        )
    rest = len(items) - top_n
    if rest > 0:
        lines.append(f"... 외 {rest}종목")
    return head + "\n".join(lines) + "\n"


def _section_compact(items: list[dict], emoji: str, title: str, max_chars: int = 1500) -> str:
    if not items:
        return f"━━━ {emoji} {title} (0) ━━━\n해당 없음\n"
    head = f"━━━ {emoji} {title} ({len(items)}) ━━━\n"
    names: list[str] = []
    used = 0
    for it in items:
        s = _fmt_label(it)
        if used + len(s) + 2 > max_chars:
            names.append("...")
            break
        names.append(s)
        used += len(s) + 2
    return head + ", ".join(names) + "\n"


def format_results(results: dict[str, list[dict]], as_of: datetime) -> str:
    top_n = _per_category_top()
    parts: list[str] = []
    parts.append(f"🔔 한국 주식 기술적 신호 ({as_of.strftime('%Y-%m-%d %H:%M KST')})\n")

    parts.append(_section_high(results.get("high_52w", []), "📈", "52주 신고가", top_n))
    parts.append(_section_ichimoku(results.get("ichimoku_breakout", []), top_n))
    parts.append(_section_volume(results.get("volume_breakout", []), top_n))
    parts.append(_section_near(results.get("near_breakout_52w", []), top_n))
    parts.append(_section_compact(results.get("high_60", []), "📈", "60일 신고가"))
    parts.append(_section_compact(results.get("high_20", []), "📈", "20일 신고가"))

    total = sum(len(v) for v in results.values())
    if total == 0:
        parts.append("\n오늘은 신호 발생 종목이 없습니다.")

    return "\n".join(parts)
