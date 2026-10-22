"""Layer D — 자동 rollback. should_rollback 시 env history에서 이전 모델 복원."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import approval_store, health, railway_env
from .candidates import TIER_ENV
from .recommender import _excluded_providers, _provider_of

log = logging.getLogger(__name__)

# 최근 이 시간 안에 OpenRouter 401 breaker가 열렸으면 Layer D 판정 자체를 건너뜀 —
# 실패율이 키 장애로 오염됐을 수 있고, 롤백해도 같은 키라 의미 없음 (2026-10-21 사고).
AUTH_GUARD_S = 7200


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


def _manual_instruction(env_name: str, model: str) -> str:
    return (f"Railway 대시보드 Variables에서 `{env_name}` = `{model}` 로 수정 후 재배포")


def check_and_rollback() -> list[dict]:
    """현재 env 전체 순회 → should_rollback 시 이전 모델 복원.

    가드 (2026-10-21 키 유출·disable 사고 후속):
      1. 401 breaker가 최근 2h 안에 열렸으면 판정 skip (로그 1줄, 메시지 없음).
      2. 서로 다른 모델 2개+가 같은 창에서 실패율 초과 → 플랫폼/키 장애로 보고
         롤백하지 않고 systemic action 1건만 반환.
      3. Railway 토큰 미설정이면 upsert 시도 없이 수동 적용 안내.

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

    # systemic 가드 — 모델 2개+ 동시 실패는 모델 탓이 아님 (롤백해도 같은 장애)
    failing = health.failing_models([m for _, m in env_models])
    if len(failing) >= 2:
        reason = f"모델 {len(failing)}개 동시 실패율 초과: {', '.join(failing)}"
        log.warning("[rollback] systemic 장애 의심 — 롤백 보류 (%s)", reason)
        return [{"env_name": None, "from": None, "to": None, "reason": reason,
                 "applied": False, "note": "systemic", "models": failing}]

    can_upsert = railway_env.available()
    actions = []
    for env_name, cur in env_models:
        need, reason = health.should_rollback(cur, env_name=env_name)
        if not need:
            continue
        prev = _previous_model_for_env(env_name, cur)
        if not prev:
            log.warning("[rollback] %s 롤백 필요 (%s) but history 없음 — skip", env_name, reason)
            health.reset(cur)  # 같은 샘플로 매시간 재경고 방지 — 다음 1h 데이터로 재판정
            actions.append({"env_name": env_name, "from": cur, "to": None,
                            "reason": reason, "applied": False,
                            "note": "history 없음"})
            continue
        if not can_upsert:
            # 토큰 없음(유출 후 미발급 등) — 실패할 upsert 시도 대신 수동 안내
            log.warning("[rollback] %s 롤백 필요 (%s) — Railway 토큰 미설정, 수동 안내: → %s",
                        env_name, reason, prev)
            health.reset(cur)  # 같은 샘플로 매시간 재경고 방지
            actions.append({"env_name": env_name, "from": cur, "to": prev,
                            "reason": reason, "applied": False, "note": "manual",
                            "manual": _manual_instruction(env_name, prev)})
            continue
        ok, msg = railway_env.upsert_variable(env_name, prev)
        if ok:
            health.reset(cur)  # oscillation 방지
            approval_store.append_history({
                "env_name": env_name,
                "old_model": cur, "new_model": prev,
                "classification": "auto_rollback",
                "reason": reason,
                "status": "auto_rollback",
                "resolved_at": int(__import__("time").time()),
            })
            log.warning("[rollback] %s: %s → %s (%s)", env_name, cur, prev, reason)
        action = {
            "env_name": env_name, "from": cur, "to": prev,
            "reason": reason, "applied": ok, "note": msg,
        }
        if not ok:
            action["manual"] = _manual_instruction(env_name, prev)
        actions.append(action)
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
            lines.append(f"   {a['manual']} (Railway API 토큰 미설정)")
            lines.append(f"   사유: {a['reason']}")
        else:
            lines.append(f"\n⚠ `{a['env_name']}` 롤백 불가: {a.get('note','')}")
            if a.get("manual"):
                lines.append(f"   수동: {a['manual']}")
            lines.append(f"   사유: {a['reason']}")
    return "\n".join(lines)
