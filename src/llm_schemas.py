"""Layer B — LLM 응답 검증 Pydantic schema 모음.

각 fragile 호출처에 1:1 매핑. summarizer.chat_with_retry(validate_schema=...)에 전달.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IntentSchema(BaseModel):
    """src/intent_router.py — 자유텍스트→intent 분류."""
    intent: Literal["report", "deepdive", "research", "compare", "industry",
                    "market", "global", "idea", "screener", "earnings",
                    "disclosure", "recap", "small_talk", "help"]
    confidence: float = Field(ge=0.0, le=1.0)
    target: str | None = None  # 종목명/티커 (있을 때)
    reason: str | None = None


class ThesisV0Schema(BaseModel):
    """src/idea_bot.py:1379 — 0.5단계 thesis + constraints."""
    thesis: str = Field(min_length=5)
    constraints: list[str] = Field(default_factory=list)
    timeframe: str | None = None


class NarrowScoreSchema(BaseModel):
    """src/idea_bot.py narrow loop — 4축 점수화."""
    tech_score: int = Field(ge=0, le=100)
    demand_score: int = Field(ge=0, le=100)
    valuation_score: int = Field(ge=0, le=100)
    momentum_score: int = Field(ge=0, le=100)
    reason: str | None = None


class EarningsPredictionSchema(BaseModel):
    """src/earnings_bot.py predictions — 어닝 결과 → 다음 분기 예측."""
    epsilon: float | None = None  # EPS surprise 예상
    direction: Literal["up", "down", "sideways"] | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    key_drivers: list[str] = Field(default_factory=list)


# 호출처에서 사용하는 alias
__all__ = [
    "IntentSchema",
    "ThesisV0Schema",
    "NarrowScoreSchema",
    "EarningsPredictionSchema",
]
