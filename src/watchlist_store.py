"""사용자별 종목 watchlist 영구 저장소.

state_store 패턴을 그대로 차용:
  - 같은 볼륨 (`/data/...`)에 watchlists.json 저장
  - threading.RLock으로 동일 프로세스 내 read-modify-write 직렬화
  - 원자적 rename으로 파일 손상 방지

데이터 구조:
  {
    "chat_id_str": [
      {
        "ticker": "005930",
        "name": "삼성전자",
        "added_at": "2026-05-05T01:23:45+09:00",
        "muted_until": null  // ISO datetime or null
      },
      ...
    ]
  }

각 ticker는 chat 단위로 추가/삭제. 폴링은 DisclosureWatcher가 chat을 가로질러
union ticker set 단위로 호출 (DART API quota 절약).
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


def _store_dir() -> Path:
    """state_store와 동일한 우선순위로 영구 보존 경로 결정."""
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


STORE_FILE = _store_dir() / "watchlists.json"


def _load() -> dict[str, list[dict]]:
    if not STORE_FILE.exists():
        return {}
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("watchlist 로드 실패: %s", STORE_FILE)
        return {}


def _save(state: dict[str, list[dict]]) -> None:
    try:
        tmp = STORE_FILE.with_suffix(STORE_FILE.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(STORE_FILE)
    except Exception:
        log.exception("watchlist 저장 실패: %s", STORE_FILE)


def diag_log() -> None:
    try:
        existed = STORE_FILE.exists()
        size = STORE_FILE.stat().st_size if existed else 0
        st = _load()
        per_chat = {k: len(v) for k, v in st.items()}
        log.info(
            "watchlist_store 진단: path=%s, exists=%s, size=%d, chats=%s",
            STORE_FILE, existed, size, per_chat or "(빈 watchlist)",
        )
    except Exception:
        log.exception("watchlist_store 진단 실패")


def list_for_chat(chat_id: str | int) -> list[dict]:
    """특정 chat의 watchlist (mute 상태 포함)."""
    with _LOCK:
        return list(_load().get(str(chat_id), []))


def all_tickers() -> set[str]:
    """모든 chat에 걸친 union ticker 셋 — DART 폴링 대상."""
    with _LOCK:
        st = _load()
        out: set[str] = set()
        for items in st.values():
            for it in items:
                t = it.get("ticker")
                if t:
                    out.add(t)
        return out


def subscribers_of(ticker: str) -> list[str]:
    """특정 ticker를 watch 중인 chat_id 리스트 (mute 만료 안 된 것 제외).

    mute_until이 None이거나 과거면 alert 받음. 미래 datetime이면 조용히 패스.
    """
    now = datetime.now(KST)
    with _LOCK:
        st = _load()
        out: list[str] = []
        for chat_id, items in st.items():
            for it in items:
                if it.get("ticker") != ticker:
                    continue
                mu = it.get("muted_until")
                if mu:
                    try:
                        when = datetime.fromisoformat(mu)
                        if when > now:
                            continue  # 음소거 중
                    except Exception:
                        pass
                out.append(chat_id)
                break
        return out


def add(chat_id: str | int, ticker: str, name: str = "") -> tuple[bool, str]:
    """ticker 추가. (성공여부, 메시지) 반환.

    이미 등록된 ticker면 (False, "이미 추적 중") 반환. 정상 추가 시 (True, "추가됨").
    """
    chat_id = str(chat_id)
    ticker = ticker.strip().zfill(6)
    with _LOCK:
        st = _load()
        items = st.get(chat_id, [])
        for it in items:
            if it.get("ticker") == ticker:
                return False, f"이미 추적 중: {it.get('name') or ticker}"
        items.append({
            "ticker": ticker,
            "name": name or ticker,
            "added_at": datetime.now(KST).isoformat(timespec="seconds"),
            "muted_until": None,
        })
        st[chat_id] = items
        _save(st)
        log.info("watchlist add: chat=%s ticker=%s name=%s (총 %d)",
                 chat_id, ticker, name, len(items))
        return True, f"추가됨: {name or ticker} ({ticker})"


def remove(chat_id: str | int, ticker: str) -> tuple[bool, str]:
    """ticker 제거."""
    chat_id = str(chat_id)
    ticker = ticker.strip().zfill(6)
    with _LOCK:
        st = _load()
        items = st.get(chat_id, [])
        before = len(items)
        items = [it for it in items if it.get("ticker") != ticker]
        if len(items) == before:
            return False, f"watchlist에 없음: {ticker}"
        st[chat_id] = items
        _save(st)
        log.info("watchlist remove: chat=%s ticker=%s (남은 %d)",
                 chat_id, ticker, len(items))
        return True, f"제거됨: {ticker}"


def mute(chat_id: str | int, ticker: str, days: int) -> tuple[bool, str]:
    """ticker 일시 음소거 (days일 동안). days=0이면 음소거 해제."""
    chat_id = str(chat_id)
    ticker = ticker.strip().zfill(6)
    with _LOCK:
        st = _load()
        items = st.get(chat_id, [])
        for it in items:
            if it.get("ticker") == ticker:
                if days <= 0:
                    it["muted_until"] = None
                    msg = f"음소거 해제: {it.get('name') or ticker}"
                else:
                    until = datetime.now(KST) + timedelta(days=days)
                    it["muted_until"] = until.isoformat(timespec="seconds")
                    msg = f"{days}일 동안 음소거: {it.get('name') or ticker} (~{until.strftime('%Y-%m-%d')})"
                _save(st)
                log.info("watchlist mute: chat=%s ticker=%s days=%d", chat_id, ticker, days)
                return True, msg
        return False, f"watchlist에 없음: {ticker}"


def reset_chat(chat_id: str | int) -> None:
    """특정 chat의 watchlist 전체 삭제 (테스트/관리 용도)."""
    chat_id = str(chat_id)
    with _LOCK:
        st = _load()
        st.pop(chat_id, None)
        _save(st)
