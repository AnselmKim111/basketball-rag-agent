"""모델 가성비 점수 계산.

score = cost_score*0.4 + usage_evidence*0.25 + freshness*0.15 + ctx_score*0.1 + tier_bonus*0.1
"""
from __future__ import annotations

import math
import time

from .candidates import VERIFIED_MODELS


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
                tier_bonus: float = 0.0) -> dict:
    """모델 1개 → 점수 dict (cost/evidence/freshness/ctx/total + 세부)."""
    mid = model["id"]
    cs = cost_score(model["in_price"], model["out_price"])
    ue = usage_evidence(mid, activity)
    fr = freshness(model.get("created_at", 0))
    cx = ctx_score(model.get("ctx_length", 0))
    total = cs * 0.4 + ue * 0.25 + fr * 0.15 + cx * 0.1 + tier_bonus * 0.1
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
