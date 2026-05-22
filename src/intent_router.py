"""자연어 → 인텐트 JSON 추출 + 라우팅.

종목봇 text_handler가 사용. 사용자가 자유 텍스트 ("전닉 capex 비교", "삼성전자 어때",
"AI 인프라 종목 추천") 입력 시 LLM(haiku)이 의도를 JSON으로 분류 → 적절한
핸들러로 위임.

5 인텐트:
  research  — 단일 종목 통합 분석 → deep_research.run()
  compare   — N종목 측면 비교 (capex 등) → comparator.run_compare()
  theme     — 테마 발굴 → IdeaBot 안내 (라우팅 불가, 봇 분리)
  question  — 일반 질문 → LLM 답변
  unknown   — 매칭 실패 → 사용자 명확화 요청
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from src.summarizer import OpenRouterCreditExhausted, chat_with_retry, get_client

log = logging.getLogger(__name__)

# 모델 — 빠른 추론 (haiku) + JSON 출력. idea_bot narrow tier 재사용.
ROUTER_MODEL_ENV = "INTENT_ROUTER_MODEL"
DEFAULT_ROUTER_MODEL = "anthropic/claude-haiku-4.5"


@dataclass
class Intent:
    intent: str  # "research" | "compare" | "theme" | "question" | "unknown"
    ticker_names: list[str] = field(default_factory=list)  # 사용자가 쓴 원본 이름·약어
    tickers: list[str] = field(default_factory=list)        # resolved 6자리 (선택)
    aspect: Optional[str] = None                            # compare 시 비교 측면 (capex 등)
    theme: Optional[str] = None
    raw_question: Optional[str] = None
    confidence: float = 0.0
    clarification: Optional[str] = None  # 사용자에게 물어볼 질문 (unknown 시)


ROUTER_PROMPT = """당신은 한국 주식 봇의 의도 분류기다. 사용자의 자유 텍스트
입력을 받아 어떤 동작을 원하는지 JSON으로 분류하라.

[사용자 입력]
"{text}"

[인텐트 5종]
1. research  — 단일 종목 통합 분석 (예: "삼성전자", "005930", "삼성전자 분석")
2. compare   — N(2-5)종목을 특정 측면으로 비교 (예: "삼성전자 SK하이닉스 capex 비교",
                "엔비디아 ams 영업이익 비교")
3. theme     — 테마·산업으로 종목 발굴 (예: "AI 인프라 종목 추천", "2차전지 좋은 거")
4. question  — 일반 질문 (예: "지금 시황 어때?", "PBR 설명해줘")
5. unknown   — 분류 불가 또는 종목 약어가 모호

[한국 주식 약어 처리]
- 정확한 종목명 (삼성전자, SK하이닉스 등) → ticker_names에 그대로
- 약어/별명 (전닉, 케이엠, 한진 등) → ticker_names에 원본 그대로 두기.
  봇이 별도 단계에서 resolve.
- 약어가 너무 모호하면 intent=unknown + clarification 필드에
  "전닉이 무슨 종목인지 정확히 알려주세요" 같은 메시지.

[출력 — 반드시 valid JSON 1개]
{{
  "intent": "research|compare|theme|question|unknown",
  "ticker_names": ["삼성전자", "SK하이닉스"],     // 원본 종목명·약어 (research 1개, compare 2-5개)
  "aspect": "capex|매출|영업이익|...",            // compare 시 비교 측면
  "theme": "AI 인프라",                              // theme 시
  "raw_question": "...",                            // question 시 원문 그대로
  "confidence": 0.0-1.0,                            // 분류 자신도
  "clarification": "..."                             // unknown 시 사용자에게 물어볼 질문
}}

[규칙]
- 종목 1개만 언급 + "분석/리서치/봐줘" 류 → research
- 종목 2개 이상 + 비교 키워드 ("비교", "차이", "vs", "어느게 더") → compare
- aspect 추출: "capex 비교"·"매출 비교"·"영업이익 비교" 같이 명시되면 그 단어, 없으면 "전반"
- 단순 종목명 1개만 ("삼성전자") → research (기본 동작)
- JSON 외 다른 텍스트 출력 금지
"""


def _extract_json(text: str) -> Optional[dict]:
    """LLM 응답에서 JSON 1개 추출. fence + bare 모두."""
    if not text:
        return None
    # ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # bare JSON
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            log.warning("intent_router JSON parse 실패: %s", text[first:last + 1][:200])
    return None


async def classify(text: str) -> Intent:
    """자연어 → Intent. 실패 시 intent='unknown'."""
    text = (text or "").strip()
    if not text:
        return Intent(intent="unknown", clarification="입력이 비어있습니다.")

    # 6자리 ticker 또는 정확한 4-6자 한글 종목명만이면 LLM 거치지 않고 빠른 path
    if text.isdigit() and len(text) == 6:
        return Intent(intent="research", ticker_names=[text], confidence=1.0)

    try:
        client = get_client()
    except Exception:
        log.exception("OpenRouter client 초기화 실패")
        return Intent(intent="unknown", clarification="LLM 라우터 초기화 실패 — 명시 명령(/research, /curate) 사용해 주세요.")

    model = os.getenv(ROUTER_MODEL_ENV) or DEFAULT_ROUTER_MODEL
    prompt = ROUTER_PROMPT.format(text=text)

    import asyncio
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(
            None,
            lambda: chat_with_retry(
                client,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                model=model,
                temperature=0.0,
                context=f"intent_router ({text[:30]})",
            ),
        )
    except OpenRouterCreditExhausted:
        return Intent(intent="unknown", clarification="OpenRouter 크레딧 부족 — 명시 명령 사용")
    except Exception:
        log.exception("intent_router LLM 호출 실패")
        return Intent(intent="unknown", clarification="라우터 LLM 호출 실패 — 명시 명령 사용")

    parsed = _extract_json(raw or "")
    if not parsed:
        log.warning("intent_router 파싱 실패: %s", (raw or "")[:200])
        # 폴백: 단순 종목명 1개로 추정해 research 시도
        return Intent(intent="research", ticker_names=[text], confidence=0.3)

    intent = parsed.get("intent", "unknown")
    if intent not in ("research", "compare", "theme", "question", "unknown"):
        intent = "unknown"

    return Intent(
        intent=intent,
        ticker_names=parsed.get("ticker_names") or [],
        tickers=parsed.get("tickers") or [],
        aspect=parsed.get("aspect"),
        theme=parsed.get("theme"),
        raw_question=parsed.get("raw_question"),
        confidence=float(parsed.get("confidence") or 0.0),
        clarification=parsed.get("clarification"),
    )


# ------------------------------------------------------------------
# ticker resolve — 종목명·약어 → 6자리 ticker
# ------------------------------------------------------------------
async def resolve_tickers(ticker_names: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(name, ticker) 쌍 리스트 + 실패한 name 리스트 반환.

    1차: 6자리 숫자면 그대로
    2차: dart_client.lookup_ticker_by_name (정확 매칭)
    3차: 실패 시 unresolved에 담아 호출자가 사용자에게 명확화 요청
    """
    import asyncio
    try:
        from src.deepdive import dart_client
    except Exception:
        log.exception("dart_client import 실패 — resolve 불가")
        return [], list(ticker_names)

    loop = asyncio.get_running_loop()
    resolved: list[tuple[str, str]] = []
    unresolved: list[str] = []

    for name in ticker_names:
        name = (name or "").strip()
        if not name:
            continue
        if name.isdigit() and len(name) == 6:
            # ticker만 있으면 corp 이름 lookup
            try:
                pair = await loop.run_in_executor(None, dart_client.get_corp_code, name)
                resolved.append((pair[1] if pair else name, name))
            except Exception:
                log.exception("get_corp_code 실패 %s", name)
                resolved.append((name, name))
            continue
        try:
            ticker = await loop.run_in_executor(None, dart_client.lookup_ticker_by_name, name)
        except Exception:
            log.exception("lookup_ticker_by_name 실패 %s", name)
            ticker = None
        if ticker:
            resolved.append((name, ticker))
        else:
            unresolved.append(name)

    return resolved, unresolved
