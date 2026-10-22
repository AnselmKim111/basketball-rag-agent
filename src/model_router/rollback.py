"""Layer D — 자동 rollback. should_rollback 시 env history에서 이전 모델 복원."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from . import approval_store, health, railway_env
from .candidates import TIER_ENV
from .recommender import _excluded_providers, _provider_of

log = logging.getLogger(__name__)

# 최근 이 시간 안에 OpenRouter 401 breaker가 열렸으면 Layer D 판정 자체를 건너뜀 —
# 실패율이 키 장애로 오염됐을 수 있고, 롤백해도 같은 키라 의미 없음 (2026-10-21 사고).
AUTH_GUARD_S = 7200
# systemic 알림 dedup — 마지막 systemic 알림 후 이 시간 안엔, 실패 *프로바이더*가 새로
# 늘지 않는 한 재알림 안 함. 모델 집합으로 비교하면 장애 중 모델별 호출량이 샘플 하한을
# 넘나들며 집합이 매시간 바뀌어 매번 재알림됨 (2026-10-22 시뮬: 3h 장애에 3회).
# health.reset도 소용없음 (시간 bucket 창 + :17 cron이라 장애가 이어지면 다시 채워짐).
SYSTEMIC_ALERT_DEDUP_S = 6 * 3600


def _previous_model_for_env(env_name: str, current: str) -> str | None:
    """history.json에서 env_name의 직전 값 (현재 값 직전 적용분) 찾기.

    적용 순서: history 가장 최근 status='approved' or 'auto_applied' 항목 중
    env_name 일치 + new_model != current 인 것의 old_model.
    제외 프로바이더(recommender._excluded_providers — 기본 anthropic, 사용자 지시
    2026-09-03) 모델은 후보에서 빼고 더 과거 항목을 계속 탐색.
    없으면 None.
    """
    p = approval_store._history_path()
    if not p.exists():
        return None
    try:
        history = json.loads(p.read_text())
    except Exception:
        return None
    excluded = _excluded_providers()
    # 최근 → 과거 순으로 탐색
    for item in reversed(history):
        if item.get("env_name") != env_name:
            continue
        if item.get("status") not in ("approved", "auto_applied"):
            continue
        old = item.get("old_model")
        if item.get("new_model") == current and old and old != current:
            if _provider_of(old) in excluded:
                log.info("[rollback] %s 후보 %s — 제외 프로바이더라 skip", env_name, old)
                continue
            return old
    return None


def _auth_recently_failed() -> bool:
    """summarizer 401 breaker가 최근 AUTH_GUARD_S 안에 열렸는지. import 실패 시 False."""
    try:
        from src import summarizer
        return summarizer.auth_breaker_tripped_within(AUTH_GUARD_S)
    except Exception:
        return False


def _now() -> float:
    """wall clock (테스트에서 monkeypatch로 시간 이동)."""
    return time.time()


def _systemic_alert_path() -> Path:
    return health._store_dir() / "systemic_alert.json"


def _last_systemic_providers(now: float) -> set[str] | None:
    """SYSTEMIC_ALERT_DEDUP_S 안의 마지막 systemic 알림이 다룬 프로바이더 집합. 없거나
    창 밖·읽기 실패면 None. 구버전 기록(models만 있음)은 모델에서 프로바이더 추출."""
    try:
        data = json.loads(_systemic_alert_path().read_text())
        if now - float(data.get("ts") or 0) >= SYSTEMIC_ALERT_DEDUP_S:
            return None
        provs = data.get("providers")
        if provs is None:
            provs = [_provider_of(m) for m in data.get("models") or []]
        return {str(x) for x in provs}
    except Exception:
        return None


def _note_systemic_alert(providers: set[str], now: float) -> None:
    try:
        _systemic_alert_path().write_text(json.dumps({"ts": now, "providers": sorted(providers)}))
    except Exception:
        log.exception("[rollback] systemic 알림 기록 실패 (dedup 없이 진행)")


def _manual_instruction(env_name: str, model: str) -> str:
    return (f"Railway 대시보드 Variables에서 `{env_name}` = `{model}` 로 수정 후 재배포")


def check_and_rollback() -> list[dict]:
    """현재 env 전체 순회 → should_rollback 시 이전 모델 복원.

    가드 (2026-10-21 키 유출·disable 사고 후속):
      1. 401 breaker가 최근 2h 안에 열렸으면 판정 skip (로그 1줄, 메시지 없음).
      2. 롤백이 필요한 env가 있을 때만, 실패율 초과 모델들이 서로 다른 *프로바이더* 2개+에
         걸치면 → 플랫폼/키 장애로 보고 롤백하지 않고 systemic action 1건만 반환. 집계는
         health에 기록된 모든 모델(chain 2차·폴백 포함), *실패* ≥ health.SIGNAL_MIN_SAMPLES(3)
         + 실패율 초과 — 저볼륨 티어도 증거로 세되 평시 우발 실패 1건(1/3)은 제외.
         롤백 대상 env가 없으면 systemic 판정·알림 자체를 생략 (평시 오경보 방지).
         알림은 6h 내 실패 프로바이더가 새로 늘 때만 재발송.
         한 프로바이더 장애(예: deepseek flash+pro 동시 실패)는 모델 문제 — env별 정상 롤백
         (이전 모델은 다른 프로바이더라 롤백이 곧 복구).
      3. 이전 모델이 지금 실패 중(실패 ≥3·실패율 초과)이면 그 env 롤백 보류 (실패 중인
         모델로 옮기지 않음 — note='held', 증거 유지해 다음 시간 재판정).
      4. Railway 토큰 probe — 'invalid'(미설정/폐기)면 upsert 시도 없이 수동 적용 안내,
         'unknown'(Railway API 일시 장애)이면 upsert 실패와 같게 증거 유지·다음 시간 재시도
         (+수동 안내 병기 — '무효'로 오안내하지 않음).

    판정을 전 env에 대해 먼저 끝낸 뒤 적용하고, health.reset은 루프 뒤 모델당 1회 —
    env 하나 롤백하며 공유 모델(OPENROUTER_MODEL+IDEA_NARROW_MODEL=v4-flash 등) 샘플을
    지우면 같은 모델의 다른 env가 같은 회차에 판정에서 빠짐 (2026-10-22 시뮬).

    반환: [{env_name, from, to, reason, applied, note}] list.
    """
    if _auth_recently_failed():
        log.warning("[rollback] OpenRouter 401 breaker 최근 %dh 내 작동 — Layer D 판정 skip",
                    AUTH_GUARD_S // 3600)
        return []

    # env → 현재 모델 (중복 env 제거)
    env_models: list[tuple[str, str]] = []
    seen_envs = set()
    for envs in TIER_ENV.values():
        for env_name in envs:
            if env_name in seen_envs:
                continue
            seen_envs.add(env_name)
            cur = os.getenv(env_name)
            if cur:
                env_models.append((env_name, cur))

    # 1) 판정 — 전 env 먼저 (reset이 공유 모델 샘플을 지우기 전에, systemic 가드보다 먼저:
    #    롤백할 env가 없으면 systemic 판정·알림도 불필요)
    needed = []
    for env_name, cur in env_models:
        need, reason = health.should_rollback(cur, env_name=env_name)
        if need:
            needed.append((env_name, cur, reason))
    if not needed:
        return []

    # systemic 가드 — 프로바이더 2개+에 걸친 동시 실패는 모델 탓이 아님 (롤백해도 같은 장애).
    # 모델 수가 아니라 프로바이더 수로 판정: 한 프로바이더 장애로 같은 회사 모델 2개
    # (deepseek-v4-flash + v4-pro)가 함께 실패하는 건 Layer D가 롤백해야 하는 경우.
    # 실패 *건수* 하한(SIGNAL_MIN_SAMPLES): 샘플 하한만이면 저볼륨 모델의 우발 실패 1건
    # (gpt-5.2 1/3 timeout + qwen 1/4 빈 응답)이 systemic으로 잡혀 deepseek 단독 장애 롤백을 막음.
    failing = health.failing_models(None, min_samples=health.SIGNAL_MIN_SAMPLES,
                                    min_fails=health.SIGNAL_MIN_SAMPLES)
    providers = {_provider_of(m) for m in failing}
    if len(providers) >= 2:
        reason = (f"프로바이더 {len(providers)}곳({', '.join(sorted(providers))}) 모델 "
                  f"{len(failing)}개 동시 실패율 초과: {', '.join(failing)}")
        now = _now()
        last = _last_systemic_providers(now)
        if last is not None and providers <= last:
            log.warning("[rollback] systemic 장애 지속 — 롤백 보류, %dh 내 알림한 프로바이더라 재알림 생략 (%s)",
                        SYSTEMIC_ALERT_DEDUP_S // 3600, reason)
            return []
        _note_systemic_alert(providers | (last or set()), now)
        log.warning("[rollback] systemic 장애 의심 — 롤백 보류 (%s)", reason)
        return [{"env_name": None, "from": None, "to": None, "reason": reason,
                 "applied": False, "note": "systemic", "models": failing,
                 "providers": sorted(providers)}]

    # 2) 적용
    token_state: str | None = None  # 필요할 때만 토큰 probe (평시 매시간 Railway 호출 방지), 회차당 1회
    actions = []
    reset_ok: set[str] = set()     # 처리 끝난 모델 → 루프 뒤 reset
    keep: set[str] = set()         # 보류·upsert 실패·Railway 일시 장애 env가 남은 모델 → 증거 유지 (다음 시간 재시도)
    for env_name, cur, reason in needed:
        prev = _previous_model_for_env(env_name, cur)
        if not prev:
            log.warning("[rollback] %s 롤백 필요 (%s) but history 없음 — skip", env_name, reason)
            reset_ok.add(cur)  # 같은 샘플로 매시간 재경고 방지 — 다음 1h 데이터로 재판정
            actions.append({"env_name": env_name, "from": cur, "to": None,
                            "reason": reason, "applied": False,
                            "note": "history 없음"})
            continue
        if health.is_failing(prev, min_samples=health.SIGNAL_MIN_SAMPLES,
                             min_fails=health.SIGNAL_MIN_SAMPLES):
            st = health.recent_stats(prev)
            log.warning("[rollback] %s 롤백 보류 — 이전 모델 %s도 실패 중 (%d건 실패율 %.0f%%)",
                        env_name, prev, st["samples"], st["fail_rate"] * 100)
            keep.add(cur)
            actions.append({"env_name": env_name, "from": cur, "to": prev,
                            "reason": reason, "applied": False, "note": "held",
                            "prev_stats": f"{st['samples']}건 실패율 {st['fail_rate']:.0%}"})
            continue
        if token_state is None:
            # available()은 설정 존재만 (네트워크 없음) — 없으면 probe 호출 없이 'invalid'
            token_state = (railway_env.probe() if railway_env.available()
                           else railway_env.PROBE_INVALID)
        if token_state == railway_env.PROBE_UNKNOWN:
            # Railway API 일시 장애(네트워크·5xx·429) — 토큰 무효가 아님. upsert 실패와 같게
            # 증거 유지해 다음 시간 재시도 (reset하면 1시간 손해 + '무효' 오안내였음)
            log.warning("[rollback] %s 롤백 필요 (%s) — Railway API 일시 장애, 다음 시간 재시도: → %s",
                        env_name, reason, prev)
            keep.add(cur)
            actions.append({"env_name": env_name, "from": cur, "to": prev,
                            "reason": reason, "applied": False,
                            "note": "⚠ Railway API 일시 장애 — 다음 시간 재시도",
                            "manual": _manual_instruction(env_name, prev)})
            continue
        if token_state != railway_env.PROBE_OK:
            # 토큰 없음/폐기(유출 후 미발급 등) — 실패할 upsert 시도 대신 수동 안내
            log.warning("[rollback] %s 롤백 필요 (%s) — Railway 토큰 미설정/무효, 수동 안내: → %s",
                        env_name, reason, prev)
            reset_ok.add(cur)  # 같은 샘플로 매시간 재경고 방지
            actions.append({"env_name": env_name, "from": cur, "to": prev,
                            "reason": reason, "applied": False, "note": "manual",
                            "manual": _manual_instruction(env_name, prev)})
            continue
        ok, msg = railway_env.upsert_variable(env_name, prev)
        if ok:
            reset_ok.add(cur)  # oscillation 방지
            approval_store.append_history({
                "env_name": env_name,
                "old_model": cur, "new_model": prev,
                "classification": "auto_rollback",
                "reason": reason,
                "status": "auto_rollback",
                "resolved_at": int(time.time()),
            })
            log.warning("[rollback] %s: %s → %s (%s)", env_name, cur, prev, reason)
        else:
            keep.add(cur)
        action = {
            "env_name": env_name, "from": cur, "to": prev,
            "reason": reason, "applied": ok, "note": msg,
        }
        if not ok:
            action["manual"] = _manual_instruction(env_name, prev)
        actions.append(action)

    # 3) reset — 모델당 1회, 그 모델에 보류·실패 env가 남아 있으면 유지
    for m in sorted(reset_ok - keep):
        health.reset(m)
    return actions


def format_rollback_message(actions: list[dict]) -> str:
    """텔레그램 알림 메시지 생성."""
    if not actions:
        return ""
    lines = ["⚠ *모델 자동 롤백*"]
    for a in actions:
        if a.get("note") == "systemic":
            lines.append("\n⏸ 플랫폼/키 장애 의심 — 모델 롤백 보류 (모델 문제 아님)")
            lines.append(f"   사유: {a['reason']}")
            lines.append("   OpenRouter 상태·키 유효성 확인 필요")
        elif a["applied"]:
            lines.append(f"\n✓ `{a['env_name']}`: {a['from']} → *{a['to']}*")
            lines.append(f"   사유: {a['reason']}")
        elif a.get("note") == "manual":
            lines.append(f"\n📝 `{a['env_name']}` 수동 롤백 필요: {a['from']} → *{a['to']}*")
            lines.append(f"   {a['manual']} (Railway API 토큰 미설정/무효)")
            lines.append(f"   사유: {a['reason']}")
        elif a.get("note") == "held":
            lines.append(f"\n⏸ `{a['env_name']}` 롤백 보류: {a['from']} → {a['to']} "
                         f"— 이전 모델도 실패 중 ({a.get('prev_stats', '')})")
            lines.append(f"   사유: {a['reason']}")
            lines.append("   다음 시간 재판정 (이전 모델 회복 시 롤백)")
        else:
            lines.append(f"\n⚠ `{a['env_name']}` 롤백 불가: {a.get('note','')}")
            if a.get("manual"):
                lines.append(f"   수동: {a['manual']}")
            lines.append(f"   사유: {a['reason']}")
    return "\n".join(lines)
