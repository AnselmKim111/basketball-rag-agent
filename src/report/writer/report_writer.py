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
    }
    user_msg = (
        "다음은 오늘 시장 데이터·전일 대비 변화(deltas_vs_yesterday)·생성된 차트 목록이다. "
        "시스템 규칙(8섹션 + 전일 대비 팔로업)에 따라 한국어 Markdown 시황 리포트를 작성하라. "
        "맨 앞에 '📍 어제 대비 변화' 블록과 Executive Summary(3~4줄)를 두고, 각 차트는 반드시 "
        "![](images/파일명)으로 본문에 삽입하며, 각 섹션 끝에 '한 줄 takeaway'를 붙여라. "
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
            return md.strip()
        log.warning("[report.writer] LLM 짧은 응답 → fallback")
    except Exception:
        log.exception("[report.writer] LLM 실패 → fallback")
    return _fallback_markdown(date_iso, market_color, chart_list, signals, deltas)


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
