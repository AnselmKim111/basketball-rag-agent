"""미국 신호 결과 → 텔레그램 메시지 (미미 스타일, 영문 종목).

한국 src/screener/formatter.py 미러. 차이:
  - 헤더: "미국 주식 기술적 신호"
  - 시장 라벨: KOSPI/KOSDAQ → S&P500/NASDAQ100
  - 종가: cent 단위 정수 저장 → $ 표기 (÷100). 종목명(등락률)만 표시이므로 종가 직접 표기는 없음.
  - 섹터: 미국 GICS (영문, FDR 제공)
"""
from __future__ import annotations

import os
from datetime import datetime


def _per_category_top() -> int:
    try:
        return max(1, int(os.getenv("US_SCREENER_PER_CATEGORY_TOP", "80")))
    except ValueError:
        return 80


def _fmt_pct(v: float, signed: bool = True) -> str:
    sign = "+" if v > 0 and signed else ""
    return f"{sign}{v:.1f}%"


def _fmt_label_compact(item: dict) -> str:
    """단순 라벨: 'AAPL(+5.2%)' — 미국은 심볼이 곧 식별자."""
    sym = item.get("ticker", "")
    chg = item.get("chg_pct", 0.0)
    return f"{sym}({_fmt_pct(chg)})"


def _market_rank(item: dict) -> int:
    """S&P500 0, 그 외(NASDAQ100) 1 — 대형 인덱스 우선 정렬용."""
    m = (item.get("market") or "").upper()
    return 0 if m == "S&P500" else 1


def _group_by_sector(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for it in items:
        sec = (it.get("sector") or "").strip() or "Other"
        if sec not in groups:
            groups[sec] = []
            order.append(sec)
        groups[sec].append(it)
    for sec in groups:
        groups[sec].sort(key=lambda it: (_market_rank(it),))
    sorted_secs = sorted(order, key=lambda s: (-len(groups[s]), order.index(s)))
    return [(s, groups[s]) for s in sorted_secs]


def _fmt_pct_or_na(v) -> str:
    return _fmt_pct(v) if isinstance(v, (int, float)) else "N/A"


def _format_section(items: list[dict], emoji: str, title: str,
                    links: dict | None = None, extra: dict | None = None,
                    seen: set | None = None) -> str:
    """4열 평면 포맷 (티커 / 당일 / 연초대비 / EPS YoY). 티커는 채널 게시물로 하이퍼링크.

    seen: 앞 섹션에서 이미 표시된 티커 집합 — 중복 제거(앞에 나온 종목은 뒷 섹션서 제외).
    """
    links = links or {}
    extra = extra or {}
    seen = seen if seen is not None else set()
    items = [it for it in items if it.get("ticker") not in seen]
    head = f"━━━ {emoji} {title} ({len(items)}) ━━━"
    if not items:
        return head + "\n해당 없음\n"
    cap = _per_category_top()
    items_sorted = sorted(items, key=lambda it: -(it.get("chg_pct") or 0.0))
    for it in items_sorted:
        seen.add(it.get("ticker"))
    items_capped = items_sorted[:cap]
    rest = len(items) - len(items_capped)
    lines = [head, "(티커 / 당일 / 연초대비 / EPS YoY)"]
    for it in items_capped:
        sym = it.get("ticker", "")
        chg = _fmt_pct(it.get("chg_pct", 0.0))
        ex = extra.get(sym, {})
        ytd = _fmt_pct_or_na(ex.get("ytd"))
        eps = _fmt_pct_or_na(ex.get("eps_yoy"))
        url = links.get(sym)
        sym_disp = f"[{sym}]({url})" if url else sym
        lines.append(f"{sym_disp} / {chg} / {ytd} / {eps}")
    if rest > 0:
        lines.append(f"... 외 {rest}종목")
    return "\n".join(lines) + "\n"


def _sector_summary(items: list[dict], top_n: int = 6) -> str:
    if not items:
        return ""
    counts: dict[str, int] = {}
    for it in items:
        s = (it.get("sector") or "").strip() or "Other"
        counts[s] = counts.get(s, 0) + 1
    if not counts:
        return ""
    sorted_items = sorted(counts.items(), key=lambda x: -x[1])[:top_n]
    return ", ".join(f"{name}({n})" for name, n in sorted_items)


_KO_WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"]


def _fmt_kst_header(dt: datetime) -> str:
    return dt.strftime("%Y.%m.%d.") + f"({_KO_WEEKDAY[dt.weekday()]}) " + dt.strftime("%H:%M KST")


def format_results(
    results: dict[str, list[dict]],
    as_of: datetime,
    base_date: str | None = None,
    stats: dict | None = None,
    links: dict | None = None,
    extra: dict | None = None,
) -> str:
    parts: list[str] = []
    parts.append(f"🇺🇸 미국 주식 기술적 신호 — {_fmt_kst_header(as_of)}")
    if base_date:
        as_of_iso = as_of.strftime("%Y-%m-%d")
        if base_date != as_of_iso:
            parts.append(f"📊 기준일: {base_date} (미국 장마감 종가)")
        else:
            parts.append(f"📊 기준일: {base_date} 당일 종가")
    if stats:
        proc = stats.get("processed", 0)
        skipped_no_base = stats.get("skipped_no_base", 0)
        line = f"✓ {proc}종목 신호 계산 (시총 $1B+)"
        if skipped_no_base > 0:
            line += f" · {skipped_no_base}종목 base_date 데이터 누락"
        parts.append(line)

    # 앞에 나온 종목은 뒷 섹션서 제외 (역사적 신고가 → 52주 신고가 순으로 dedup)
    seen: set = set()
    parts.append(_format_section(results.get("high_all", []), "🚀", "역사적 신고가", links, extra, seen))
    parts.append(_format_section(results.get("high_52w", []), "📈", "52주 신고가", links, extra, seen))
    parts.append(_format_section(results.get("vcp_breakout", []), "💎", "VCP 돌파 (최근 1주 이내)", links, extra, seen))
    parts.append(_format_section(results.get("near_breakout_52w", []), "🎯", "52주 돌파 직전 95-99%", links, extra, seen))

    total = sum(len(results.get(k, [])) for k in
                ("high_all", "high_52w", "vcp_breakout", "near_breakout_52w"))
    if total == 0:
        parts.append("\n오늘은 신호 발생 종목이 없습니다.")

    return "\n".join(parts)
