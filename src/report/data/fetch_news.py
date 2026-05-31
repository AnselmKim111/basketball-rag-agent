"""뉴스 그라운딩 — perplexity/sonar(웹검색)로 오늘의 시장 견인 뉴스 확보.

기존 OPENROUTER_API_KEY 재사용(새 키 불필요). 결과는 write_report(news=...)로 주입되어
LLM이 각 섹션 내러티브에 인용(출처 url)하도록 한다.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "report_news.txt"


def _prompt() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return ("오늘 미국 시장을 움직인 핵심 뉴스 8-15개를 "
                '[{"title","summary","tickers","theme","url"}] JSON 배열로만 출력.')


def _parse_json_array(text: str) -> list[dict]:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s).strip()
    # 첫 '[' ~ 마지막 ']' 추출
    i, j = s.find("["), s.rfind("]")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    try:
        data = json.loads(s)
        return data if isinstance(data, list) else []
    except Exception:
        log.info("[report.news] JSON 파싱 실패")
        return []


def fetch_market_news(max_items: int = 18, attempts: int = 3) -> list[dict]:
    """오늘의 시장 견인 뉴스 list[dict]. 실패 시 빈 리스트.

    각 항목: title·summary·tickers·theme·source. URL은 부정확하므로 수집/사용하지 않음
    (모델이 url을 넣어도 여기서 제거 — 가짜 인용 방지).
    perplexity가 간헐적으로 빈 배열을 반환하므로 최대 attempts회 재시도(0건이면 재시도).
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        log.info("[report.news] OPENROUTER_API_KEY 없음 — 스킵")
        return []
    model = (os.getenv("REPORT_RESEARCH_MODEL") or os.getenv("IDEA_RESEARCH_MODEL")
             or "perplexity/sonar-pro")
    try:
        from openai import OpenAI
        from src import summarizer
        client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    except Exception:
        log.exception("[report.news] 클라이언트 초기화 실패")
        return []

    for attempt in range(1, attempts + 1):
        try:
            text = summarizer.chat_with_retry(
                client,
                messages=[
                    {"role": "system", "content": "You are a precise financial research assistant. Output JSON only."},
                    {"role": "user", "content": _prompt()},
                ],
                max_tokens=4000,
                model=model,
                temperature=0.3,
                context="report_news",
            )
            items = _parse_json_array(text or "")
            clean = []
            for it in items:
                if not isinstance(it, dict) or not it.get("title"):
                    continue
                it.pop("url", None)  # 부정확한 URL 차단 (가짜 인용 방지)
                clean.append(it)
            if clean:
                log.info("[report.news] %d건 확보 (model=%s, attempt=%d)", len(clean), model, attempt)
                return clean[:max_items]
            log.warning("[report.news] 0건 (attempt=%d/%d) — 재시도", attempt, attempts)
        except Exception:
            log.exception("[report.news] fetch 예외 (attempt=%d)", attempt)
    log.warning("[report.news] 전 시도 0건 — 빈 리스트 반환(캐시 폴백 예정)")
    return []
