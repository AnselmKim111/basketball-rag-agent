"""미미 스타일 포맷 헬퍼 (시장 비의존).

KR/US 공통: 섹터 그룹핑, % 포맷, 섹션 박스, 헤더 날짜.

시장별 차이는 `FormatConfig`로 주입:
  - `fallback_sector`: 빈 섹터 라벨 ("기타" / "Other")
  - `priority_market`: 우선 정렬할 시장 ("KOSPI" / "S&P500")
  - `per_category_env`: 카테고리당 표시 한도 env 키
  - `label_fn`: 종목 dict → 표시 문자열 (KR=name, US=ticker)

시장별 `format_results`는 헤더·시장 라벨·통화 표기와 함께 이 헬퍼들을 호출.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass(frozen=True)
class FormatConfig:
    fallback_sector: str
    priority_market: str
    per_category_env: str
    label_fn: Callable[[dict], str]


def fmt_pct(v: float, signed: bool = True) -> str:
    sign = "+" if v > 0 and signed else ""
    return f"{sign}{v:.1f}%"


def per_category_top(env_key: str, default: int = 80) -> int:
    """카테고리당 표시 한도 (env 오버라이드 가능)."""
    try:
        return max(1, int(os.getenv(env_key, str(default))))
    except ValueError:
        return default


def _market_rank(item: dict, priority_market: str) -> int:
    """priority_market 일치 0, 그 외 1 — 정렬용."""
    m = (item.get("market") or "").upper()
    return 0 if m == priority_market.upper() else 1


def group_by_sector(items: list[dict], cfg: FormatConfig) -> list[tuple[str, list[dict]]]:
    """섹터별 그룹화 + 섹터 내 priority_market 우선 정렬.

    섹터 순서: 종목 수 desc + 첫 등장 순. 상위 정렬은 호출자가 이미 한 상태 가정.
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for it in items:
        sec = (it.get("sector") or "").strip() or cfg.fallback_sector
        if sec not in groups:
            groups[sec] = []
            order.append(sec)
        groups[sec].append(it)
    for sec in groups:
        groups[sec].sort(key=lambda it: (_market_rank(it, cfg.priority_market),))
    sorted_secs = sorted(order, key=lambda s: (-len(groups[s]), order.index(s)))
    return [(s, groups[s]) for s in sorted_secs]


def format_section(
    items: list[dict],
    emoji: str,
    title: str,
    cfg: FormatConfig,
) -> str:
    """미미 스타일 섹션: 섹터 그룹 + 종목 라벨 콤마 나열."""
    head = f"━━━ {emoji} {title} ({len(items)}) ━━━"
    if not items:
        return head + "\n해당 없음\n"
    cap = per_category_top(cfg.per_category_env)
    items_capped = items[:cap]
    rest = len(items) - cap
    sector_groups = group_by_sector(items_capped, cfg)
    lines = [head]
    for sec, sec_items in sector_groups:
        names = ", ".join(cfg.label_fn(it) for it in sec_items)
        lines.append(f"({sec}) {names}")
    if rest > 0:
        lines.append(f"... 외 {rest}종목")
    return "\n".join(lines) + "\n"


def sector_summary(items: list[dict], cfg: FormatConfig, top_n: int = 6) -> str:
    """전체 신호 통합 섹터 집계 — 'IT(12), 금융(5), ...' 형식."""
    if not items:
        return ""
    counts: dict[str, int] = {}
    for it in items:
        s = (it.get("sector") or "").strip() or cfg.fallback_sector
        counts[s] = counts.get(s, 0) + 1
    if not counts:
        return ""
    sorted_items = sorted(counts.items(), key=lambda x: -x[1])[:top_n]
    return ", ".join(f"{name}({n})" for name, n in sorted_items)


_KO_WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"]


def fmt_kst_header(dt: datetime) -> str:
    """미미 스타일 헤더 날짜: '2026.05.08.(금) 16:00 KST'."""
    return dt.strftime("%Y.%m.%d.") + f"({_KO_WEEKDAY[dt.weekday()]}) " + dt.strftime("%H:%M KST")
