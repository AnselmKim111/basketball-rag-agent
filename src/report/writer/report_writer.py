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
) -> str:
    """LLM으로 전체 Markdown 리포트 생성.

    chart_list: [{"filename","title","caption_hint"}], signals: MarketSignal dicts,
    macro_summary/korea_summary: 요약 dict, news: [{title,source,url,published_at}].
    실패 시 규칙 기반 fallback markdown 반환.
    """
    payload = {
        "date": date_iso,
        "market_color": market_color,
        "charts": chart_list,
        "signals": signals[:60],
        "macro": macro_summary,
        "korea": korea_summary,
        "news": (news or [])[:15],
    }
    user_msg = (
        "다음은 오늘 시장 데이터와 생성된 차트 목록이다. 이를 바탕으로 시스템 규칙에 따라 "
        "한국어 Markdown 시황 리포트를 작성하라. 차트는 반드시 ![](images/파일명)으로 본문에 "
        "삽입하고 각 차트에 관찰/해석/체크 3줄을 붙여라.\n\n"
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
    return _fallback_markdown(date_iso, market_color, chart_list, signals)


def _fallback_markdown(date_iso, market_color, chart_list, signals) -> str:
    """LLM 실패 시 차트+신호만으로 최소 리포트."""
    lines = [
        f"# {date_iso} 시장 색깔 리포트 (자동 생성 — LLM 미작동 fallback)",
        f"\n**오늘의 시장 색깔: {market_color.get('market_color','Mixed')}** "
        f"({', '.join(market_color.get('evidence', []))})\n",
    ]
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
