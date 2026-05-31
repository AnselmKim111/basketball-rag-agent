"""어닝콜 pre-emptive watchlist — chat별 ticker 구독.

storage layout:
  {STATE_ROOT}/earnings_watchlist.json
  payload: {
    "<chat_id>": {
      "tickers": ["MSFT", "NVDA", ...],
      "added_at": {"MSFT": "2026-05-31T00:00:00Z", ...}
    },
    ...
  }

사용:
- add / remove / list_for_chat / all_subscribers_of / all_tickers / diag_log
- watchlist_store(KR 공시)와 격리 — 별도 파일에 저장. 패턴은 거의 동일.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_LOCK = threading.RLock()
_FILE_NAME = "earnings_watchlist.json"


def _store_dir() -> Path:
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v)
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                continue
    for candidate in ("/data", "/app/data", "/tmp"):
        try:
            p = Path(candidate)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return Path("/tmp")


def _file_path() -> Path:
    return _store_dir() / _FILE_NAME


def _load() -> dict[str, dict]:
    p = _file_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.exception("[earnings_watchlist] load 실패: %s", p)
        return {}


def _save(state: dict[str, dict]) -> None:
    p = _file_path()
    with _LOCK:
        try:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            log.exception("[earnings_watchlist] save 실패: %s", p)


def diag_log() -> None:
    state = _load()
    n_chats = len(state)
    all_t: set[str] = set()
    for v in state.values():
        all_t.update(v.get("tickers") or [])
    log.info("[earnings_watchlist] chats=%d unique_tickers=%d (%s)",
             n_chats, len(all_t), ",".join(sorted(all_t)[:15]))


# ------------------------------------------------------------------
# 공개 API
# ------------------------------------------------------------------
def list_for_chat(chat_id: str | int) -> list[str]:
    state = _load()
    entry = state.get(str(chat_id)) or {}
    return sorted(entry.get("tickers") or [])


def all_tickers() -> set[str]:
    state = _load()
    out: set[str] = set()
    for v in state.values():
        out.update(v.get("tickers") or [])
    return out


def subscribers_of(ticker: str) -> list[str]:
    """주어진 ticker를 watch 중인 chat_id 리스트."""
    ticker = (ticker or "").upper().strip()
    state = _load()
    out: list[str] = []
    for cid, v in state.items():
        ts = {t.upper() for t in (v.get("tickers") or [])}
        if ticker in ts:
            out.append(cid)
    return out


def add(chat_id: str | int, ticker: str) -> tuple[bool, str]:
    """추가. 반환: (성공여부, 메시지)."""
    ticker = (ticker or "").upper().strip()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        return False, f"잘못된 ticker: {ticker!r}"
    state = _load()
    cid = str(chat_id)
    entry = state.setdefault(cid, {"tickers": [], "added_at": {}})
    if ticker in entry["tickers"]:
        return False, f"{ticker} 이미 watch 중"
    entry["tickers"].append(ticker)
    entry["added_at"][ticker] = datetime.now(KST).isoformat()
    _save(state)
    return True, f"{ticker} watch 추가됨"


def remove(chat_id: str | int, ticker: str) -> tuple[bool, str]:
    ticker = (ticker or "").upper().strip()
    state = _load()
    cid = str(chat_id)
    entry = state.get(cid)
    if not entry or ticker not in (entry.get("tickers") or []):
        return False, f"{ticker} watch 안 함"
    entry["tickers"] = [t for t in entry["tickers"] if t != ticker]
    entry.get("added_at", {}).pop(ticker, None)
    _save(state)
    return True, f"{ticker} watch 해제됨"
