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


def _fmt_sector(s: str | None) -> str:
    if not s:
        return ""
    # 섹터 너무 길면 줄임
    s = s.strip()
    if len(s) > 12:
        s = s[:12]
    return f" [{s}]"


def _line_simple(i: int, it: dict) -> str:
    """공통 단순 라인: '1. 삼성전자(005930) +5.2% — 75,000원, 시총 500조 [반도체]'."""
    chg = it.get("chg_pct", 0.0)
    return (
        f"{i}. {_fmt_label(it)} {_fmt_pct(chg)} — "
        f"{_fmt_money(it['close'])}원{_fmt_cap(it.get('market_cap'))}"
        f"{_fmt_sector(it.get('sector'))}"
    )


def _format_section(items: list[dict], emoji: str, title: str) -> str:
    """KOSPI(top_n_kospi) + KOSDAQ(top_n_kosdaq) 분리 섹션. 단순 라인."""
    kospi, kosdaq = _split_by_market(items)
    n_kospi = _per_category_top_kospi()
    n_kosdaq = _per_category_top_kosdaq()
    head = f"━━━ {emoji} {title} (KOSPI {len(kospi)}, KOSDAQ {len(kosdaq)}) ━━━\n"

    if not kospi and not kosdaq:
        return head + "해당 없음\n"

    out = [head]
    out.append("📍 유가증권시장 (KOSPI)")
    if kospi:
        for i, it in enumerate(kospi[:n_kospi], 1):
            out.append(_line_simple(i, it))
        rest = len(kospi) - n_kospi
        if rest > 0:
            out.append(f"... 외 {rest}종목")
    else:
        out.append("해당 없음")
    out.append("")
    out.append("📍 코스닥 (KOSDAQ)")
    if kosdaq:
        for i, it in enumerate(kosdaq[:n_kosdaq], 1):
            out.append(_line_simple(i, it))
        rest = len(kosdaq) - n_kosdaq
        if rest > 0:
            out.append(f"... 외 {rest}종목")
    else:
        out.append("해당 없음")
    return "\n".join(out) + "\n"


def _sector_summary(items: list[dict], top_n: int = 5) -> str:
    """섹터별 종목 수 집계 (KOSPI+KOSDAQ 통합)."""
    if not items:
        return ""
    counts: dict[str, int] = {}
    for it in items:
        s = (it.get("sector") or "").strip() or "기타"
        counts[s] = counts.get(s, 0) + 1
    if not counts:
        return ""
    sorted_items = sorted(counts.items(), key=lambda x: -x[1])[:top_n]
    return ", ".join(f"{name}({n})" for name, n in sorted_items)


def format_results(results: dict[str, list[dict]], as_of: datetime) -> str:
    parts: list[str] = []
    parts.append(f"🔔 한국 주식 기술적 신호 ({as_of.strftime('%Y-%m-%d %H:%M KST')})")
    parts.append("(시총 3000억원 이상 · 유가증권시장 우선 · 시총·상승률 복합 정렬)\n")

    # 섹터 요약 (모든 신호 통합)
    all_signals: list[dict] = []
    for v in results.values():
        all_signals.extend(v)
    sec_summary = _sector_summary(all_signals)
    if sec_summary:
        parts.append(f"🏷️ 주요 섹터: {sec_summary}\n")

    parts.append(_format_section(results.get("high_all", []), "🚀", "역사적 신고가"))
    parts.append(_format_section(results.get("high_52w", []), "📈", "52주 신고가"))
    parts.append(_format_section(results.get("volume_breakout", []), "🔥", "거래량 돌파 ≥2배"))
    parts.append(_format_section(results.get("near_breakout_52w", []), "🎯", "52주 돌파 직전 (95-99%)"))

    total = sum(len(v) for v in results.values())
    if total == 0:
        parts.append("\n오늘은 신호 발생 종목이 없습니다.")

    return "\n".join(parts)
