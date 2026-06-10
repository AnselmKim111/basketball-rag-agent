"""티어별 최적 모델 추천 → 현재 env 값과 diff 생성.

automatic/suggest/skip 분류 후 텔레그램 메시지용 dict 반환.
"""
from __future__ import annotations

import logging
import os

from .candidates import (
    AUTOMATIC_UPGRADES, TIER_CANDIDATES, TIER_CONSTRAINTS, TIER_ENV,
)
from .fetcher import fetch_activity, fetch_models
from .scorer import score_model

log = logging.getLogger(__name__)

# 점수 차 5% 미만이면 추천 skip (noise)
SKIP_MARGIN = 0.05


def _passes_constraints(model: dict, tier: str) -> bool:
    c = TIER_CONSTRAINTS.get(tier, {})
    if c.get("min_ctx") and model.get("ctx_length", 0) < c["min_ctx"]:
        return False
    if c.get("max_out_price") is not None and model["out_price"] > c["max_out_price"]:
        return False
    return True


def _rank_tier(tier: str, all_models: dict, activity: dict) -> list[dict]:
    """티어 후보를 점수순으로 정렬 — top N."""
    candidates = TIER_CANDIDATES.get(tier, [])
    scored = []
    for mid in candidates:
        if mid not in all_models:
            continue
        m = all_models[mid]
        if not _passes_constraints(m, tier):
            continue
        s = score_model(m, activity, tier=tier)
        scored.append(s)
    scored.sort(key=lambda x: -x["total"])
    return scored


def _current_env_model(env_names: list[str]) -> tuple[str, str] | None:
    """env list 중 처음 set된 것 반환 (name, value). 없으면 None."""
    for name in env_names:
        val = os.getenv(name)
        if val:
            return (name, val)
    return None


def build_recommendations() -> list[dict]:
    """티어별 추천 list 반환.

    각 추천:
      {tier, env_name, old_model, new_model, classification, score_old, score_new,
       savings_pct, reason}
    classification: automatic | suggest | skip
    """
    all_models = fetch_models()
    if not all_models:
        log.warning("[model_router.recommender] models fetch 실패 — 추천 skip")
        return []
    activity = fetch_activity(days=30)

    out = []
    for tier, env_names in TIER_ENV.items():
        ranked = _rank_tier(tier, all_models, activity)
        if not ranked:
            continue
        best = ranked[0]
        for env_name in env_names:
            old_val = os.getenv(env_name)
            if not old_val:
                # env 미설정 — best 모델로 신규 권유
                out.append({
                    "tier": tier, "env_name": env_name,
                    "old_model": None, "new_model": best["model_id"],
                    "classification": "suggest",
                    "score_old": 0.0, "score_new": best["total"],
                    "savings_pct": None,
                    "reason": "env 미설정 — 권장값 신규 등록",
                })
                continue
            if old_val == best["model_id"]:
                continue  # 이미 최적

            # automatic upgrade 체크
            if AUTOMATIC_UPGRADES.get(old_val) == best["model_id"]:
                cls = "automatic"
                reason = "동가 안전 업그레이드 (가격·구조 동일, 신모델)"
            else:
                # 점수 차 5% 미만이면 skip
                old_meta = all_models.get(old_val)
                if old_meta:
                    old_score = score_model(old_meta, activity, tier=tier)
                    if best["total"] - old_score["total"] < SKIP_MARGIN:
                        continue
                    old_total = old_score["total"]
                    sav = None
                    if old_meta["out_price"] > 0:
                        sav = round((1 - best["out_price"] / old_meta["out_price"]) * 100, 1)
                else:
                    old_total = 0.0
                    sav = None
                cls = "suggest"
                reason = f"비용 절감 {sav}%" if sav and sav > 30 else "가성비 점수 우위"

            out.append({
                "tier": tier, "env_name": env_name,
                "old_model": old_val, "new_model": best["model_id"],
                "classification": cls,
                "score_old": round(old_total, 4) if cls != "automatic" else 0.0,
                "score_new": best["total"],
                "savings_pct": sav if cls != "automatic" else 0.0,
                "reason": reason,
            })
    log.info("[model_router.recommender] %d recommendations (auto=%d suggest=%d)",
             len(out),
             sum(1 for x in out if x["classification"] == "automatic"),
             sum(1 for x in out if x["classification"] == "suggest"))
    return out
