"""승인 대기 추천 store — /data/model_router/pending.json.

각 pending: {id, env_name, old_model, new_model, classification, reason,
             score_old, score_new, savings_pct, created_at, ttl_at, status}
status: pending | approved | rejected | expired
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


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


def _pending_path() -> Path:
    return _store_dir() / "pending.json"


def _history_path() -> Path:
    return _store_dir() / "history.json"


def load_pending() -> list[dict]:
    p = _pending_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        log.exception("[approval_store] pending.json 로드 실패")
        return []


def save_pending(items: list[dict]) -> None:
    p = _pending_path()
    try:
        p.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    except Exception:
        log.exception("[approval_store] pending.json 저장 실패")


def append_history(item: dict) -> None:
    p = _history_path()
    try:
        history = json.loads(p.read_text()) if p.exists() else []
    except Exception:
        history = []
    history.append(item)
    try:
        p.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=2))  # 최근 200건
    except Exception:
        log.exception("[approval_store] history.json 저장 실패")


def stage_recommendations(recs: list[dict], ttl_hours: int = 12) -> list[dict]:
    """추천 list → pending.json에 저장 + id 부여.

    automatic은 caller가 즉시 적용 후 history로 보냄 — pending에 안 넣음.
    suggest만 pending에 저장.
    """
    now = int(time.time())
    pending = []
    next_id = 1
    for r in recs:
        if r["classification"] != "suggest":
            continue
        pending.append({
            "id": next_id,
            "env_name": r["env_name"],
            "old_model": r["old_model"],
            "new_model": r["new_model"],
            "classification": r["classification"],
            "reason": r["reason"],
            "score_old": r["score_old"],
            "score_new": r["score_new"],
            "savings_pct": r.get("savings_pct"),
            "created_at": now,
            "ttl_at": now + ttl_hours * 3600,
            "status": "pending",
        })
        next_id += 1
    save_pending(pending)
    return pending


def get_by_id(pending_id: int) -> dict | None:
    for item in load_pending():
        if item["id"] == pending_id and item["status"] == "pending":
            return item
    return None


def mark_status(pending_id: int, status: str, note: str = "") -> dict | None:
    items = load_pending()
    target = None
    for item in items:
        if item["id"] == pending_id:
            item["status"] = status
            item["resolved_at"] = int(time.time())
            if note:
                item["note"] = note
            target = item
            break
    save_pending(items)
    if target:
        append_history(target)
    return target


def expire_old() -> list[dict]:
    """TTL 만료된 pending → expired 처리 + history. 만료된 항목 list 반환."""
    items = load_pending()
    now = int(time.time())
    expired = []
    for item in items:
        if item["status"] == "pending" and item.get("ttl_at", 0) < now:
            item["status"] = "expired"
            item["resolved_at"] = now
            expired.append(item)
            append_history(item)
    save_pending(items)
    return expired
