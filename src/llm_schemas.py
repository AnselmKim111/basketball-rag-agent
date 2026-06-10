"""Layer B — LLM 응답 검증 Pydantic schema 모음.

각 fragile 호출처의 **실제 프롬프트 출력 형식**과 1:1 정합 (추정 금지 — 프롬프트 파일 대조 필수).
summarizer.chat_with_retry(validate_schema=...)에 전달하면 JSON parse + 필드 검증 후
실패 시 다음 retry에 schema 재주입.

| Schema | 호출처 | 프롬프트 근거 |
|---|---|---|
| IntentSchema | src/intent_router.py | ROUTER_PROMPT (intent 5종) |
| IdeaParseSchema | src/idea_bot.py::_parse_idea | prompts/idea_parse.txt |
| PredictionsExtractSchema | src/earnings_bot.py::_step_extract_predictions | prompts/earnings_predictions_extract.txt |
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IntentSchema(BaseModel):
    """src/intent_router.py ROUTER_PROMPT 출력.

    실제 intent 5종 (research/compare/theme/question/unknown) — Intent dataclass와 동일.
    """
    intent: Literal["research", "compare", "theme", "question", "unknown"]
    ticker_names: list[str] = Field(default_factory=list)
    aspect: str | None = None
    theme: str | None = None
    raw_question: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    clarification: str | None = None


class _IdeaConstraints(BaseModel):
    """idea_parse constraints 하위 객체 — 모든 필드 optional (제약 없으면 null/빈 배열)."""
    market_cap_max_krw: float | None = None
    market_cap_min_krw: float | None = None
    industry_filter: list[str] = Field(default_factory=list)
    exchange: str | None = None
    exclude_keywords: list[str] = Field(default_factory=list)
    other_notes: list[str] = Field(default_factory=list)


class IdeaParseSchema(BaseModel):
    """src/idea_bot.py::_parse_idea — prompts/idea_parse.txt 출력."""
    thesis: str = Field(min_length=5)
    constraints: _IdeaConstraints = Field(default_factory=_IdeaConstraints)
    constraints_summary: str | None = None


class _Prediction(BaseModel):
    """earnings predictions 항목 — prompts/earnings_predictions_extract.txt."""
    type: Literal["trigger", "counter", "action", "guidance"]
    statement_text: str = Field(min_length=10)
    conviction: int | None = Field(ge=1, le=5, default=None)
    metric_threshold: dict | None = None
    payload_excerpt: str | None = None


class PredictionsExtractSchema(BaseModel):
    """src/earnings_bot.py::_step_extract_predictions 출력. 빈 배열 OK."""
    predictions: list[_Prediction] = Field(default_factory=list)


__all__ = [
    "IntentSchema",
    "IdeaParseSchema",
    "PredictionsExtractSchema",
]
