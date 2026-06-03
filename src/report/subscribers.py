"""MarketReportBot subscribers — chat_id별 가입/탈퇴 영속화.

스크리너봇 패턴(`src/screener/subscribers.py`) 이식. /data/report_bot.db에
자체 테이블을 두어 스크리너 DB와 독립. cron broadcast는 admin env ∪ DB 합집합.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

_DB_PATH = Path(os.getenv("REPORT_DB_PATH", "/data/report_bot.db"))


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH, timeout=30.0)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_schema() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
              chat_id        TEXT PRIMARY KEY,
              username       TEXT,
              full_name      TEXT,
              subscribed_at  TEXT NOT NULL,
              is_blocked     INTEGER NOT NULL DEFAULT 0
            )
            """
        )


def subscribe(chat_id: str | int, username: str | None = None, full_name: str | None = None) -> bool:
    """등록. 이미 있으면 정보만 업데이트. 차단된 chat_id면 거부.

    반환: True=신규, False=기존/차단.
    """
    _ensure_schema()
    cid = str(chat_id)
    now = datetime.now(KST).isoformat()
    with _conn() as c:
        cur = c.execute("SELECT is_blocked FROM subscribers WHERE chat_id=?", (cid,))
        row = cur.fetchone()
        if row:
            if int(row[0]) == 1:
                log.warning("[report.subs] %s 차단됨 — 등록 거부", cid)
                return False
            c.execute(
                "UPDATE subscribers SET username=?, full_name=? WHERE chat_id=?",
                (username, full_name, cid),
            )
            return False
        c.execute(
            "INSERT INTO subscribers (chat_id, username, full_name, subscribed_at, is_blocked) "
            "VALUES (?, ?, ?, ?, 0)",
            (cid, username, full_name, now),
        )
        log.info("[report.subs] 신규 chat_id=%s username=%s name=%s", cid, username, full_name)
        return True


def unsubscribe(chat_id: str | int) -> bool:
    _ensure_schema()
    cid = str(chat_id)
    with _conn() as c:
        cur = c.execute("DELETE FROM subscribers WHERE chat_id=?", (cid,))
        deleted = cur.rowcount or 0
    if deleted:
        log.info("[report.subs] 탈퇴 chat_id=%s", cid)
    return deleted > 0


def block(chat_id: str | int) -> bool:
    _ensure_schema()
    cid = str(chat_id)
    with _conn() as c:
        cur = c.execute("UPDATE subscribers SET is_blocked=1 WHERE chat_id=?", (cid,))
        if (cur.rowcount or 0) == 0:
            now = datetime.now(KST).isoformat()
            c.execute(
                "INSERT OR REPLACE INTO subscribers "
                "(chat_id, username, full_name, subscribed_at, is_blocked) "
                "VALUES (?, NULL, NULL, ?, 1)",
                (cid, now),
            )
    log.info("[report.subs] 차단 chat_id=%s", cid)
    return True


def unblock(chat_id: str | int) -> bool:
    _ensure_schema()
    cid = str(chat_id)
    with _conn() as c:
        cur = c.execute("UPDATE subscribers SET is_blocked=0 WHERE chat_id=?", (cid,))
    log.info("[report.subs] 차단 해제 chat_id=%s", cid)
    return (cur.rowcount or 0) > 0


def list_active_chat_ids() -> list[str]:
    """차단되지 않은 활성 구독자 — cron broadcast 대상."""
    _ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT chat_id FROM subscribers WHERE is_blocked=0 ORDER BY subscribed_at"
        )
        return [r[0] for r in cur.fetchall()]


def list_all() -> list[dict]:
    _ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT chat_id, username, full_name, subscribed_at, is_blocked "
            "FROM subscribers ORDER BY subscribed_at"
        )
        return [
            {
                "chat_id": r[0],
                "username": r[1],
                "full_name": r[2],
                "subscribed_at": r[3],
                "is_blocked": bool(r[4]),
            }
            for r in cur.fetchall()
        ]


def is_subscribed(chat_id: str | int) -> bool:
    _ensure_schema()
    cid = str(chat_id)
    with _conn() as c:
        cur = c.execute(
            "SELECT 1 FROM subscribers WHERE chat_id=? AND is_blocked=0", (cid,)
        )
        return cur.fetchone() is not None
