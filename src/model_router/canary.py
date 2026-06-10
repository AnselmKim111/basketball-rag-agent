"""Layer A — Canary smoke test.

모델 변경 직전 자동 실행. 4 티어 sentinel prompt → 결과 검증 → 통과 시에만 적용.
실패 시 변경 거부 + 텔레그램 보고.
"""
from __future__ import annotations

import logging
import os
import time

from src.summarizer import chat_with_retry, get_client

from .candidates import tier_of_env

log = logging.getLogger(__name__)


# --- validator 함수들 ---

def _v_korean_summary(content: str) -> tuple[bool, str]:
    """200-1500자 한국어 ratio ≥0.3."""
    n = len(content)
    if not (200 <= n <= 1500):
        return False, f"length {n} (200-1500 기대)"
    kor = sum(1 for c in content if '가' <= c <= '힣')
    ratio = kor / max(1, n)
    if ratio < 0.3:
        return False, f"korean ratio {ratio:.2f} <0.3"
    return True, ""


def _v_json_with_fields(*required_fields: str):
    """JSON 응답 + 필수 필드 존재 검증."""
    def _check(content: str) -> tuple[bool, str]:
        from src.llm_json import parse_json_object
        try:
            obj = parse_json_object(content)
        except Exception as e:
            return False, f"parse: {e}"
        if not isinstance(obj, dict):
            return False, "not dict"
        missing = [f for f in required_fields if f not in obj]
        if missing:
            return False, f"missing fields: {missing}"
        return True, ""
    return _check


def _v_nonempty(content: str) -> tuple[bool, str]:
    """단순 비어있지 않음 (research·extract tier 용)."""
    if len(content) < 50:
        return False, f"too short {len(content)}"
    return True, ""


# --- 티어별 sentinel ---

TIER_SMOKE: dict[str, list[dict]] = {
    "A_summary": [
        {
            "system": "당신은 PDF 요약 전문가. 한국어로 핵심만 압축.",
            "user": "다음 텍스트를 3-5문장 한국어 요약:\n\n"
                    "삼성전자 1분기 매출은 71조원으로 전년 동기 대비 12.8% 증가했다. "
                    "메모리 반도체 부문이 HBM 수요 폭증으로 흑자 전환했고, 파운드리는 적자 폭이 줄었다. "
                    "회사는 2분기 가이던스를 상향 조정하며 연간 영업이익 30조원을 목표로 한다고 밝혔다.",
            "max_tokens": 600,
            "temperature": 0.3,
            "validator": _v_korean_summary,
        },
    ],
    "C_narrow": [
        {
            "system": "당신은 종목 분석 JSON 출력기. 반드시 JSON 객체로만 응답.",
            "user": "다음 종목을 4축으로 점수화 (0-100). JSON 객체로 응답:\n"
                    "{\"tech_score\": 정수, \"demand_score\": 정수, "
                    "\"valuation_score\": 정수, \"momentum_score\": 정수}\n\n"
                    "종목: NVIDIA (NVDA, AI 반도체 리더, 시총 $3T)",
            "max_tokens": 400,
            "temperature": 0.2,
            "validator": _v_json_with_fields("tech_score", "demand_score", "valuation_score", "momentum_score"),
        },
    ],
    "D_synthesis": [
        {
            "system": "당신은 시장 분석가. JSON 객체로만 응답.",
            "user": "다음 thesis를 분석:\n"
                    "'AI 인프라 수요 폭증 + 메모리 사이클 회복'\n\n"
                    "JSON: {\"core_thesis\": 한 문장, \"bull\": 한 문장, \"bear\": 한 문장}",
            "max_tokens": 800,
            "temperature": 0.4,
            "validator": _v_json_with_fields("core_thesis", "bull", "bear"),
        },
    ],
    "E_deep": [
        {
            "system": "당신은 어닝 분석가. 한국어로 깊은 분석.",
            "user": "NVDA가 Q4 EPS $0.92 (추정 $0.84) · 매출 $35.1B (추정 $33.7B) 비트. "
                    "데이터센터 부문 +94% YoY. 향후 2-3 분기 전망을 한국어 200-400자로 정리.",
            "max_tokens": 600,
            "temperature": 0.3,
            "validator": _v_korean_summary,
        },
    ],
    "B_research": [
        {
            "system": "당신은 웹검색 어시스턴트.",
            "user": "오늘 미국 주식시장 주요 뉴스 1-2건 짧게 요약 (한국어).",
            "max_tokens": 400,
            "temperature": 0.3,
            "validator": _v_nonempty,
        },
    ],
    "X_fallback": [
        # fallback은 A_summary와 동일 검증
        {
            "system": "당신은 한국어 요약 전문가.",
            "user": "다음을 3문장 요약: NVIDIA가 Q4 어닝에서 데이터센터 매출 가이던스를 상향 조정.",
            "max_tokens": 400,
            "temperature": 0.3,
            "validator": _v_korean_summary,
        },
    ],
}

# 통과 임계 — 80% 이상 sentinel pass + 각 호출 60s 이내
PASS_RATIO = 0.8
MAX_LATENCY_S = 60


def run_canary(env_name: str, new_model: str) -> dict:
    """티어 추정 후 sentinel 실행. {passed, failures, latency_ms, sentinels} 반환.

    env_name 으로 tier 결정 → sentinel list → 각 호출 (model swap) → validator → 집계.
    """
    if os.getenv("MODEL_ROUTER_SKIP_CANARY", "0") == "1":
        log.info("[canary] MODEL_ROUTER_SKIP_CANARY=1 — skip")
        return {"passed": True, "failures": [], "latency_ms": 0, "sentinels": 0, "skipped": True}

    tier = tier_of_env(env_name)
    if not tier:
        log.warning("[canary] unknown env %s — skip", env_name)
        return {"passed": True, "failures": [], "latency_ms": 0, "sentinels": 0, "skipped": True}

    sentinels = TIER_SMOKE.get(tier, [])
    if not sentinels:
        return {"passed": True, "failures": [], "latency_ms": 0, "sentinels": 0, "skipped": True}

    failures = []
    latencies = []
    passed_count = 0

    client = get_client()
    for i, s in enumerate(sentinels):
        t0 = time.time()
        try:
            content = chat_with_retry(
                client,
                model=new_model,
                messages=[
                    {"role": "system", "content": s["system"]},
                    {"role": "user", "content": s["user"]},
                ],
                max_tokens=s["max_tokens"],
                temperature=s["temperature"],
                max_attempts=2,  # canary는 빠르게 — fallback 안 거침
                context=f"canary:{tier}:{i}",
            )
        except Exception as e:
            failures.append(f"[{i}] exception: {e}")
            continue
        elapsed = time.time() - t0
        latencies.append(int(elapsed * 1000))

        if not content:
            failures.append(f"[{i}] empty content (elapsed {elapsed:.1f}s)")
            continue
        if elapsed > MAX_LATENCY_S:
            failures.append(f"[{i}] too slow {elapsed:.1f}s")
            continue
        ok, err = s["validator"](content)
        if not ok:
            failures.append(f"[{i}] validator: {err}")
            continue
        passed_count += 1

    ratio = passed_count / max(1, len(sentinels))
    passed = ratio >= PASS_RATIO
    log.info("[canary] %s → %s: %d/%d pass (ratio=%.2f) latency_p50=%dms",
             env_name, new_model, passed_count, len(sentinels), ratio,
             sorted(latencies)[len(latencies) // 2] if latencies else 0)
    return {
        "passed": passed,
        "failures": failures,
        "latency_ms": max(latencies) if latencies else 0,
        "sentinels": len(sentinels),
        "tier": tier,
    }
