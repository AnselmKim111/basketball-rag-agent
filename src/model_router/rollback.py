"""Layer D — 자동 rollback. should_rollback 시 env history에서 이전 모델 복원."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import approval_store, health, railway_env
from .candidates import TIER_ENV

log = logging.getLogger(__name__)


def _previous_model_for_env(env_name: str, current: str) -> str | None:
    """history.json에서 env_name의 직전 값 (현재 값 직전 적용분) 찾기.

    적용 순서: history 가장 최근 status='approved' or 'auto_applied' 항목 중
    env_name 일치 + new_model != current 인 것의 old_model.
    없으면 None.
    """
    p = Path(approval_store._history_path() if hasattr(approval_store, '_history_path')
             else "/data/model_router/history.json")
    if not p.exists():
        return None
    try:
        history = json.loads(p.read_text())
    except Exception:
        return None
    # 최근 → 과거 순으로 탐색
    for item in reversed(history):
        if item.get("env_name") != env_name:
            continue
        if item.get("status") not in ("approved", "auto_applied"):
            continue
        if item.get("new_model") == current and item.get("old_model"):
            return item["old_model"]
    return None


def check_and_rollback() -> list[dict]:
    """현재 env 전체 순회 → should_rollback 시 이전 모델 복원.

    반환: [{env_name, from, to, reason, applied}] list.
    """
    actions = []
    seen_envs = set()
    for envs in TIER_ENV.values():
        for env_name in envs:
            if env_name in seen_envs:
                continue
            seen_envs.add(env_name)
            cur = os.getenv(env_name)
            if not cur:
                continue
            need, reason = health.should_rollback(cur)
            if not need:
                continue
            prev = _previous_model_for_env(env_name, cur)
            if not prev:
                log.warning("[rollback] %s 롤백 필요 (%s) but history 없음 — skip", env_name, reason)
                actions.append({"env_name": env_name, "from": cur, "to": None,
                                "reason": reason, "applied": False,
                                "note": "history 없음"})
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
            actions.append({
                "env_name": env_name, "from": cur, "to": prev,
                "reason": reason, "applied": ok, "note": msg,
            })
    return actions


def format_rollback_message(actions: list[dict]) -> str:
    """텔레그램 알림 메시지 생성."""
    if not actions:
        return ""
    lines = ["⚠ *모델 자동 롤백*"]
    for a in actions:
        if a["applied"]:
            lines.append(f"\n✓ `{a['env_name']}`: {a['from']} → *{a['to']}*")
            lines.append(f"   사유: {a['reason']}")
        else:
            lines.append(f"\n⚠ `{a['env_name']}` 롤백 불가: {a.get('note','')}")
            lines.append(f"   사유: {a['reason']}")
    return "\n".join(lines)
