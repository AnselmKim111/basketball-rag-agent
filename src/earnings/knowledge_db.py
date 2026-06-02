"""Phase 7F — Earnings Knowledge Base (SQLite).

테이블:
  calls(id, ticker, year, quarter, call_date, source, grounded, transcript_chars,
        extract_json, synthesis_text, counter_text, retrospective_text, quality_json,
        created_at)
  themes(id, call_id, theme_text)
  entities(id, call_id, mention_text, mention_type)

history.save_call 시 자동 백업. /query 시 풀텍스트 + 정량 컬럼 쿼리.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


def _db_path() -> Path:
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v)
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p / "earnings_knowledge.db"
            except Exception:
                continue
    for candidate in ("/data", "/app/data", "/tmp"):
        try:
            p = Path(candidate)
            p.mkdir(parents=True, exist_ok=True)
            return p / "earnings_knowledge.db"
        except Exception:
            continue
    return Path("/tmp/earnings_knowledge.db")


_LOCK = threading.RLock()
_INITIALIZED = False


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    with _LOCK:
        c = sqlite3.connect(str(_db_path()), timeout=30.0, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()


def ensure_schema() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
              id                  INTEGER PRIMARY KEY AUTOINCREMENT,
              ticker              TEXT NOT NULL,
              year                INTEGER NOT NULL,
              quarter             INTEGER NOT NULL,
              call_date           TEXT,
              source              TEXT,
              grounded            INTEGER,
              transcript_chars    INTEGER,
              extract_json        TEXT,
              synthesis_text      TEXT,
              counter_text        TEXT,
              retrospective_text  TEXT,
              quality_json        TEXT,
              created_at          TEXT NOT NULL,
              UNIQUE(ticker, year, quarter)
            );
            CREATE INDEX IF NOT EXISTS idx_calls_ticker ON calls(ticker);
            CREATE INDEX IF NOT EXISTS idx_calls_year_q ON calls(year, quarter);

            CREATE TABLE IF NOT EXISTS themes (
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              call_id   INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
              theme     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_themes_call ON themes(call_id);
            CREATE INDEX IF NOT EXISTS idx_themes_text ON themes(theme);

            CREATE TABLE IF NOT EXISTS entities (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              call_id       INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
              mention_text  TEXT NOT NULL,
              mention_type  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_entities_call ON entities(call_id);
            CREATE INDEX IF NOT EXISTS idx_entities_mention ON entities(mention_text);
            """
        )
    _INITIALIZED = True
    log.info("[earnings/knowledge_db] schema ready at %s", p)


# ------------------------------------------------------------------
# INSERT / UPSERT
# ------------------------------------------------------------------
def upsert_call(
    ticker: str, year: int, quarter: int,
    call_date: str = "", source: str = "", grounded: bool = False,
    transcript_chars: int = 0, extract: dict | None = None,
    synthesis_text: str = "", counter_text: str = "",
    retrospective_text: str = "", quality: dict | None = None,
) -> int | None:
    """1콜 upsert. ticker+year+quarter 유니크. 자동으로 themes/entities 추출."""
    if not ticker or not year or not quarter:
        return None
    ensure_schema()
    now = datetime.now(KST).isoformat()
    try:
        with _conn() as c:
            cur = c.execute(
                """INSERT INTO calls
                   (ticker, year, quarter, call_date, source, grounded, transcript_chars,
                    extract_json, synthesis_text, counter_text, retrospective_text, quality_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, year, quarter) DO UPDATE SET
                     call_date = excluded.call_date,
                     source = excluded.source,
                     grounded = excluded.grounded,
                     transcript_chars = excluded.transcript_chars,
                     extract_json = excluded.extract_json,
                     synthesis_text = excluded.synthesis_text,
                     counter_text = excluded.counter_text,
                     retrospective_text = excluded.retrospective_text,
                     quality_json = excluded.quality_json
                   RETURNING id""",
                (
                    ticker.upper(), int(year), int(quarter), call_date or "",
                    source or "", 1 if grounded else 0, int(transcript_chars or 0),
                    json.dumps(extract or {}, ensure_ascii=False, default=str)[:200000],
                    (synthesis_text or "")[:50000],
                    (counter_text or "")[:50000],
                    (retrospective_text or "")[:10000],
                    json.dumps(quality or {}, ensure_ascii=False, default=str)[:5000],
                    now,
                ),
            )
            row = cur.fetchone()
            call_id = row[0] if row else None
            if not call_id:
                return None
            # themes/entities 자동 추출 (있으면)
            c.execute("DELETE FROM themes WHERE call_id = ?", (call_id,))
            c.execute("DELETE FROM entities WHERE call_id = ?", (call_id,))
            if extract:
                # named_deals → entities
                for d in (extract.get("named_deals") or []):
                    if isinstance(d, dict) and d.get("counterparty"):
                        c.execute(
                            "INSERT INTO entities (call_id, mention_text, mention_type) VALUES (?, ?, ?)",
                            (call_id, d["counterparty"][:200], "deal_counterparty"),
                        )
                for cc in (extract.get("competitor_callouts") or []):
                    if isinstance(cc, dict) and cc.get("competitor"):
                        c.execute(
                            "INSERT INTO entities (call_id, mention_text, mention_type) VALUES (?, ?, ?)",
                            (call_id, cc["competitor"][:200], "competitor"),
                        )
                for sc in (extract.get("supply_chain_signals") or []):
                    if isinstance(sc, dict) and sc.get("vendor_or_component"):
                        c.execute(
                            "INSERT INTO entities (call_id, mention_text, mention_type) VALUES (?, ?, ?)",
                            (call_id, sc["vendor_or_component"][:200], "supplier"),
                        )
                # themes — segment_geo_mix.segment_or_region + 단순 keyword
                for sg in (extract.get("segment_geo_mix") or []):
                    if isinstance(sg, dict) and sg.get("segment_or_region"):
                        c.execute(
                            "INSERT INTO themes (call_id, theme) VALUES (?, ?)",
                            (call_id, sg["segment_or_region"][:200]),
                        )
            return call_id
    except Exception:
        log.exception("[earnings/knowledge_db] upsert_call 실패")
        return None


# ------------------------------------------------------------------
# QUERY
# ------------------------------------------------------------------
def search_entity_mentions(
    mention_substring: str, limit: int = 30,
    year_from: int | None = None, year_to: int | None = None,
) -> list[dict]:
    """엔티티 멘션 (counterparty/competitor/supplier) 부분 매칭."""
    ensure_schema()
    sub = f"%{mention_substring}%"
    sql = (
        "SELECT e.mention_text, e.mention_type, c.ticker, c.year, c.quarter, c.call_date "
        "FROM entities e JOIN calls c ON c.id = e.call_id "
        "WHERE e.mention_text LIKE ? "
    )
    params: list = [sub]
    if year_from is not None:
        sql += " AND c.year >= ?"
        params.append(int(year_from))
    if year_to is not None:
        sql += " AND c.year <= ?"
        params.append(int(year_to))
    sql += " ORDER BY c.year DESC, c.quarter DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[earnings/knowledge_db] search_entity_mentions 실패")
        return []


def list_calls_for_ticker(ticker: str, limit: int = 12) -> list[dict]:
    ensure_schema()
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT id, ticker, year, quarter, call_date, source, grounded, transcript_chars, created_at
                   FROM calls WHERE ticker = ? ORDER BY year DESC, quarter DESC LIMIT ?""",
                (ticker.upper(), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[earnings/knowledge_db] list_calls_for_ticker 실패")
        return []


def list_recent_calls(days: int = 7, limit: int = 50) -> list[dict]:
    """최근 N일 처리된 calls (digest용)."""
    ensure_schema()
    cutoff = (datetime.now(KST) - timedelta(days=int(days))).isoformat()
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT id, ticker, year, quarter, call_date, source, grounded, created_at,
                          substr(synthesis_text, 1, 500) AS syn_head,
                          substr(counter_text, 1, 300) AS cnt_head
                   FROM calls WHERE created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (cutoff, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[earnings/knowledge_db] list_recent_calls 실패")
        return []


def get_call(ticker: str, year: int, quarter: int) -> dict | None:
    ensure_schema()
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM calls WHERE ticker = ? AND year = ? AND quarter = ?",
                (ticker.upper(), int(year), int(quarter)),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        log.exception("[earnings/knowledge_db] get_call 실패")
        return None


def fulltext_search(needle: str, limit: int = 20) -> list[dict]:
    """synthesis_text + counter_text + retrospective_text 단순 LIKE 검색."""
    ensure_schema()
    sub = f"%{needle}%"
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT id, ticker, year, quarter, call_date,
                          substr(synthesis_text, 1, 800) AS syn_head
                   FROM calls
                   WHERE synthesis_text LIKE ? OR counter_text LIKE ? OR retrospective_text LIKE ?
                   ORDER BY year DESC, quarter DESC LIMIT ?""",
                (sub, sub, sub, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[earnings/knowledge_db] fulltext_search 실패")
        return []


def diag_log() -> None:
    try:
        ensure_schema()
        with _conn() as c:
            nc = c.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]
            ne = c.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
            nt = c.execute("SELECT COUNT(*) AS n FROM themes").fetchone()["n"]
        log.info("[earnings/knowledge_db] path=%s calls=%d entities=%d themes=%d",
                 _db_path(), nc, ne, nt)
    except Exception:
        log.exception("[earnings/knowledge_db] diag 실패")
