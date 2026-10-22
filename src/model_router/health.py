"""Layer D — 모델별 호출 health 측정 + rollback 트리거.

summarizer.chat_with_retry가 호출별 (model, ok, latency_ms) 기록.
hourly bucket으로 누적 → recent_stats로 실패율·p95 계산 → should_rollback 판정.
창은 bucket 단위 — "최근 N시간"과 겹치는 bucket 전부라 실제로는 N~N+1시간 (:17 cron이
직전 시간 bucket을 보도록. 시작 시각 기준이면 직전 bucket이 통째로 빠져 17분치만 봄).
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# 임계
ROLLBACK_MIN_SAMPLES = 10
ROLLBACK_FAIL_RATE = 0.20
# 신호용 낮은 하한 — 롤백 판정(ROLLBACK_MIN_SAMPLES)이 아니라 (a) systemic 가드의
# 프로바이더 집계 (저볼륨 모델도 '같이 실패 중'이면 플랫폼 장애 증거), (b) 롤백 대상
# (이전 모델)이 지금 실패 중인지 확인, (c) should_rollback의 '현재 bucket 회복' 판정에 사용.
# 10건 하한이면 earnings·narrow 같은 저볼륨 티어는 장애 중에도 집계에서 빠짐 (2026-10-22
# 시뮬: gpt-5.2 8/8 실패 미집계). (a)(b)는 *실패 건수* 하한(min_fails)으로 씀 — 샘플 3건
# 하한이면 평시 저볼륨 모델의 timeout 1건(1/3)·빈 응답 1건(1/4)이 '실패 중'으로 잡혀
# systemic 오경보 + 진짜 단일 프로바이더 장애 롤백 차단 (2026-10-22 sim_systemic_fp).
SIGNAL_MIN_SAMPLES = 3
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
    """최근 N시간 통계: {samples, fails, fail_rate, p95_latency_ms}.

    [now-N시간, now]와 겹치는 hourly bucket 전부 집계 → 실제 창은 N~N+1시간.
    bucket 시작 시각으로 자르면 :17 cron에서 직전 시간 bucket(:00 시작)이 통째로 빠져
    현재 17분치만 보게 됨 (h0:50 실패 15건이 h1:17에 samples=0).
    hours=0이면 현재 시간 bucket만."""
    data = _load().get(model, {})
    if not data:
        return {"samples": 0, "fails": 0, "fail_rate": 0.0, "p95_latency_ms": 0}
    now = time.gmtime()
    epoch = int(time.time())
    cutoff = epoch - hours * 3600
    ok = fail = 0
    lats = []
    for bucket, b in data.items():
        try:
            # bucket은 UTC(gmtime) — mktime(로컬)로 읽으면 비-UTC TZ에서 창 밖으로 밀림
            t = calendar.timegm(time.strptime(bucket, "%Y%m%d-%H"))
        except Exception:
            continue
        if t + 3600 <= cutoff:  # bucket [t, t+1h)이 창과 안 겹칠 때만 제외
            continue
        ok += b.get("ok", 0)
        fail += b.get("fail", 0)
        lats.extend(b.get("lat", []))
    samples = ok + fail
    fail_rate = fail / samples if samples else 0.0
    p95 = sorted(lats)[int(len(lats) * 0.95)] if lats else 0
    return {"samples": samples, "fails": fail, "fail_rate": fail_rate, "p95_latency_ms": p95}


def should_rollback(model: str, env_name: str | None = None) -> tuple[bool, str]:
    """rollback 필요 여부 + 이유. samples 부족 시 False (insufficient data).

    env_name을 주면 그 작업 유형의 p95 임계 적용 (P95_LATENCY_BY_ENV_S).

    창(60~120분)이 임계를 넘어도 현재 시간 bucket만으로 샘플 ≥ SIGNAL_MIN_SAMPLES이고
    실패율·p95가 임계 이내면 (False, 'recovered (current bucket healthy)') — 이미 끝난 장애의
    꼬리(직전 bucket)로 회복된 모델을 롤백하지 않음 (2026-10-22 sim_f two: 복구 17분 뒤
    flash 21%로 불필요한 env 변경·재배포). 현재 bucket이 비었거나 얇으면(< 하한) 창 판정 유지
    (h0:50 실패 → h1:17 롤백, sim_f_window)."""
    st = recent_stats(model, hours=1)
    if st["samples"] < ROLLBACK_MIN_SAMPLES:
        return False, f"insufficient samples ({st['samples']})"
    limit = p95_threshold_s(env_name)
    if st["fail_rate"] > ROLLBACK_FAIL_RATE:
        reason = f"fail_rate {st['fail_rate']:.0%} > {ROLLBACK_FAIL_RATE:.0%}"
    elif st["p95_latency_ms"] / 1000 > limit:
        reason = f"p95 latency {st['p95_latency_ms']}ms > {limit}s ({env_name or 'default'})"
    else:
        return False, "healthy"
    cur = recent_stats(model, hours=0)
    if (cur["samples"] >= SIGNAL_MIN_SAMPLES and cur["fail_rate"] <= ROLLBACK_FAIL_RATE
            and cur["p95_latency_ms"] / 1000 <= limit):
        return False, "recovered (current bucket healthy)"
    return True, reason


def is_failing(model: str, min_samples: int = ROLLBACK_MIN_SAMPLES, hours: int = 1,
               min_fails: int = 0) -> bool:
    """샘플 ≥ min_samples · 실패 건수 ≥ min_fails 이고 실패율 > ROLLBACK_FAIL_RATE 인지
    (p95는 안 봄). min_fails는 저볼륨 모델의 우발 실패 1건(1/3 = 33%) 오판 방지용 절대 하한."""
    st = recent_stats(model, hours=hours)
    return (st["samples"] >= min_samples and st["fails"] >= min_fails
            and st["fail_rate"] > ROLLBACK_FAIL_RATE)


def failing_models(models: list[str] | None = None, hours: int = 1,
                   min_samples: int = ROLLBACK_MIN_SAMPLES, min_fails: int = 0) -> list[str]:
    """실패율 임계 초과 모델 (샘플 ≥ min_samples·실패 ≥ min_fails, 중복 제거·입력 순서 유지).

    models=None이면 health에 기록된 모든 모델(env 미설정 chain 2차·폴백 포함) 대상 —
    rollback.check_and_rollback의 systemic 가드가 min_samples=min_fails=SIGNAL_MIN_SAMPLES로
    호출해 실패 모델들의 *프로바이더* 수를 셈 (2곳+ 동시 실패 = 모델 문제가 아니라 플랫폼/키 장애).
    기본 min_samples(ROLLBACK_MIN_SAMPLES)는 롤백 판정과 같은 하한."""
    if models is None:
        models = list(_load())
    out: list[str] = []
    for m in dict.fromkeys(x for x in models if x):
        if is_failing(m, min_samples=min_samples, hours=hours, min_fails=min_fails):
            out.append(m)
    return out


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
