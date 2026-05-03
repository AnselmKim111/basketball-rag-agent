"""dedup state — 이미 보낸 rpt_id + 정규화된 title 추적.

저장 위치 우선순위:
  1) RAILWAY_VOLUME_MOUNT_PATH (Railway 볼륨)
  2) STATE_DIR 환경변수
  3) /data → /app/data → /tmp/wisereport_state (fallback)

볼륨 없으면 컨테이너 재시작 시 state 손실 — Railway는 반드시 볼륨을 /data
같은 경로에 attach하고 STATE_DIR=/data 설정 권장.

dedup은 두 축 모두 사용:
  - rpt_id: wisereport 고유 ID (정확하지만 같은 리포트가 다른 채널로
    재등록되면 새 id 부여돼 못 잡음)
  - 정규화 title: "[전략공감 2.0] X" 와 "X" 같은 prefix 차이 무시.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# 같은 프로세스 내 동시 read-modify-write 직렬화 (TOCTOU 방지).
# orchestrator는 단일 프로세스라 이걸로 충분. 다중 프로세스 환경이면 fcntl
# 레벨 lock 도입 필요.
_LOCK = threading.RLock()

# 정규화 title을 저장할 카테고리별 key suffix (state json 안에서).
_TITLE_KEY_SUFFIX = "__titles"


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


def diag_log() -> None:
    """orchestrator 부팅 시 호출. 실제 state 경로 + 영구 보존 여부 진단."""
    try:
        sd = STATE_FILE.parent
        existed = STATE_FILE.exists()
        size = STATE_FILE.stat().st_size if existed else 0
        # 볼륨 마운트 추정: /data 또는 RAILWAY_VOLUME_MOUNT_PATH 하위면 영구
        mount_hint = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("STATE_DIR") or ""
        persistent = bool(mount_hint) and str(sd).startswith(mount_hint)
        # 카테고리별 누적 통계
        st = _load()
        cats = {k: len(v) for k, v in st.items() if not k.endswith(_TITLE_KEY_SUFFIX)}
        log.info(
            "state_store 진단: path=%s, exists=%s, size=%d, persistent=%s, categories=%s",
            STATE_FILE, existed, size, persistent, cats or "(빈 state)",
        )
    except Exception:
        log.exception("state_store 진단 실패")


def normalize_title(title: str) -> str:
    """Title 정규화 — 같은 리포트가 다른 prefix/포맷으로 중복 등록되는 케이스 대응.

    예시 (모두 같은 키로 매핑됨):
      "구체화되는 AI 수익화, AI 인프라 투자의 정당성"
      "[전략공감 2.0] 구체화되는 AI 수익화, AI 인프라 투자의 정당성"
      "<NH> 구체화되는 AI 수익화, AI 인프라 투자의 정당성"
    """
    t = title or ""
    # 시작 위치의 [...]·<...>·(...) prefix 한 번 제거
    t = re.sub(r"^\s*[\[\<\(][^\]\>\)]+[\]\>\)]\s*", "", t)
    # 본문 안의 모든 [...] / <...> 제거 (괄호()는 본문 의미 유지)
    t = re.sub(r"[\[\<][^\]\>]*[\]\>]", "", t)
    # 한글/영문/숫자만 남기고 모두 제거 (공백·구두점·특수문자 무시)
    t = re.sub(r"[^\w가-힣]+", "", t, flags=re.UNICODE)
    return t.lower()


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


def seen_titles(category: str) -> set[str]:
    """카테고리에서 이미 본 정규화된 title 셋."""
    with _LOCK:
        return set(_load().get(category + _TITLE_KEY_SUFFIX, []))


def _merge_with_cap(existing: list[str], new_items: list[str], cap: int) -> list[str]:
    """순서 보존 + dedup + cap 적용 (오래된 것부터 drop)."""
    merged: list[str] = []
    seen_set: set[str] = set()
    for x in existing + new_items:
        if not x or x in seen_set:
            continue
        seen_set.add(x)
        merged.append(x)
    if len(merged) > cap:
        merged = merged[-cap:]
    return merged


def mark_seen(category: str, rpt_ids: list[str], cap: int = 1000) -> None:
    """rpt_id 리스트를 이미 본 것으로 마킹. 카테고리당 최대 cap개 (오래된 것부터 삭제).

    title 함께 마킹하려면 mark_seen_with_titles 사용.
    """
    with _LOCK:
        state = _load()
        merged = _merge_with_cap(state.get(category, []), list(rpt_ids), cap)
        state[category] = merged
        _save(state)
        log.info("state 갱신: %s에 %d개 추가 (총 %d, ids only)", category, len(rpt_ids), len(merged))


def mark_seen_with_titles(
    category: str,
    items: list[tuple[str, str]],
    cap: int = 1000,
) -> None:
    """(rpt_id, title) 페어 마킹. rpt_id + 정규화 title 둘 다 저장 → 다음 실행에서
    같은 채널·다른 prefix 리포트가 와도 title로 잡힘.
    """
    rpt_ids = [r for r, _ in items if r]
    titles_norm = [normalize_title(t) for _, t in items if t and normalize_title(t)]
    title_key = category + _TITLE_KEY_SUFFIX
    with _LOCK:
        state = _load()
        merged_ids = _merge_with_cap(state.get(category, []), rpt_ids, cap)
        merged_titles = _merge_with_cap(state.get(title_key, []), titles_norm, cap)
        state[category] = merged_ids
        state[title_key] = merged_titles
        _save(state)
        log.info(
            "state 갱신: %s에 %d개 추가 (총 ids=%d, titles=%d)",
            category, len(items), len(merged_ids), len(merged_titles),
        )


def reset(category: str | None = None) -> None:
    with _LOCK:
        if category is None:
            _save({})
            return
        state = _load()
        state.pop(category, None)
        state.pop(category + _TITLE_KEY_SUFFIX, None)
        _save(state)
