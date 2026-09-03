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


def _excluded_providers() -> set[str]:
    """추천에서 제외할 프로바이더 prefix (콤마 구분 env).

    기본 "anthropic" — 사용자 지시 (2026-09-03): Anthropic 모델 추천 금지.
    빈 문자열로 설정하면 제외 없음 (지시 해제 시).
    """
    raw = os.getenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", "anthropic")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0].lower()


def _passes_constraints(model: dict, tier: str) -> bool:
    c = TIER_CONSTRAINTS.get(tier, {})
    if c.get("min_ctx") and model.get("ctx_length", 0) < c["min_ctx"]:
        return False
    if c.get("max_out_price") is not None and model["out_price"] > c["max_out_price"]:
        return False
    # 품질 티어 가격 하한 — 증거 없는 신모델은 스코어가 비용으로 붕괴해
    # 초소형 flash가 synthesis에 올라오는 것 방지 (가격 = 크기의 대리 지표).
    if c.get("min_out_price") is not None:
        if model["out_price"] < c["min_out_price"]:
            return False
        # 이름 기반 소형/특화 라인 제외 — flash·lite·mini 등은 가격 하한을
        # 넘어도 한국어 장문 합성용이 아님. code 특화도 narrative 부적합.
        mid_low = model.get("id", "").lower()
        if any(k in mid_low for k in ("flash", "lite", "mini", "nano", "tiny", "-code", "codex")):
            return False
    sp = model.get("supported", [])
    if sp:  # supported 정보가 있을 때만 검사 (구버전 캐시 호환)
        if c.get("need_json") and not ("response_format" in sp or "structured_outputs" in sp):
            return False
        if c.get("need_reasoning") and "reasoning" not in sp:
            return False
    return True


def _discover_candidates(tier: str, all_models: dict) -> list[str]:
    """라이브 카탈로그 전체에서 티어 제약을 만족하는 모델 자동 발굴.

    수동 큐레이션(TIER_CANDIDATES)만 보면 신모델을 영영 못 찾는 병목 —
    사용자 지적(2026-09) 반영. :free/:batch 변형은 실시간 운용 불가라 제외.
    perplexity 전용 티어(B_research)는 발굴 대상 아님 (검색 내장 필요).
    """
    if tier == "B_research":
        return []
    out = []
    for mid, m in all_models.items():
        if ":" in mid:          # :free, :batch, :extended 등 변형 제외
            continue
        if _passes_constraints(m, tier):
            out.append(mid)
    return out


def _rank_tier(tier: str, all_models: dict, activity: dict) -> list[dict]:
    """티어 후보를 점수순으로 정렬 — top N.

    필터 2종:
      1. 제외 프로바이더 (기본 anthropic — MODEL_ROUTER_EXCLUDE_PROVIDERS)
      2. X_fallback은 Summary 1차와 같은 프로바이더 제외 — "fallback은 1차와
         프로바이더 분리" 원칙 (상관 장애 회피)을 랭킹 단계에서 강제.
    automatic 업그레이드도 best 랭킹을 거치므로 여기서 걸러지면 어느 경로로도
    추천 불가.
    """
    # 큐레이션 목록 + 라이브 자동 발굴 합집합 (순서 유지 dedup)
    curated = TIER_CANDIDATES.get(tier, [])
    discovered = _discover_candidates(tier, all_models)
    candidates = list(dict.fromkeys([*curated, *discovered]))
    excluded = _excluded_providers()
    fallback_conflict: str | None = None
    if tier == "X_fallback":
        from src.llm_models import DEFAULT_SUMMARY
        primary = os.getenv("OPENROUTER_MODEL") or DEFAULT_SUMMARY
        fallback_conflict = _provider_of(primary)
    scored = []
    for mid in candidates:
        if _provider_of(mid) in excluded:
            continue
        if fallback_conflict and _provider_of(mid) == fallback_conflict:
            continue
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


# 마지막 평가의 티어별 현황 — handler가 메시지에 "왜 추천이 안 떴는지" 표시용.
# in-place mutate (clear/append)만 사용 — from-import 바인딩 stale 방지.
LAST_EVAL_STATUS: list[dict] = []


def _note_status(env_name: str, current: str | None, best: str | None, state: str) -> None:
    LAST_EVAL_STATUS.append(
        {"env_name": env_name, "current": current, "best": best, "state": state})


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

    LAST_EVAL_STATUS.clear()
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
                _note_status(env_name, old_val, best["model_id"], "최적")
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
                        _note_status(env_name, old_val, best["model_id"],
                                     f"보류 (점수차 {best['total'] - old_score['total']:+.2f} < {SKIP_MARGIN})")
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
            _note_status(env_name, old_val, best["model_id"], "추천")
    log.info("[model_router.recommender] %d recommendations (auto=%d suggest=%d)",
             len(out),
             sum(1 for x in out if x["classification"] == "automatic"),
             sum(1 for x in out if x["classification"] == "suggest"))
    return out
