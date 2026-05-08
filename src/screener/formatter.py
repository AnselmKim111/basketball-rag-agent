"""신호 결과 → 텔레그램 메시지.

길이 제어:
  - 카테고리당 Top N (기본 30, env SCREENER_PER_CATEGORY_TOP)
  - 20일/60일 신고가는 종목명 콤마 구분 요약
  - 최종 텍스트는 send_text_chunked가 4000자 청크로 분할

KOSPI(유가증권시장) / KOSDAQ 분리 출력. KOSPI 위주 (top_n_kospi >= top_n_kosdaq).
"""
from __future__ import annotations

import os
from datetime import datetime


def _per_category_top_kospi() -> int:
    try:
        return max(1, int(os.getenv("SCREENER_PER_CATEGORY_TOP_KOSPI", "30")))
    except ValueError:
        return 30


def _per_category_top_kosdaq() -> int:
    try:
        return max(1, int(os.getenv("SCREENER_PER_CATEGORY_TOP_KOSDAQ", "10")))
    except ValueError:
        return 10


def _fmt_money(v: float | int) -> str:
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _fmt_pct(v: float, signed: bool = True) -> str:
    sign = "+" if v > 0 and signed else ""
    return f"{sign}{v:.1f}%"


def _fmt_cap(cap: int | None) -> str:
    if cap is None or cap <= 0:
        return ""
    if cap >= 1_000_000_000_000:  # 1조+
        return f", 시총 {cap / 1e12:.1f}조"
    return f", 시총 {cap / 1e8:.0f}억"


def _fmt_label(item: dict) -> str:
    return f"{item.get('name', item['ticker'])}({item['ticker']})"


def _split_by_market(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kospi, kosdaq) 두 리스트로 분리. market 정보 없는 건 KOSPI로."""
    kospi: list[dict] = []
    kosdaq: list[dict] = []
    for it in items:
        m = (it.get("market") or "").upper()
        if m == "KOSDAQ":
            kosdaq.append(it)
        else:
            kospi.append(it)
    return kospi, kosdaq


def _line_high(i: int, it: dict) -> str:
    return (
        f"{i}. {_fmt_label(it)} — {_fmt_money(it['close'])}원 "
        f"({_fmt_pct(it['pct'])}, 직전고점 {_fmt_money(it['prev_high'])}{_fmt_cap(it.get('market_cap'))})"
    )


def _line_ichimoku(i: int, it: dict) -> str:
    return (
        f"{i}. {_fmt_label(it)} — {_fmt_money(it['close'])}원 "
        f"(구름상단 {_fmt_money(it['cloud_top'])}, {_fmt_pct(it['pct_above'])} 위{_fmt_cap(it.get('market_cap'))})"
    )


def _line_volume(i: int, it: dict) -> str:
    return (
        f"{i}. {_fmt_label(it)} — {_fmt_money(it['close'])}원 "
        f"({_fmt_pct(it['chg_pct'])}, 거래량 {it['vol_ratio']:.1f}배{_fmt_cap(it.get('market_cap'))})"
    )


def _line_near(i: int, it: dict) -> str:
    return (
        f"{i}. {_fmt_label(it)} — {_fmt_money(it['close'])}원 "
        f"(52주고점 {_fmt_money(it['hi52'])}의 {it['proximity_pct']:.1f}%, "
        f"vol↑{it['vol_trend']:.1f}배{_fmt_cap(it.get('market_cap'))})"
    )


def _format_section(
    items: list[dict],
    emoji: str,
    title: str,
    line_func,
) -> str:
    """KOSPI(top_n_kospi) + KOSDAQ(top_n_kosdaq) 분리 섹션."""
    kospi, kosdaq = _split_by_market(items)
    n_kospi = _per_category_top_kospi()
    n_kosdaq = _per_category_top_kosdaq()
    head = f"━━━ {emoji} {title} (KOSPI {len(kospi)}, KOSDAQ {len(kosdaq)}) ━━━\n"

    if not kospi and not kosdaq:
        return head + "해당 없음\n"

    out = [head]
    # KOSPI 우선
    out.append(f"📍 유가증권시장 (KOSPI)")
    if kospi:
        for i, it in enumerate(kospi[:n_kospi], 1):
            out.append(line_func(i, it))
        rest = len(kospi) - n_kospi
        if rest > 0:
            out.append(f"... 외 {rest}종목")
    else:
        out.append("해당 없음")
    out.append("")
    out.append(f"📍 코스닥 (KOSDAQ)")
    if kosdaq:
        for i, it in enumerate(kosdaq[:n_kosdaq], 1):
            out.append(line_func(i, it))
        rest = len(kosdaq) - n_kosdaq
        if rest > 0:
            out.append(f"... 외 {rest}종목")
    else:
        out.append("해당 없음")
    return "\n".join(out) + "\n"


def _format_section_compact(
    items: list[dict], emoji: str, title: str, max_chars: int = 1500
) -> str:
    """20일/60일 신고가는 짧게 — KOSPI/KOSDAQ 라벨만 분리."""
    kospi, kosdaq = _split_by_market(items)
    head = f"━━━ {emoji} {title} (KOSPI {len(kospi)}, KOSDAQ {len(kosdaq)}) ━━━\n"

    def _names(items: list[dict]) -> str:
        if not items:
            return "해당 없음"
        names: list[str] = []
        used = 0
        for it in items:
            s = _fmt_label(it)
            if used + len(s) + 2 > max_chars:
                names.append("...")
                break
            names.append(s)
            used += len(s) + 2
        return ", ".join(names)

    return (
        head
        + f"📍 KOSPI: {_names(kospi)}\n"
        + f"📍 KOSDAQ: {_names(kosdaq)}\n"
    )


def format_results(results: dict[str, list[dict]], as_of: datetime) -> str:
    parts: list[str] = []
    parts.append(f"🔔 한국 주식 기술적 신호 ({as_of.strftime('%Y-%m-%d %H:%M KST')})")
    parts.append("(시총 3000억원 이상 · 유가증권시장 우선)\n")

    parts.append(_format_section(results.get("high_52w", []), "📈", "52주 신고가", _line_high))
    parts.append(_format_section(results.get("ichimoku_breakout", []), "📊", "일목 구름 상방 돌파", _line_ichimoku))
    parts.append(_format_section(results.get("volume_breakout", []), "🔥", "거래량 돌파 ≥2배", _line_volume))
    parts.append(_format_section(results.get("near_breakout_52w", []), "🎯", "52주 돌파 직전 95-99%", _line_near))
    parts.append(_format_section_compact(results.get("high_60", []), "📈", "60일 신고가"))
    parts.append(_format_section_compact(results.get("high_20", []), "📈", "20일 신고가"))

    total = sum(len(v) for v in results.values())
    if total == 0:
        parts.append("\n오늘은 신호 발생 종목이 없습니다.")

    return "\n".join(parts)
