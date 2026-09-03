"""티어별 모델 후보 정의 + ENV 매핑.

각 티어에 어떤 ENV 변수가 속하는지 + 후보 모델이 만족해야 할 제약 (한국어, JSON, ctx 등).
"""
from __future__ import annotations

# 티어별 env var 매핑
TIER_ENV = {
    "A_summary":   ["OPENROUTER_MODEL"],
    "B_research":  ["IDEA_RESEARCH_MODEL", "REPORT_RESEARCH_MODEL", "EARNINGS_RESEARCH_MODEL"],
    "C_narrow":    ["IDEA_NARROW_MODEL", "INTENT_ROUTER_MODEL"],
    "D_synthesis": ["IDEA_SYNTHESIS_MODEL", "REPORT_SYNTHESIS_MODEL"],
    "E_deep":      ["EARNINGS_SYNTHESIS_MODEL"],
    "X_fallback":  ["OPENROUTER_FALLBACK_MODEL"],
}

# 각 티어 제약 — 후보 필터링 기준
TIER_CONSTRAINTS = {
    "A_summary":   {"min_ctx": 100_000, "max_out_price": 6.0, "need_json": True},
    "B_research":  {"need_web_tool": True},  # perplexity 전용
    "C_narrow":    {"min_ctx": 100_000, "max_out_price": 6.0, "need_json": True},
    "D_synthesis": {"min_ctx": 200_000, "need_reasoning": True, "max_out_price": 30.0, "min_out_price": 1.0},
    "E_deep":      {"min_ctx": 200_000, "need_reasoning": True, "max_out_price": 80.0, "min_out_price": 3.0},
    "X_fallback":  {"min_ctx": 100_000, "max_out_price": 6.0},
}

# 우리가 30일 이상 운영 검증한 모델 (안전 가중)
VERIFIED_MODELS = {
    "anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-4.7", "anthropic/claude-opus-4.8",
    "anthropic/claude-haiku-4.5",
    "moonshotai/kimi-k2.6",
    "perplexity/sonar-pro",
}

# 비교 페어 — automatic 분류 대상 (동가 업그레이드만, 비용·구조 동일 → 자동 적용)
AUTOMATIC_UPGRADES = {
    "anthropic/claude-sonnet-4.5": "anthropic/claude-sonnet-4.6",
    "anthropic/claude-sonnet-4.6": "anthropic/claude-sonnet-5",   # 신세대 + 33% 저렴
    "anthropic/claude-opus-4.7":   "anthropic/claude-opus-4.8",
    "anthropic/claude-opus-4.8":   "anthropic/claude-opus-5",     # 동가 신세대
    # opus 4.8 → 4.9 같은 미래 안전 업그레이드도 자동 (등록만 하면 됨)
}

# 티어별 명시 후보 (manual curation) — leaderboard 진입한 신모델은 여기에 추가
TIER_CANDIDATES = {
    "A_summary": [
        "moonshotai/kimi-k2.6",
        "deepseek/deepseek-v4-flash",
        "tencent/hy3-preview",
        "xiaomi/mimo-v2.5",
        "minimax/minimax-m3",
    ],
    "B_research": [
        "perplexity/sonar-pro",
    ],
    "C_narrow": [
        "deepseek/deepseek-v4-flash",   # 코드 기본값 — 후보 누락으로 mimo가 추천되던 구멍 (2026-09)
        "qwen/qwen3.7-plus",
        "anthropic/claude-haiku-4.5",
        "deepseek/deepseek-v3.2",
        "moonshotai/kimi-k2.6",
        "xiaomi/mimo-v2.5",
    ],
    "D_synthesis": [
        "openai/gpt-5.1",               # synthesis chain 2차 — 후보에도 노출
        "anthropic/claude-sonnet-5",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-sonnet-4.5",
        "deepseek/deepseek-v4-pro",
        "xiaomi/mimo-v2.5-pro",
        "z-ai/glm-5.1",
        "qwen/qwen3.7-plus",
    ],
    "E_deep": [
        "openai/gpt-5.2",               # 코드 기본값 (비-Anthropic 최상급)
        "openai/gpt-5.1",
        "qwen/qwen3.8-max",
        "anthropic/claude-opus-5",
        "anthropic/claude-opus-4.8",
        "anthropic/claude-opus-4.7",
        "anthropic/claude-sonnet-4.6",
        "deepseek/deepseek-v4-pro",
    ],
    "X_fallback": [
        "moonshotai/kimi-k2.6",
        "deepseek/deepseek-v4-flash",
    ],
}


def tier_of_env(env_name: str) -> str | None:
    """env var 이름 → 티어 키 반환."""
    for tier, envs in TIER_ENV.items():
        if env_name in envs:
            return tier
    return None
