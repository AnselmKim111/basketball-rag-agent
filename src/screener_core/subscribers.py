"""ScreenerBot subscribers — KR/US 공통 구현.

chat_id별 가입/탈퇴/차단 영속화. SQLite 자체 테이블 (각 시장 DB에 합류).

저장 구조 (subscribers 테이블):
  chat_id          TEXT PRIMARY KEY
  username         TEXT (텔레그램 @username)
  full_name        TEXT
  subscribed_at    TEXT (ISO datetime)
  is_blocked       INTEGER (0/1)

사용:
  - /start 핸들러 → subscribe(chat_id, username, full_name)
  - /stop 핸들러  → unsubscribe(chat_id)
  - /list (admin) → list_all()
  - /block (admin) → block(chat_id)
  - cron 발송 시 → list_active_chat_ids() 모두에게 메시지

ALLOWED_CHAT_IDS env (admin) + subscribers DB는 union으로 합쳐서 권한 체크.

`make_api(db)` closure factory로 시장별 db 모듈 주입.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


def make_api(db) -> SimpleNamespace:
    """db 모듈 주입 → subscribers public 함수들을 closure로 반환."""

    def _ensure_schema() -> None:
        """subscribers 테이블 idempotent 생성."""
        db.ensure_schema()
        with db._conn() as c:
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
        """등록. 이미 있으면 정보만 업데이트. 차단된 chat_id면 등록 거부.

        반환: True=신규 등록, False=이미 있음/차단됨.
        """
        _ensure_schema()
        cid = str(chat_id)
        now = datetime.now(KST).isoformat()
        with db._conn() as c:
            cur = c.execute("SELECT is_blocked FROM subscribers WHERE chat_id=?", (cid,))
            row = cur.fetchone()
            if row:
                if int(row[0]) == 1:
                    log.warning("[subscribers] %s 차단됨 — 등록 거부", cid)
                    return False
                # 정보 업데이트만 (재가입은 noop)
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
            log.info("[subscribers] 신규 등록 chat_id=%s username=%s name=%s", cid, username, full_name)
            return True

    def unsubscribe(chat_id: str | int) -> bool:
        """탈퇴. 반환: True=탈퇴 완료, False=등록되어 있지 않음."""
        _ensure_schema()
        cid = str(chat_id)
        with db._conn() as c:
            cur = c.execute("DELETE FROM subscribers WHERE chat_id=?", (cid,))
            deleted = cur.rowcount or 0
        if deleted:
            log.info("[subscribers] 탈퇴 chat_id=%s", cid)
        return deleted > 0

    def block(chat_id: str | int) -> bool:
        """차단 (DB에 row 유지하되 is_blocked=1). 반환: True=차단됨."""
        _ensure_schema()
        cid = str(chat_id)
        with db._conn() as c:
            cur = c.execute("UPDATE subscribers SET is_blocked=1 WHERE chat_id=?", (cid,))
            affected = cur.rowcount or 0
            if affected == 0:
                # 없으면 차단된 상태로 새로 추가
                now = datetime.now(KST).isoformat()
                c.execute(
                    "INSERT OR REPLACE INTO subscribers "
                    "(chat_id, username, full_name, subscribed_at, is_blocked) "
                    "VALUES (?, NULL, NULL, ?, 1)",
                    (cid, now),
                )
        log.info("[subscribers] 차단 chat_id=%s", cid)
        return True

    def unblock(chat_id: str | int) -> bool:
        _ensure_schema()
        cid = str(chat_id)
        with db._conn() as c:
            cur = c.execute("UPDATE subscribers SET is_blocked=0 WHERE chat_id=?", (cid,))
        log.info("[subscribers] 차단 해제 chat_id=%s", cid)
        return (cur.rowcount or 0) > 0

    def list_active_chat_ids() -> list[str]:
        """차단되지 않은 모든 가입자 chat_id 리스트 (cron 발송용)."""
        _ensure_schema()
        with db._conn() as c:
            cur = c.execute(
                "SELECT chat_id FROM subscribers WHERE is_blocked=0 ORDER BY subscribed_at"
            )
            return [r[0] for r in cur.fetchall()]

    def list_all() -> list[dict]:
        """admin 조회용. 차단 포함 모든 가입자 정보."""
        _ensure_schema()
        with db._conn() as c:
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
        """차단되지 않은 활성 가입자인가."""
        _ensure_schema()
        cid = str(chat_id)
        with db._conn() as c:
            cur = c.execute(
                "SELECT 1 FROM subscribers WHERE chat_id=? AND is_blocked=0", (cid,)
            )
            return cur.fetchone() is not None

    return SimpleNamespace(
        subscribe=subscribe,
        unsubscribe=unsubscribe,
        block=block,
        unblock=unblock,
        list_active_chat_ids=list_active_chat_ids,
        list_all=list_all,
        is_subscribed=is_subscribed,
    )
