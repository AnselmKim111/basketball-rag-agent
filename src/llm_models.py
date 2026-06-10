"""LLM 모델 티어 선택 헬퍼 — 봇 공유.

CLAUDE.md §2 모델 티어:
  - **Summary** (`OPENROUTER_MODEL`, 갓성비 — kimi 등): PDF 요약·DART·forward·
    deepdive·idea_parse 등 단순 추출/요약.
  - **Research** (`IDEA_RESEARCH_MODEL`, perplexity/sonar-pro): 1단계 웹검색.
  - **Narrow** (`IDEA_NARROW_MODEL`, claude-haiku-4.5): 30→10 큰 출력 + parse 폴백.
  - **Synthesis** (`IDEA_SYNTHESIS_MODEL`, claude-sonnet-4.5): 1.5 importance +
    5 synthesis (진짜 지능 필요).

각 봇이 자기 env 이름을 인자로 명시하면 같은 함수에서 분기 처리. 새 봇 추가 시
일관성 자동 유지.
"""

from __future__ import annotations

import os
from typing import Iterable


# 기본값 — 각 티어의 안전 fallback
DEFAULT_SUMMARY = "moonshotai/kimi-k2.6"
DEFAULT_NARROW = "anthropic/claude-haiku-4.5"
DEFAULT_SYNTHESIS = "anthropic/claude-sonnet-4.5"
DEFAULT_RESEARCH = "perplexity/sonar-pro"


def summary_model() -> str:
    """Summary tier — kimi 등 갓성비. OPENROUTER_MODEL env가 1순위.

    PDF 요약·DART 추출·idea parse·deepdive 등 단순 작업용.
    """
    return os.getenv("OPENROUTER_MODEL") or DEFAULT_SUMMARY


def narrow_model(env: str = "IDEA_NARROW_MODEL") -> str:
    """Narrow tier — haiku 등 중간. 큰 출력 + 비용 절감 필요한 단계.

    env override → OPENROUTER_MODEL → haiku 기본.
    """
    return os.getenv(env) or os.getenv("OPENROUTER_MODEL") or DEFAULT_NARROW


def synthesis_model(env: str = "IDEA_SYNTHESIS_MODEL") -> str:
    """Synthesis tier — sonnet/opus 등 진짜 지능 필요.

    env override → OPENROUTER_MODEL → sonnet 기본.
    """
    return os.getenv(env) or os.getenv("OPENROUTER_MODEL") or DEFAULT_SYNTHESIS


def research_model(env: str = "IDEA_RESEARCH_MODEL") -> str:
    """Research tier — perplexity/sonar-pro 등 웹검색 가능 모델."""
    return os.getenv(env) or DEFAULT_RESEARCH


# ------------------------------------------------------------------
# Fallback chain — env에 단일 모델만 있어도 안정 default가 뒤에 자동 보강.
# 사용자가 모델 변경 후 그 모델이 빈 응답을 반환해도 chain이 다음으로 fallback.
# ------------------------------------------------------------------
def model_chain_from_env(env: str, defaults: list[str]) -> list[str]:
    """env value → 모델 chain.

    - env 미설정/빈 값 → defaults 그대로
    - 콤마 구분 ("a,b,c") → 각 모델로 split (정확히 사용자 의도)
    - 단일 모델 ("primary") → [primary] + defaults 중 중복 제거한 fallback

    예:
      env=None, defaults=["haiku-4.5", "kimi-k2.6"]   → ["haiku-4.5", "kimi-k2.6"]
      env="xiaomi/mimo", defaults=["haiku-4.5"]       → ["xiaomi/mimo", "haiku-4.5"]
      env="a,b", defaults=["c"]                        → ["a", "b"]
    """
    env_val = (os.getenv(env) or "").strip()
    if not env_val:
        return list(defaults)
    if "," in env_val:
        parts = [m.strip() for m in env_val.split(",") if m.strip()]
        return parts or list(defaults)
    primary = env_val
    chain = [primary]
    for d in defaults:
        if d and d != primary:
            chain.append(d)
    return chain


def narrow_chain(env: str = "IDEA_NARROW_MODEL") -> list[str]:
    """Narrow tier chain — env value + 안정 default fallback."""
    return model_chain_from_env(env, [DEFAULT_NARROW, DEFAULT_SUMMARY])


def synthesis_chain(env: str = "IDEA_SYNTHESIS_MODEL") -> list[str]:
    """Synthesis tier chain — env value + 안정 default fallback."""
    return model_chain_from_env(env, [DEFAULT_SYNTHESIS, "anthropic/claude-opus-4.7"])


def summary_chain() -> list[str]:
    """Summary tier chain — env(OPENROUTER_MODEL) value + 안정 default fallback."""
    return model_chain_from_env("OPENROUTER_MODEL", [DEFAULT_SUMMARY, DEFAULT_NARROW])


def chained_model(envs: Iterable[str], default: str) -> str:
    """env 이름 여러 개 순회 — 첫 set된 값. 모두 없으면 default.

    예: EarningsBot의 EARNINGS_SYNTHESIS_MODEL > IDEA_SYNTHESIS_MODEL > opus 기본:
        chained_model(["EARNINGS_SYNTHESIS_MODEL", "IDEA_SYNTHESIS_MODEL"], "anthropic/claude-opus-4.7")
    """
    for env in envs:
        v = os.getenv(env)
        if v:
            return v
    return default


def maybe_raise_credit(e: Exception) -> None:
    """OpenRouter credit/key-limit 오류면 OpenRouterCreditExhausted 재라이즈.

    그렇지 않으면 no-op. 모든 LLM 호출의 except 블록에서 가장 먼저 호출하면
    credit error를 silent 실패 대신 사용자에게 명확히 전달.

    summarizer 모듈에 의존 — circular import 회피 위해 lazy import.
    """
    from src import summarizer
    if isinstance(e, summarizer.APIStatusError) and summarizer._is_credit_error(e):
        raise summarizer.OpenRouterCreditExhausted(str(e)) from e
