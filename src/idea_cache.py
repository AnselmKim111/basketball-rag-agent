"""IdeaBot history cache — 매 아이디어 분석 결과를 JSON으로 영속화.

저장 위치 우선순위 (state_store와 동일 패턴):
  1) RAILWAY_VOLUME_MOUNT_PATH / idea_history
  2) STATE_DIR / idea_history
  3) /app/data / idea_history
  4) /tmp / idea_history (휘발)

저장 내용: idea_text, parsed(constraints), research(요약), top10, top5, 발송 시각, PDF 경로.
PDF 자체는 별도 폴더(downloads/idea/<safe_name>) 에 이미 저장되므로 경로만 기록.

후속 명령에서 재사용:
  - /history: 최근 N개 목록 표시
  - /show <id>: 과거 결과 재발송
  - /refine <id> <constraint>: cached research/industry_pdfs 재사용
  - /dive <rank>: 가장 최근 idea의 top5 중 N번째 deepdive
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_LOCK = threading.RLock()


def _cache_root() -> Path:
    """캐시 디렉터리. 없으면 fallback 순회."""
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v) / "idea_history"
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                continue
    for candidate in ("/data", "/app/data", "/tmp"):
        try:
            p = Path(candidate) / "idea_history"
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return Path("/tmp")


def _new_id() -> str:
    """짧은 ID — KST 기반 시각으로 sortable + 사람이 읽기 쉬움."""
    return datetime.now(KST).strftime("%Y%m%d-%H%M%S")


def save(
    idea_text: str,
    parsed: dict | None,
    research: dict | None,
    industry_pdfs: list[Path],
    industry_texts: dict[str, str],
    narrow: dict | None,
    top10: list[dict],
    company_pdfs_by_ticker: dict[str, list[Path]],
    company_texts_by_ticker: dict[str, dict[str, str]],
    synthesis: dict | None,
    download_root: Path | None = None,
) -> str:
    """idea 결과 전체를 JSON으로 캐시. 반환: 새 entry ID."""
    entry_id = _new_id()
    record = {
        "id": entry_id,
        "created_at": datetime.now(KST).isoformat(),
        "idea_text": idea_text,
        "parsed": parsed or {},
        "research": research or {},
        "industry_pdfs": [str(p) for p in industry_pdfs],
        "industry_texts": industry_texts,  # 텍스트도 저장 → /refine 시 재추출 안 함
        "narrow": narrow or {},
        "top10": top10,
        "company_pdfs_by_ticker": {k: [str(p) for p in v] for k, v in company_pdfs_by_ticker.items()},
        "company_texts_by_ticker": company_texts_by_ticker,
        "synthesis": synthesis or {},
        "download_root": str(download_root) if download_root else "",
    }
    path = _cache_root() / f"{entry_id}.json"
    try:
        with _LOCK:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        log.info("idea_cache 저장: %s (%d KB)", path.name, path.stat().st_size // 1024)
    except Exception:
        log.exception("idea_cache 저장 실패: %s", entry_id)
    return entry_id


def load(entry_id: str) -> dict | None:
    """ID로 캐시 entry 로드. 없으면 None."""
    path = _cache_root() / f"{entry_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("idea_cache 로드 실패: %s", entry_id)
        return None


def list_recent(limit: int = 20) -> list[dict]:
    """최근 N개 entry 메타데이터 (id, created_at, idea_text, top5 요약)."""
    root = _cache_root()
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out: list[dict] = []
    for p in files:
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
            top5 = record.get("synthesis", {}).get("top5") or []
            top5_brief = [
                f"{pick.get('name','?')}({pick.get('ticker6','?')})"
                for pick in top5[:5]
            ]
            out.append({
                "id": record.get("id", p.stem),
                "created_at": record.get("created_at", ""),
                "idea_text": (record.get("idea_text") or "")[:80],
                "top5_brief": top5_brief,
            })
        except Exception:
            log.exception("entry %s 메타 로드 실패", p.name)
    return out


def latest() -> dict | None:
    """가장 최근 entry 전체 로드 (없으면 None). /dive 등에서 사용."""
    root = _cache_root()
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        log.exception("latest entry 로드 실패")
        return None


def find_by_partial_id(partial: str) -> dict | None:
    """ID 끝부분만 일치해도 찾기 (사용자 편의 — 예: '143005' → '20260430-143005')."""
    if not partial:
        return None
    partial = partial.strip()
    root = _cache_root()
    # 정확 일치 우선
    exact = root / f"{partial}.json"
    if exact.exists():
        return load(partial)
    # 부분 일치
    for p in root.glob("*.json"):
        if partial in p.stem:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def cleanup_old(keep: int = 200) -> int:
    """오래된 entry 정리. 최신 keep개만 보존. 반환: 삭제 건수."""
    root = _cache_root()
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if len(files) <= keep:
        return 0
    to_delete = files[keep:]
    deleted = 0
    for p in to_delete:
        try:
            p.unlink()
            deleted += 1
        except Exception:
            pass
    return deleted
