"""Layer D — 모델별 호출 health 측정 + rollback 트리거.

summarizer.chat_with_retry가 호출별 (model, ok, latency_ms) 기록.
hourly bucket으로 누적 → recent_stats로 실패율·p95 계산 → should_rollback 판정.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# 임계
ROLLBACK_MIN_SAMPLES = 10
ROLLBACK_FAIL_RATE = 0.20
ROLLBACK_P95_LATENCY_S = 60  # env 미매칭 기본

# env(=작업 유형)별 p95 임계 — 작업 크기가 달라 단일 30s는 오탐 (2026-09-05 실측:
# v4-flash 8만자 PDF 요약 p95 70s → 정상인데 매시간 롤백 경고).
# 요약은 gather 병렬이라 p95 자체가 체감 지연이 아님. 분류(intent)만 타이트.
P95_LATENCY_BY_ENV_S = {
    "OPENROUTER_MODEL": 150,          # PDF 요약 (입력 8만자)
    "OPENROUTER_FALLBACK_MODEL": 150,
    "IDEA_NARROW_MODEL": 180,         # 30→10 JSON 12k 토큰 출력
    "IDEA_SYNTHESIS_MODEL": 240,      # Top5 합성
    "REPORT_SYNTHESIS_MODEL": 240,    # 시황 narrative
    "EARNINGS_SYNTHESIS_MODEL": 400,  # 어닝 비교합성 8000자+
    "IDEA_RESEARCH_MODEL": 120,       # perplexity 웹검색
    "REPORT_RESEARCH_MODEL": 120,
    "EARNINGS_RESEARCH_MODEL": 120,
    "INTENT_ROUTER_MODEL": 30,        # 짧은 분류 — 여기만 타이트
}


def p95_threshold_s(env_name: str | None) -> int:
    return P95_LATENCY_BY_ENV_S.get(env_name or "", ROLLBACK_P95_LATENCY_S)


def _store_dir() -> Path:
    for cand in ("/data/model_router", "reports/_model_router"):
        p = Path(cand)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    p = Path("reports/_model_router")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _health_path() -> Path:
    return _store_dir() / "health.json"


def _load() -> dict:
    p = _health_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        log.exception("[health] 로드 실패")
        return {}


def _save(data: dict) -> None:
    p = _health_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(p)  # atomic
    except Exception:
        log.exception("[health] 저장 실패")


def _current_bucket() -> str:
    """현재 시각 hourly bucket key (UTC)."""
    return time.strftime("%Y%m%d-%H", time.gmtime())


def record_call(model: str, ok: bool, latency_ms: int) -> None:
    """호출 1건 기록. model → bucket → {ok, fail, lat[]} 누적."""
    if not model:
        return
    data = _load()
    bucket = _current_bucket()
    m = data.setdefault(model, {})
    b = m.setdefault(bucket, {"ok": 0, "fail": 0, "lat": []})
    if ok:
        b["ok"] += 1
    else:
        b["fail"] += 1
    # latency 최근 50개만 (메모리 보호)
    b["lat"].append(latency_ms)
    if len(b["lat"]) > 50:
        b["lat"] = b["lat"][-50:]
    _save(data)


def recent_stats(model: str, hours: int = 1) -> dict:
    """최근 N시간 통계: {samples, fail_rate, p95_latency_ms}."""
    data = _load().get(model, {})
    if not data:
        return {"samples": 0, "fail_rate": 0.0, "p95_latency_ms": 0}
    now = time.gmtime()
    epoch = int(time.time())
    cutoff = epoch - hours * 3600
    ok = fail = 0
    lats = []
    for bucket, b in data.items():
        try:
            t = time.mktime(time.strptime(bucket, "%Y%m%d-%H"))
        except Exception:
            continue
        if t < cutoff:
            continue
        ok += b.get("ok", 0)
        fail += b.get("fail", 0)
        lats.extend(b.get("lat", []))
    samples = ok + fail
    fail_rate = fail / samples if samples else 0.0
    p95 = sorted(lats)[int(len(lats) * 0.95)] if lats else 0
    return {"samples": samples, "fail_rate": fail_rate, "p95_latency_ms": p95}


def should_rollback(model: str, env_name: str | None = None) -> tuple[bool, str]:
    """rollback 필요 여부 + 이유. samples 부족 시 False (insufficient data).

    env_name을 주면 그 작업 유형의 p95 임계 적용 (P95_LATENCY_BY_ENV_S)."""
    st = recent_stats(model, hours=1)
    if st["samples"] < ROLLBACK_MIN_SAMPLES:
        return False, f"insufficient samples ({st['samples']})"
    if st["fail_rate"] > ROLLBACK_FAIL_RATE:
        return True, f"fail_rate {st['fail_rate']:.0%} > {ROLLBACK_FAIL_RATE:.0%}"
    limit = p95_threshold_s(env_name)
    if st["p95_latency_ms"] / 1000 > limit:
        return True, f"p95 latency {st['p95_latency_ms']}ms > {limit}s ({env_name or 'default'})"
    return False, "healthy"


def reset(model: str) -> None:
    """rollback 후 모델 history 초기화 (oscillation 방지)."""
    data = _load()
    if model in data:
        del data[model]
        _save(data)
        log.info("[health] %s history reset", model)


def all_models_in_use() -> list[str]:
    """env에 set된 모든 모델 list (rollback 검사 대상)."""
    from .candidates import TIER_ENV
    out = []
    seen = set()
    for envs in TIER_ENV.values():
        for env_name in envs:
            val = os.getenv(env_name)
            if val and val not in seen:
                out.append(val)
                seen.add(val)
    return out
