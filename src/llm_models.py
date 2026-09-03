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


# 기본값 — 각 티어의 안전 fallback.
# 2026-09-03 라이브 가격 재평가 (openrouter /models 실측, $/1M in·out):
#   deepseek-v4-flash 0.089·0.177 (1M ctx, JSON OK) ← kimi-k2.6 0.95·4.00 대비 11-23x 절감
#   deepseek-v4-pro   1.042·2.085 ← sonnet 3.00·15.00 (synthesis fallback용)
#   haiku-4.5         1.00·5.00   → narrow 1차에서 v4-flash로 교체 (출력단가 28x)
# Summary·Narrow(가성비 티어)는 최저가로 공격 전환 + 품질 모델을 chain 2차로 유지.
# Synthesis·Deep(품질 티어)은 CLAUDE.md §2 가드레일대로 검증 모델 유지 — 승격은
# model_router 주간 평가 + canary 경로로만.
DEFAULT_SUMMARY = "deepseek/deepseek-v4-flash"
DEFAULT_NARROW = "deepseek/deepseek-v4-flash"
# 사용자 지시(2026-09-03): Anthropic 제외 — 비-Anthropic 중 최적 조합.
# v4-pro $1.04/$2.08 (JSON OK, repo 후보 1순위) + gpt-5.1 품질 백업.
DEFAULT_SYNTHESIS = "deepseek/deepseek-v4-pro"
DEFAULT_RESEARCH = "perplexity/sonar-pro"
DEFAULT_DEEP = "openai/gpt-5.2"             # 비-Anthropic 최상급 ($1.75/$14)

# chain 2차용 — 1차(초저가)와 프로바이더를 분리해 상관 장애 회피.
FALLBACK_SUMMARY = "moonshotai/kimi-k2.6"
FALLBACK_NARROW = "qwen/qwen3.7-plus"       # $0.32/$1.28, 1M ctx, JSON OK
FALLBACK_SYNTHESIS = "openai/gpt-5.1"       # 1차(deepseek)와 프로바이더 분리, 프론티어 백업


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
    """Narrow tier chain — 초저가 1차 + 검증된 품질 모델(haiku) 2차."""
    return model_chain_from_env(env, [DEFAULT_NARROW, FALLBACK_NARROW])


def synthesis_chain(env: str = "IDEA_SYNTHESIS_MODEL") -> list[str]:
    """Synthesis tier chain — sonnet 1차 + 저가 고성능(v4-pro) 2차.

    이전 2차는 opus-4.7($25/M out)이었는데 fallback이 1차보다 비싼 역구조 —
    v4-pro($2.09/M out)로 교체. fallback은 1차 빈 응답 시에만 타므로 품질
    리스크 국소적, 비용 12x 절감.
    """
    return model_chain_from_env(env, [DEFAULT_SYNTHESIS, FALLBACK_SYNTHESIS])


def summary_chain() -> list[str]:
    """Summary tier chain — 초저가 1차 + 프로바이더 분리된 kimi 2차."""
    return model_chain_from_env("OPENROUTER_MODEL", [DEFAULT_SUMMARY, FALLBACK_SUMMARY])


def chained_model(envs: Iterable[str], default: str) -> str:
    """env 이름 여러 개 순회 — 첫 set된 값. 모두 없으면 default.

    예: EarningsBot의 EARNINGS_SYNTHESIS_MODEL > IDEA_SYNTHESIS_MODEL > opus 기본:
        chained_model(["EARNINGS_SYNTHESIS_MODEL", "IDEA_SYNTHESIS_MODEL"], DEFAULT_DEEP)
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
