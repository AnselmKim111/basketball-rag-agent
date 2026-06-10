"""모델 가성비 점수 계산 — 티어별 가중.

가성비 티어 (A_summary/C_narrow/X_fallback): cost 0.4 — 저렴함이 곧 가치.
품질 티어 (D_synthesis/E_deep): evidence 0.45·cost 0.15 — 검증된 지능 우선.
  (한국어 narrative·deep reasoning은 미검증 모델 silent 품질 저하 위험이 절감액보다 큼)
"""
from __future__ import annotations

import math
import time

from .candidates import VERIFIED_MODELS

# 티어별 가중 (cost, evidence, freshness, ctx, bonus) — 합 1.0
_DEFAULT_WEIGHTS = (0.40, 0.25, 0.15, 0.10, 0.10)
TIER_WEIGHTS: dict[str, tuple[float, float, float, float, float]] = {
    "A_summary":   _DEFAULT_WEIGHTS,
    "B_research":  _DEFAULT_WEIGHTS,
    "C_narrow":    _DEFAULT_WEIGHTS,
    "X_fallback":  _DEFAULT_WEIGHTS,
    # 품질 티어 — 검증 우선, 비용 가중 낮춤
    "D_synthesis": (0.15, 0.45, 0.15, 0.10, 0.15),
    "E_deep":      (0.10, 0.50, 0.15, 0.10, 0.15),
}


def cost_score(in_price: float, out_price: float) -> float:
    """비용 점수 — 출력 3배 가중 (실측 in:out 비중 반영)."""
    eff = in_price + 3.0 * out_price  # $/M 가중
    if eff <= 0:
        return 0.0
    # 정규화: $0.01/M (최저) → 1.0, $80/M (최고 opus-fast) → 0.05
    return min(1.0, 1.0 / (1.0 + eff / 5.0))


def usage_evidence(model_id: str, activity: dict[str, dict] | None = None) -> float:
    """실제 운영 검증 점수.

    - VERIFIED 모델: 1.0
    - 30일 활동 데이터 있음 (우리가 써본): 0.85
    - 그 외 (미검증): 0.4
    """
    if model_id in VERIFIED_MODELS:
        return 1.0
    if activity and model_id in activity and activity[model_id].get("req", 0) > 5:
        return 0.85
    return 0.4


def freshness(created_at: int) -> float:
    """신선도 — 6개월 이내 1.0, 1년 이내 0.7, 2년+ 0.4."""
    if not created_at:
        return 0.5
    now = int(time.time())
    age_days = max(0, (now - int(created_at)) // 86400)
    if age_days < 180:
        return 1.0
    if age_days < 365:
        return 0.7
    if age_days < 730:
        return 0.5
    return 0.3


def ctx_score(ctx_length: int) -> float:
    """컨텍스트 길이 점수 — 200K 이상 동일 가치."""
    if not ctx_length:
        return 0.0
    return min(1.0, ctx_length / 200_000)


def score_model(model: dict, activity: dict[str, dict] | None = None,
                tier_bonus: float = 0.0, tier: str | None = None) -> dict:
    """모델 1개 → 점수 dict (cost/evidence/freshness/ctx/total + 세부).

    tier 지정 시 TIER_WEIGHTS 적용 — 품질 티어(D/E)는 evidence 우선.
    """
    mid = model["id"]
    cs = cost_score(model["in_price"], model["out_price"])
    ue = usage_evidence(mid, activity)
    fr = freshness(model.get("created_at", 0))
    cx = ctx_score(model.get("ctx_length", 0))
    w_cost, w_ev, w_fr, w_cx, w_bonus = TIER_WEIGHTS.get(tier or "", _DEFAULT_WEIGHTS)
    total = cs * w_cost + ue * w_ev + fr * w_fr + cx * w_cx + tier_bonus * w_bonus
    return {
        "model_id": mid,
        "total": round(total, 4),
        "cost": round(cs, 3),
        "evidence": round(ue, 3),
        "freshness": round(fr, 3),
        "ctx": round(cx, 3),
        "in_price": model["in_price"],
        "out_price": model["out_price"],
        "ctx_length": model.get("ctx_length", 0),
    }
