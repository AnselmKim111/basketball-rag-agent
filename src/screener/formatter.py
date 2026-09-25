"""신호 결과 → 텔레그램 메시지.

미미 스타일 포맷 (https://t.me/mimi_ATH, 2026-09-23 사용자 요청 "이정도 수준으로 깔끔하게"):
  📈 신고가 — 날짜
  (반도체) 아스플로★, 엑시콘, 메가터치
  (소프트웨어) 안랩
  (소형주/1,000억↓) ...
  ★ 역사적 신고가

  🎯 돌파 직전 (52주 고점 근접)
  (반도체) ...
숫자 열·통계 헤더 없음. 종목명은 차트 채널 게시물로 하이퍼링크. 운영노트는 맨 끝 한 줄.
"""
from __future__ import annotations

import os
from datetime import datetime


def _fmt_pct(v: float, signed: bool = True) -> str:
    sign = "+" if v > 0 and signed else ""
    return f"{sign}{v:.1f}%"


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


def _fmt_cap_short(cap) -> str:
    """시총 짧은 표기: 2,150억 / 1.5조. fmt_won에서 '원'만 제거."""
    from src.screener_common import fmt_won
    s = fmt_won(cap)
    return s[:-1] if s.endswith("원") else s


# ------------------------------------------------------------------
# 표시 정책 (2026-09-23, 미미 스타일 — https://t.me/mimi_ATH)
#   📈 신고가: 52주 종가 신고가 ∪ 장중고가 신고가 ∪ 역사적 신고가(★) — 업종별 한 줄
#   (소형주) 시총 필터(1000억) 미만 신고가 — 미미 '동전주' 버킷처럼 한 줄
#   🎯 돌파 직전: 52주 고점 근접 (신고가에 이미 나온 종목 제외)
# 나머지 신호(6개월·VCP·수급·RS·추세전환)는 계산·저장만 하고 메시지엔 안 보임.
# ------------------------------------------------------------------
NEW_HIGH_CATS = ("high_all", "high_52w", "high_52w_hi")
DISPLAY_NEW_HIGH = "new_high"
DISPLAY_NEAR = "near_breakout"
DISPLAY_SMALL = "new_high_small"


def smallcap_max() -> float:
    """소형주 버킷 경계 — signals의 시총 필터(기본 1000억)와 동일."""
    try:
        return float(os.getenv("SCREENER_MIN_MARKET_CAP", "") or 100_000_000_000)
    except ValueError:
        return 100_000_000_000


def display_items(results: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """메시지에 실제 표시되는 그룹별 종목 (차트 게시 대상과 1:1 계약).

    반환 키: new_high(★=high_all 포함) / near_breakout / new_high_small.
    앞 그룹이 종목을 선점(dedup). 각 item에는 원신호 카테고리 목록 "cats"가 붙는다.
    """
    cap_max = smallcap_max()

    def _is_small(it: dict) -> bool:
        cap = it.get("market_cap")
        return bool(cap) and cap < cap_max

    seen: set = set()
    merged: dict[str, dict] = {}
    for cat in NEW_HIGH_CATS:
        for it in results.get(cat, []) or []:
            t = it.get("ticker")
            if not t or _is_small(it):
                continue
            entry = merged.setdefault(t, dict(it, cats=[]))
            entry["cats"].append(cat)
    new_high = list(merged.values())
    seen.update(merged.keys())

    near = []
    for it in results.get("near_breakout_52w", []) or []:
        t = it.get("ticker")
        if not t or t in seen or _is_small(it):
            continue
        near.append(dict(it, cats=["near_breakout_52w"]))
        seen.add(t)

    small: dict[str, dict] = {}
    for cat in ("high_52w_small",) + NEW_HIGH_CATS:
        for it in results.get(cat, []) or []:
            t = it.get("ticker")
            if not t or t in seen or t in small:
                continue
            if cat == "high_52w_small" or _is_small(it):
                small[t] = dict(it, cats=[cat])
    return {DISPLAY_NEW_HIGH: new_high, DISPLAY_NEAR: near, DISPLAY_SMALL: list(small.values())}


def _name_disp(it: dict, links: dict) -> str:
    from src.bot_helpers import html_escape
    name = html_escape(it.get("name") or it.get("ticker", ""))
    if "high_all" in (it.get("cats") or []):
        name += "★"
    url = links.get(it.get("ticker"))
    return f'<a href="{url}">{name}</a>' if url else name


SECTOR_LINE_MAX = 12   # 업종 한 줄 최대 종목 수
SMALL_SHOW_MAX = 15    # 소형주 버킷 최대 종목 수


def _sector_groups(items: list[dict]) -> list[tuple[str, list[dict], int]]:
    """[(업종, 표시 종목, 생략 수)] — 종목 수 많은 업종 먼저, 기타는 맨 뒤."""
    ordered = sorted(items, key=lambda it: -(it.get("chg_pct") or 0.0))
    groups = _group_by_sector(ordered)
    groups.sort(key=lambda g: (g[0] == "기타", -len(g[1])))
    return [(sec, its[:SECTOR_LINE_MAX], max(0, len(its) - SECTOR_LINE_MAX)) for sec, its in groups]


def _small_shown(small: list[dict]) -> list[dict]:
    return sorted(small, key=lambda it: -(it.get("chg_pct") or 0.0))[:SMALL_SHOW_MAX]


def shown_items(results: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """display_items 중 메시지에 실제 이름이 찍히는 종목만 ('외 N' 생략분 제외).

    차트 게시 대상 = 이것과 1:1 — 메시지의 모든 이름이 차트 링크를 갖는다.
    """
    disp = display_items(results)
    out = {}
    for key in (DISPLAY_NEW_HIGH, DISPLAY_NEAR):
        out[key] = [it for _, its, _ in _sector_groups(disp[key]) for it in its]
    out[DISPLAY_SMALL] = _small_shown(disp[DISPLAY_SMALL])
    return out


def _sector_lines(items: list[dict], links: dict) -> list[str]:
    """업종별 한 줄: '(반도체) 아스플로★, 엑시콘, ...'. 종목 수 많은 업종 먼저, 기타는 맨 뒤."""
    if not items:
        return ["해당 없음"]
    lines = []
    for sec, its, rest in _sector_groups(items):
        names = [_name_disp(it, links) for it in its]
        tail = f" 외 {rest}" if rest > 0 else ""
        lines.append(f"({sec}) {', '.join(names)}{tail}")
    return lines


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
    """미미 스타일 메시지 (2026-09-23 사용자 요청 — "이정도 수준으로, 깔끔하게").

    base_date(YYYY-MM-DD): 신호 계산에 쓰인 OHLCV 종가 날짜 — 당일이 아닐 때만 표기.
    stats/extra/retro는 호환용으로 받되 본문엔 쓰지 않음 (검증제외는 ops_notes로만).
    """
    links = links or {}
    disp = display_items(results)
    parts: list[str] = []
    head = f"📈 신고가 — {_fmt_kst_header(as_of)}"
    if base_date and base_date != as_of.strftime("%Y-%m-%d"):
        head += f"  (기준 {base_date} 종가)"
    parts.append(head)
    parts.extend(_sector_lines(disp[DISPLAY_NEW_HIGH], links))
    small = disp[DISPLAY_SMALL]
    if small:
        cap_label = _fmt_cap_short(smallcap_max())
        names = [_name_disp(it, links) for it in _small_shown(small)]
        rest = len(small) - len(names)
        parts.append(f"(소형주/{cap_label}↓) {', '.join(names)}{f' 외 {rest}' if rest > 0 else ''}")
    if any("high_all" in (it.get("cats") or []) for it in disp[DISPLAY_NEW_HIGH] + small):
        parts.append("★ 역사적 신고가")
    parts.append("")
    parts.append("🎯 돌파 직전 (52주 고점 근접)")
    parts.extend(_sector_lines(disp[DISPLAY_NEAR], links))

    # 운영노트 — 실행 중 특이사항(백필·검증제외 등)을 티 안 나게 끝에 한 줄
    if ops_notes:
        from src.bot_helpers import html_escape
        parts.append("")
        parts.append(f"<i>〔운영: {html_escape(' · '.join(ops_notes))}〕</i>")

    return "\n".join(parts)
