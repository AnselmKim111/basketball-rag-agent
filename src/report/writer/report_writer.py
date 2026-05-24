"""LLM 리포트 작성 — 데이터/신호/차트 메타 → 한국어 Markdown 내러티브.

summarizer.chat_with_retry 재사용 (synthesis tier). 차트는 코드가 생성하고,
여기선 차트 목록(파일명+설명)을 LLM에 넘겨 본문에 ![](images/..) 삽입하게 한다.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_SYSTEM_PATH = Path(__file__).resolve().parents[3] / "prompts" / "report_system.txt"


def _system_prompt() -> str:
    try:
        return _SYSTEM_PATH.read_text(encoding="utf-8")
    except Exception:
        return "너는 한국어 시황 전략가다. 차트와 데이터로 시장 색깔 변화를 진단하라."


def write_report(
    date_iso: str,
    market_color: dict,
    chart_list: list[dict],
    signals: list[dict],
    macro_summary: dict,
    korea_summary: dict,
    news: list[dict] | None = None,
    theme_momentum: dict | None = None,
    deltas: dict | None = None,
    breadth: dict | None = None,
    highlights: list[dict] | None = None,
    earnings: dict | None = None,
    stale: list[dict] | None = None,
) -> str:
    """LLM으로 전체 Markdown 리포트 생성 (8섹션 + 전일 대비 팔로업).

    chart_list: [{"filename","title","caption_hint","section"}], signals: MarketSignal dicts,
    macro_summary/korea_summary: 요약 dict, theme_momentum: {buckets,hot,cold},
    deltas: 전일 대비 변화, breadth: 시장 폭, highlights: 개별주 [{label,chg,note}].
    실패 시 규칙 기반 fallback markdown 반환.
    """
    payload = {
        "date": date_iso,
        "market_color": market_color,
        "deltas_vs_yesterday": deltas or {},
        "breadth": breadth or {},
        "theme_momentum": theme_momentum or {},
        "charts": chart_list,
        "signals": signals[:60],
        "macro": macro_summary,
        "korea": korea_summary,
        "highlights": highlights or [],
        "news": (news or [])[:15],
        "earnings": earnings or {},
        "stale_data": stale or [],
    }
    user_msg = (
        "다음은 오늘 시장 데이터·전일 대비 변화(deltas_vs_yesterday)·생성된 차트 목록이다. "
        "시스템 규칙(버터대디 캐주얼 1인칭 문체 + 오늘의 핵심 가설 관통 + 8섹션 + 전일 대비 팔로업)에 따라 "
        "한국어 Markdown 시황 리포트를 작성하라. "
        "맨 앞에 '📍 어제 대비 변화' 블록과 Executive Summary(3~4줄)를 두고, 각 차트는 반드시 "
        "![](images/파일명)으로 본문에 삽입하며, 각 섹션 끝에 '한 줄 takeaway(•)'를 붙여라. "
        "§5 어닝 모멘텀은 earnings(최근 beat율·평균 서프라이즈·top beats/misses·다가올 일정)로 작성하라. "
        "§6 개별 종목은 highlights와 news를 엮어 종목별 미니 스토리로(단순 등락 나열 금지). "
        "news는 내러티브에 자연스럽게 녹이되 **출처 URL은 절대 쓰지 말고**(지어내기 금지), 매체/주체 이름만 "
        "필요시 언급하라. 입력 news에 있는 사실만 쓰고 없는 수치·사실은 만들지 마라. "
        "stale_data에 있는 카테고리는 '⚠ 전일자 데이터(asof)'로 명시하라. "
        "데이터 미확보는 '데이터 미수집'으로, 가설은 '가설'로 표기하라.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    try:
        from openai import OpenAI
        from src import summarizer
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
        )
        model = os.getenv("REPORT_SYNTHESIS_MODEL") or os.getenv("IDEA_SYNTHESIS_MODEL") or "anthropic/claude-sonnet-4.5"
        md = summarizer.chat_with_retry(
            client,
            messages=[{"role": "system", "content": _system_prompt()},
                      {"role": "user", "content": user_msg}],
            max_tokens=8000,
            model=model,
            temperature=0.4,
            context="report_writer",
        )
        if md and len(md.strip()) > 200:
            return _ensure_all_charts(md.strip(), chart_list)
        log.warning("[report.writer] LLM 짧은 응답 → fallback")
    except Exception:
        log.exception("[report.writer] LLM 실패 → fallback")
    return _ensure_all_charts(_fallback_markdown(date_iso, market_color, chart_list, signals, deltas),
                              chart_list)


def _ensure_all_charts(md: str, chart_list: list[dict]) -> str:
    """LLM이 일부 차트를 빠뜨리거나 없는 파일명을 적어도, 모든 차트가 제 섹션 맥락에
    정확히 1회씩 들어가도록 보정. PDF 단일 파일에 전 차트가 빠짐없이 박히게 하는 핵심.

    1) 전체를 감싼 코드펜스 제거
    2) chart_list에 없는(환각) 이미지 참조 제거
    3) 본문에 안 들어간 차트를 그 section 헤딩 아래(없으면 말미 부록)에 삽입
    """
    import re
    if not chart_list:
        return md
    s = md.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s).strip()
    md = s

    valid = {c["filename"] for c in chart_list}

    # 2) 환각 이미지 참조 제거 (filename이 chart_list에 없으면 삭제)
    def _drop(m):
        return m.group(0) if m.group(1) in valid else ""
    md = re.sub(r"!\[[^\]]*\]\(images/([^)\s]+)\)", _drop, md)

    referenced = {f for f in re.findall(r"images/([^)\s]+)", md) if f in valid}
    missing = [c for c in chart_list if c["filename"] not in referenced]
    if not missing:
        return md

    lines = md.split("\n")

    def _section_key(sec: str) -> str:
        return re.sub(r"^\s*\d+\.?\s*", "", sec or "").strip()

    from collections import OrderedDict
    by_sec: "OrderedDict[str, list]" = OrderedDict()
    for c in missing:
        by_sec.setdefault(c.get("section", ""), []).append(c)

    appendix: list[dict] = []
    for sec, charts in by_sec.items():
        key = _section_key(sec)
        tokens = [t for t in key.split() if len(t) >= 2]
        target = None
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("#") and ((key and key in ln) or (tokens and tokens[0] in ln)):
                target = i
                break
        if target is None:
            appendix.extend(charts)
            continue
        block: list[str] = []
        for c in charts:
            block.append(f"\n![](images/{c['filename']})")
            if c.get("caption_hint"):
                block.append(f"*{c['caption_hint']}*")
        lines[target + 1:target + 1] = block
    md = "\n".join(lines)

    if appendix:
        md += "\n\n## 📎 추가 차트\n"
        for c in appendix:
            md += f"\n![](images/{c['filename']})\n"
            if c.get("caption_hint"):
                md += f"*{c['caption_hint']}*\n"
    return md


def _fallback_markdown(date_iso, market_color, chart_list, signals, deltas=None) -> str:
    """LLM 실패 시 차트+신호만으로 최소 리포트."""
    lines = [
        f"# [Daily Macro] {market_color.get('market_color','Mixed')} ({date_iso})",
        f"\n**오늘의 시장 색깔: {market_color.get('market_color','Mixed')}** "
        f"({', '.join(market_color.get('evidence', []))})\n",
    ]
    if deltas and deltas.get("notes"):
        lines.append("\n## 📍 어제 대비 변화")
        for note in deltas["notes"]:
            lines.append(f"- {note}")
    for ch in chart_list:
        lines.append(f"\n## {ch.get('title','')}")
        lines.append(f"![]({'images/' + ch['filename']})")
        if ch.get("caption_hint"):
            lines.append(f"\n{ch['caption_hint']}")
    if signals:
        lines.append("\n## 탐지된 신호")
        for s in signals[:30]:
            lines.append(f"- {s['asset']}: {s['signal_type']} — {s.get('comment','')}")
    lines.append("\n_지수는 움직였고, 이제 돈은 어디로 가고 있는가?_")
    return "\n".join(lines)
