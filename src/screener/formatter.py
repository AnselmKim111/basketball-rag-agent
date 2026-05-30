"""신호 결과 → 텔레그램 메시지.

미미 스타일 포맷 (https://t.me/mimi_ATH 참고):
  - 각 신호 카테고리(역사적 신고가/52주 신고가/거래량 돌파/52주 돌파 직전)
    안에서 섹터별로 그룹핑
  - 라인: "(섹터명) 종목명1(+5.2%), 종목명2(+3.1%), ..."
  - 시총·구름상단·거래량배수 등 디테일은 헤더 1줄로만 안내
  - KOSPI 우선 정렬 (각 섹터 내부)
  - 최종 텍스트는 send_text_chunked가 4000자 청크로 분할
"""
from __future__ import annotations

import os
from datetime import datetime


def _per_category_top() -> int:
    """카테고리당 표시 한도 (기본 80, env SCREENER_PER_CATEGORY_TOP)."""
    try:
        return max(1, int(os.getenv("SCREENER_PER_CATEGORY_TOP", "80")))
    except ValueError:
        return 80


def _fmt_pct(v: float, signed: bool = True) -> str:
    sign = "+" if v > 0 and signed else ""
    return f"{sign}{v:.1f}%"


def _fmt_label_compact(item: dict) -> str:
    """미미 스타일 단순 라벨: '삼성전자(+5.2%)'."""
    name = item.get("name") or item.get("ticker", "")
    chg = item.get("chg_pct", 0.0)
    return f"{name}({_fmt_pct(chg)})"


def _market_rank(item: dict) -> int:
    """KOSPI 0, 그 외 1 — 정렬용."""
    m = (item.get("market") or "").upper()
    return 0 if m != "KOSDAQ" else 1


def _group_by_sector(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """섹터별 그룹화. 섹터 비어있으면 '기타'.

    각 섹터 내부는 KOSPI 우선 → 시총·상승률 복합 정렬은 입력 그대로 유지.
    섹터 순서: 종목 수가 많은 섹터 먼저 + 동률이면 첫 등장 순.
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for it in items:
        sec = (it.get("sector") or "").strip() or "기타"
        if sec not in groups:
            groups[sec] = []
            order.append(sec)
        groups[sec].append(it)

    # 섹터별 KOSPI 우선 정렬 (시총·상승률 정렬은 signals.py에서 이미 됨)
    for sec in groups:
        groups[sec].sort(key=lambda it: (_market_rank(it),))

    # 섹터 순서: 종목 수 desc + 첫 등장 순
    sorted_secs = sorted(order, key=lambda s: (-len(groups[s]), order.index(s)))
    return [(s, groups[s]) for s in sorted_secs]


def _fmt_pct_or_na(v) -> str:
    return _fmt_pct(v) if isinstance(v, (int, float)) else "N/A"


def _format_section(items: list[dict], emoji: str, title: str,
                    links: dict | None = None, extra: dict | None = None) -> str:
    """4열 평면 포맷 (종목 / 당일 / 연초대비 / EPS YoY). 종목명은 채널 게시물로 하이퍼링크."""
    links = links or {}
    extra = extra or {}
    head = f"━━━ {emoji} {title} ({len(items)}) ━━━"
    if not items:
        return head + "\n해당 없음\n"
    cap = _per_category_top()
    items_sorted = sorted(items, key=lambda it: -(it.get("chg_pct") or 0.0))
    items_capped = items_sorted[:cap]
    rest = len(items) - len(items_capped)
    lines = [head, "(종목 / 당일 / 연초대비 / EPS YoY)"]
    for it in items_capped:
        tkr = it.get("ticker", "")
        name = it.get("name") or tkr
        chg = _fmt_pct(it.get("chg_pct", 0.0))
        ex = extra.get(tkr, {})
        ytd = _fmt_pct_or_na(ex.get("ytd"))
        eps = _fmt_pct_or_na(ex.get("eps_yoy"))
        url = links.get(tkr)
        name_disp = f"[{name}]({url})" if url else name
        lines.append(f"{name_disp} / {chg} / {ytd} / {eps}")
    if rest > 0:
        lines.append(f"... 외 {rest}종목")
    return "\n".join(lines) + "\n"


def _sector_summary(items: list[dict], top_n: int = 6) -> str:
    """전체 신호 통합 섹터 집계."""
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


_KO_WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"]


def _fmt_kst_header(dt: datetime) -> str:
    """미미 스타일 헤더 날짜: '2026.05.08.(금) 16:00 KST'."""
    return dt.strftime("%Y.%m.%d.") + f"({_KO_WEEKDAY[dt.weekday()]}) " + dt.strftime("%H:%M KST")


def format_results(
    results: dict[str, list[dict]],
    as_of: datetime,
    base_date: str | None = None,
    stats: dict | None = None,
    links: dict | None = None,
    extra: dict | None = None,
) -> str:
    """미미 스타일 메시지 포맷.

    base_date(YYYY-MM-DD): 신호 계산에 쓰인 OHLCV 종가 날짜.
    stats: signals.compute_all이 반환한 dict — 검증된 종목 수 / 누락 종목 수 표시.
    """
    parts: list[str] = []
    parts.append(f"📈 한국 주식 기술적 신호 — {_fmt_kst_header(as_of)}")
    if base_date:
        as_of_iso = as_of.strftime("%Y-%m-%d")
        if base_date != as_of_iso:
            parts.append(f"📊 기준일: {base_date} (장마감 종가)")
        else:
            parts.append(f"📊 기준일: {base_date} 당일 종가")
    if stats:
        # 데이터 정확성 명시 — 검증된 종목 수 / 누락 종목 수 + 이중확인 결과
        proc = stats.get("processed", 0)
        skipped_no_base = stats.get("skipped_no_base", 0)
        validated = stats.get("validated", -1)
        rejected = stats.get("rejected", -1)
        verify_lines = [f"✓ {proc}종목 신호 계산 (시총 3000억+)"]
        if skipped_no_base > 0:
            verify_lines[-1] += f" · {skipped_no_base}종목 base_date 데이터 누락"
        if validated >= 0:
            v_line = f"✓ 이중확인: Naver 재 fetch로 {validated}종목 정합성 통과"
            if rejected > 0:
                v_line += f" · {rejected}종목 불일치/누락 제외"
            verify_lines.append(v_line)
        parts.append("\n".join(verify_lines))
    parts.append("(종목 클릭 → 채널의 1년 일봉 차트·상세) · 당일 등락 내림차순\n")

    # 전체 신호 통합 섹터 요약
    all_signals: list[dict] = []
    for v in results.values():
        all_signals.extend(v)
    sec_summary = _sector_summary(all_signals)
    if sec_summary:
        parts.append(f"🏷️ 주요 섹터: {sec_summary}\n")

    parts.append(_format_section(results.get("high_all", []), "🚀", "역사적 신고가", links, extra))
    parts.append(_format_section(results.get("high_52w", []), "📈", "52주 신고가", links, extra))
    parts.append(_format_section(results.get("vcp_breakout", []), "💎", "VCP 돌파 (최근 2주 이내)", links, extra))
    parts.append(_format_section(results.get("volume_breakout", []), "🔥", "거래량 돌파 ≥2배", links, extra))
    parts.append(_format_section(results.get("near_breakout_52w", []), "🎯", "52주 돌파 직전 95-99%", links, extra))

    total = sum(len(v) for v in results.values())
    if total == 0:
        parts.append("\n오늘은 신호 발생 종목이 없습니다.")

    return "\n".join(parts)
