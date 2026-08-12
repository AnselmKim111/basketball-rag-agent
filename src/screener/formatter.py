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
                    links: dict | None = None, extra: dict | None = None,
                    seen: set | None = None) -> str:
    """4열 평면 포맷 (종목 / 당일 / 연초대비 / EPS YoY). 종목명은 채널 게시물로 하이퍼링크.

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
    lines = [head, "(종목 / 당일 / 연초대비 / EPS YoY)"]
    from src.bot_helpers import html_escape
    for it in items_capped:
        tkr = it.get("ticker", "")
        name = html_escape(it.get("name") or tkr)
        chg = _fmt_pct(it.get("chg_pct", 0.0))
        ex = extra.get(tkr, {})
        ytd = _fmt_pct_or_na(ex.get("ytd"))
        eps = _fmt_pct_or_na(ex.get("eps_yoy"))
        url = links.get(tkr)
        name_disp = f'<a href="{url}">{name}</a>' if url else name
        lines.append(f"{name_disp} / {chg} / {ytd} / {eps}")
    if rest > 0:
        lines.append(f"... 외 {rest}종목")
    return "\n".join(lines) + "\n"


def _fmt_cap_short(cap) -> str:
    """시총 짧은 표기: 2,150억 / 1.5조. fmt_won에서 '원'만 제거."""
    from src.screener_common import fmt_won
    s = fmt_won(cap)
    return s[:-1] if s.endswith("원") else s


def _format_smallcap_section(small_by_ticker: dict, links: dict | None,
                             extra: dict | None, seen: set,
                             cap_n: int = 12) -> str:
    """🧩 중소형(1000억~3000억) 신호 — 시총 표기 + 발화 신호 이모지.

    small_by_ticker: {ticker: {"item": dict, "emojis": [..]}} (복수 신호는 이모지 병기).
    라인: 이름(2,150억) / 당일 / 연초대비 / 🚀📈
    """
    links = links or {}
    extra = extra or {}
    items = [(t, v) for t, v in small_by_ticker.items() if t not in seen]
    head = f"━━━ 🧩 중소형 신호 (1000억~3000억) ({len(items)}) ━━━"
    if not items:
        return head + "\n해당 없음\n"
    items.sort(key=lambda kv: -(kv[1]["item"].get("chg_pct") or 0.0))
    for t, _ in items:
        seen.add(t)
    capped = items[:cap_n]
    rest = len(items) - len(capped)
    from src.bot_helpers import html_escape
    lines = [head, "(종목(시총) / 당일 / 연초대비 / 신호)"]
    for t, v in capped:
        it = v["item"]
        name = html_escape(it.get("name") or t)
        cap_s = _fmt_cap_short(it.get("market_cap"))
        chg = _fmt_pct(it.get("chg_pct", 0.0))
        ytd = _fmt_pct_or_na((extra.get(t) or {}).get("ytd"))
        url = links.get(t)
        name_disp = f'<a href="{url}">{name}</a>' if url else name
        lines.append(f"{name_disp}({cap_s}) / {chg} / {ytd} / {''.join(v['emojis'])}")
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
    retro: dict | None = None,
    ops_notes: list[str] | None = None,
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
        proc = stats.get("processed", 0)
        skipped_no_base = stats.get("skipped_no_base", 0)
        line = f"✓ {proc}종목 신호 계산 (시총 1000억+)"
        if skipped_no_base > 0:
            line += f" · {skipped_no_base}종목 base_date 데이터 누락"
        parts.append(line)
        # 시장 폭 — 신고가 부재가 시장 탓인지 판별
        breadth = stats.get("breadth")
        if breadth:
            from src.screener.breadth import format_breadth_line
            bl = format_breadth_line(breadth)
            if bl:
                parts.append(bl)

    # 회고 (있으면): 지난 신호 종목들의 N영업일 후 평균 수익률
    if retro:
        from src.screener.retrospective import format_retrospective_line
        retro_str = format_retrospective_line(retro)
        if retro_str:
            parts.append(retro_str)

    # 시총 3000억 경계로 대형/중소형 분리 — 기존 섹션은 3000억+(또는 시총 미상)만,
    # 중소형(1000억~3000억)은 맨 뒤 🧩 섹션에 시총 표기와 함께 모아서.
    import os as _os
    try:
        smallcap_max = float(_os.getenv("SCREENER_SMALLCAP_MAX", "") or 300_000_000_000)
    except ValueError:
        smallcap_max = 300_000_000_000
    _CAT_EMOJI = [
        ("near_breakout_52w", "🎯"), ("high_all", "🚀"), ("high_52w", "📈"),
        ("high_26w", "📊"), ("vcp_breakout", "💎"), ("volume_surge", "🔥"),
        ("rs_leaders", "💪"),
    ]
    big: dict[str, list[dict]] = {}
    small_by_ticker: dict[str, dict] = {}  # ticker → {"item", "emojis"}
    for cat, emoji in _CAT_EMOJI:
        big[cat] = []
        for it in results.get(cat, []) or []:
            cap = it.get("market_cap")
            if cap and cap < smallcap_max:
                agg = small_by_ticker.setdefault(it.get("ticker"), {"item": it, "emojis": []})
                if emoji not in agg["emojis"]:
                    agg["emojis"].append(emoji)
            else:
                big[cat].append(it)

    # 섹션 순서 = 백테스트 edge 순 (2026-06 검증: 돌파직전 +2.3%p > 52주 +2.0%p > ATH).
    # 앞 섹션이 종목을 먼저 claim (공용 seen dedup).
    seen: set = set()
    parts.append(_format_section(big["near_breakout_52w"], "🎯", "52주 돌파 직전 90-99%", links, extra, seen))
    parts.append(_format_section(big["high_all"], "🚀", "역사적 신고가", links, extra, seen))
    parts.append(_format_section(big["high_52w"], "📈", "52주 신고가", links, extra, seen))
    # 6개월 신고가 — 52주 계열에 이미 나온 종목은 dedup으로 빠져 "회복 국면"만 남음
    parts.append(_format_section(big["high_26w"], "📊", "6개월 신고가 (회복 국면)", links, extra, seen))
    parts.append(_format_section(big["vcp_breakout"], "💎", "VCP 돌파 (최근 1주 이내)", links, extra, seen))
    parts.append(_format_section(big["volume_surge"], "🔥", "수급 유입 (거래량 3배+ 급등)", links, extra, seen))
    parts.append(_format_section(big["rs_leaders"], "💪", "상대강도 리더 (시장 대비 상위 10%)", links, extra, seen))
    parts.append(_format_smallcap_section(small_by_ticker, links, extra, seen))

    total = sum(len(v) for v in big.values()) + len(small_by_ticker)
    if total == 0:
        parts.append("\n오늘은 신호 발생 종목이 없습니다.")

    # 운영노트 — 실행 중 특이사항(백필·검증제외 등)을 티 안 나게 끝에 한 줄
    if ops_notes:
        from src.bot_helpers import html_escape
        parts.append(f"<i>〔운영: {html_escape(' · '.join(ops_notes))}〕</i>")

    return "\n".join(parts)
