"""recommender 프로바이더 제외 + fallback 분리 필터 검증."""
from __future__ import annotations

from src.model_router import recommender as rc


def _fake_models():
    mk = lambda mid, out=1.0: {"model_id": mid, "id": mid, "ctx_length": 1_000_000,
                               "out_price": out, "in_price": 0.5}
    return {m["model_id"]: m for m in [
        mk("anthropic/claude-sonnet-4.6"), mk("anthropic/claude-haiku-4.5"),
        mk("deepseek/deepseek-v4-pro"), mk("deepseek/deepseek-v4-flash"),
        mk("moonshotai/kimi-k2.6"), mk("qwen/qwen3.7-plus"),
    ]}


def test_anthropic_excluded_by_default(monkeypatch):
    monkeypatch.delenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", raising=False)
    monkeypatch.setattr(rc, "score_model",
                        lambda m, a, tier=None: {"model_id": m["model_id"], "total": 1.0})
    ranked = rc._rank_tier("D_synthesis", _fake_models(), {})
    ids = [r["model_id"] for r in ranked]
    assert ids, "후보가 비면 안 됨"
    assert not any(i.startswith("anthropic/") for i in ids), f"anthropic 미제외: {ids}"


def test_exclusion_disable(monkeypatch):
    """env를 빈 문자열로 → 제외 해제 (사용자가 지시 철회 시)."""
    monkeypatch.setenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", "")
    assert rc._excluded_providers() == set()


def test_fallback_provider_separation(monkeypatch):
    """X_fallback은 Summary 1차(deepseek)와 같은 프로바이더 제외."""
    monkeypatch.delenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", raising=False)
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setattr(rc, "score_model",
                        lambda m, a, tier=None: {"model_id": m["model_id"], "total": 1.0})
    ranked = rc._rank_tier("X_fallback", _fake_models(), {})
    ids = [r["model_id"] for r in ranked]
    assert not any(i.startswith("deepseek/") for i in ids), f"deepseek 미분리: {ids}"
    assert not any(i.startswith("anthropic/") for i in ids)
