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
import tempfile
from pathlib import Path

from filelock import FileLock

log = logging.getLogger(__name__)


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
_LOCK_FILE = STATE_FILE.with_suffix(STATE_FILE.suffix + ".lock")
_FILE_LOCK = FileLock(str(_LOCK_FILE))


def _load() -> dict[str, list[str]]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("state 파일 로드 실패: %s", STATE_FILE)
        return {}


def _save(state: dict[str, list[str]]) -> None:
    # 임시 파일에 쓰고 os.replace로 원자 교체 — 쓰기 도중 크래시해도 기존 파일은 온전.
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=STATE_FILE.name + ".",
            suffix=".tmp",
            dir=str(STATE_FILE.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, STATE_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        log.exception("state 파일 저장 실패: %s", STATE_FILE)


def seen(category: str) -> set[str]:
    """카테고리(예: 'industry_top10', 'strategy_daily')에서 이미 본 rpt_id 셋."""
    with _FILE_LOCK:
        return set(_load().get(category, []))


def mark_seen(category: str, rpt_ids: list[str], cap: int = 1000) -> None:
    """rpt_id 리스트를 이미 본 것으로 마킹. 카테고리당 최대 cap개 보관 (오래된 것부터 삭제).

    동일 STATE_FILE을 공유하는 다른 프로세스(예: orchestrator + CLI 동시 실행)와의
    read-modify-write 경합을 막기 위해 cross-process FileLock으로 직렬화한다.
    """
    with _FILE_LOCK:
        state = _load()
        cur = state.get(category, [])
        # 새 것을 뒤에 붙이고 중복 제거 (insertion order 보존)
        merged = []
        seen_set = set()
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
    with _FILE_LOCK:
        if category is None:
            _save({})
            return
        state = _load()
        state.pop(category, None)
        _save(state)
