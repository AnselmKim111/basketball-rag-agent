"""dedup state — 이미 보낸 rpt_id 추적.

저장 위치 우선순위:
  1) RAILWAY_VOLUME_MOUNT_PATH (Railway 볼륨)
  2) STATE_DIR 환경변수
  3) /app/data (도커 기본)
  4) /tmp/wisereport_state (fallback)

볼륨 없으면 컨테이너 재시작 시 state 손실 — 일부 리포트가 중복 발송될 수 있으나
치명적이지 않음.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# 같은 프로세스 내 동시 read-modify-write 직렬화 (TOCTOU 방지).
# orchestrator는 단일 프로세스라 이걸로 충분. 다중 프로세스 환경이면 fcntl
# 레벨 lock 도입 필요.
_LOCK = threading.RLock()


def _state_dir() -> Path:
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v)
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                continue
    for candidate in ("/data", "/app/data", "/tmp/wisereport_state"):
        try:
            p = Path(candidate)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return Path("/tmp")


STATE_FILE = _state_dir() / "seen_rpt_ids.json"


def _load() -> dict[str, list[str]]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("state 파일 로드 실패: %s", STATE_FILE)
        return {}


def _save(state: dict[str, list[str]]) -> None:
    """tmp 파일에 쓰고 원자적 rename — 동시 write 시 파일 손상 방지."""
    try:
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(STATE_FILE)
    except Exception:
        log.exception("state 파일 저장 실패: %s", STATE_FILE)


def seen(category: str) -> set[str]:
    """카테고리(예: 'industry_top10', 'strategy_daily')에서 이미 본 rpt_id 셋."""
    with _LOCK:
        return set(_load().get(category, []))


def mark_seen(category: str, rpt_ids: list[str], cap: int = 1000) -> None:
    """rpt_id 리스트를 이미 본 것으로 마킹. 카테고리당 최대 cap개 (오래된 것부터 삭제)."""
    with _LOCK:
        state = _load()
        cur = state.get(category, [])
        merged = []
        seen_set: set[str] = set()
        for r in cur + list(rpt_ids):
            if r not in seen_set:
                seen_set.add(r)
                merged.append(r)
        if len(merged) > cap:
            merged = merged[-cap:]
        state[category] = merged
        _save(state)
        log.info("state 갱신: %s에 %d개 추가 (총 %d)", category, len(rpt_ids), len(merged))


def reset(category: str | None = None) -> None:
    with _LOCK:
        if category is None:
            _save({})
            return
        state = _load()
        state.pop(category, None)
        _save(state)
