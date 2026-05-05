"""SQLite 영속성 — 일봉 OHLCV + 종목 메타 + 신호 히스토리.

저장 위치는 src.state_store._state_dir() 패턴을 차용해 Railway 볼륨에 둠.
WAL 모드로 동시 read/write 안전.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

from src.state_store import _state_dir

log = logging.getLogger(__name__)

DB_PATH = _state_dir() / "screener.db"

_LOCK = threading.RLock()
_INITIALIZED = False


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    with _LOCK:
        c = sqlite3.connect(str(DB_PATH), timeout=30.0, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=OFF")
        try:
            yield c
        finally:
            c.close()


def ensure_schema() -> None:
    """첫 호출 시 스키마 생성. 이후 호출은 noop."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickers (
              ticker     TEXT PRIMARY KEY,
              name       TEXT NOT NULL,
              market     TEXT NOT NULL,
              is_active  INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ohlcv (
              ticker  TEXT NOT NULL,
              date    TEXT NOT NULL,
              open    INTEGER NOT NULL,
              high    INTEGER NOT NULL,
              low     INTEGER NOT NULL,
              close   INTEGER NOT NULL,
              volume  INTEGER NOT NULL,
              value   INTEGER,
              PRIMARY KEY (ticker, date)
            );
            CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv(date);
            CREATE TABLE IF NOT EXISTS meta (
              key   TEXT PRIMARY KEY,
              value TEXT
            );
            CREATE TABLE IF NOT EXISTS signals (
              date      TEXT NOT NULL,
              ticker    TEXT NOT NULL,
              signal    TEXT NOT NULL,
              payload   TEXT NOT NULL,
              PRIMARY KEY (date, ticker, signal)
            );
            """
        )
    _INITIALIZED = True
    log.info("[screener.db] 스키마 준비 완료 path=%s", DB_PATH)


# ------------------------------------------------------------------
# OHLCV
# ------------------------------------------------------------------
def upsert_ohlcv_bulk(rows: Iterable[tuple]) -> int:
    """rows: (ticker, date, open, high, low, close, volume, value).
    반환: 처리된 row 수.
    """
    ensure_schema()
    rows = list(rows)
    if not rows:
        return 0
    with _conn() as c:
        c.execute("BEGIN")
        c.executemany(
            "INSERT OR REPLACE INTO ohlcv (ticker, date, open, high, low, close, volume, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.execute("COMMIT")
    return len(rows)


def load_ohlcv(ticker: str, days: int = 260) -> list[dict]:
    """단일 종목의 최근 days일 OHLCV (date asc)."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT date, open, high, low, close, volume, value FROM ohlcv "
            "WHERE ticker=? ORDER BY date DESC LIMIT ?",
            (ticker, days),
        )
        rows = cur.fetchall()
    rows.reverse()  # asc
    return [
        {
            "date": r[0], "open": r[1], "high": r[2], "low": r[3],
            "close": r[4], "volume": r[5], "value": r[6],
        }
        for r in rows
    ]


def row_count() -> int:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT COUNT(*) FROM ohlcv")
        return int(cur.fetchone()[0])


def latest_date() -> Optional[str]:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT MAX(date) FROM ohlcv")
        v = cur.fetchone()[0]
    return v


def distinct_tickers_in_db() -> set[str]:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT DISTINCT ticker FROM ohlcv")
        return {r[0] for r in cur.fetchall()}


def has_date(date_str: str) -> bool:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT 1 FROM ohlcv WHERE date=? LIMIT 1", (date_str,))
        return cur.fetchone() is not None


def delete_older_than(cutoff_date: str) -> int:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("DELETE FROM ohlcv WHERE date < ?", (cutoff_date,))
        return cur.rowcount or 0


# ------------------------------------------------------------------
# Tickers
# ------------------------------------------------------------------
def upsert_tickers(rows: Iterable[tuple]) -> int:
    """rows: (ticker, name, market, is_active, updated_at)."""
    ensure_schema()
    rows = list(rows)
    if not rows:
        return 0
    with _conn() as c:
        c.execute("BEGIN")
        c.executemany(
            "INSERT OR REPLACE INTO tickers (ticker, name, market, is_active, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        c.execute("COMMIT")
    return len(rows)


def get_active_tickers() -> list[dict]:
    ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT ticker, name, market FROM tickers WHERE is_active=1 ORDER BY ticker"
        )
        return [{"ticker": r[0], "name": r[1], "market": r[2]} for r in cur.fetchall()]


def get_ticker_name(ticker: str) -> Optional[str]:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT name FROM tickers WHERE ticker=?", (ticker,))
        row = cur.fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------
# Meta
# ------------------------------------------------------------------
def meta_get(key: str) -> Optional[str]:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cur.fetchone()
    return row[0] if row else None


def meta_set(key: str, value: str) -> None:
    ensure_schema()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )


# ------------------------------------------------------------------
# Signals (히스토리)
# ------------------------------------------------------------------
def save_signals(date_str: str, results: dict[str, list[dict]]) -> int:
    """results: {category_key: [ {ticker, ...}, ... ]}.
    각 종목·신호 쌍을 row로 저장.
    """
    ensure_schema()
    rows: list[tuple] = []
    for cat, items in results.items():
        for item in items:
            t = item.get("ticker")
            if not t:
                continue
            rows.append((date_str, t, cat, json.dumps(item, ensure_ascii=False)))
    if not rows:
        return 0
    with _conn() as c:
        c.execute("BEGIN")
        c.executemany(
            "INSERT OR REPLACE INTO signals (date, ticker, signal, payload) VALUES (?, ?, ?, ?)",
            rows,
        )
        c.execute("COMMIT")
    return len(rows)


# ------------------------------------------------------------------
# 진단
# ------------------------------------------------------------------
def status() -> str:
    """텔레그램 친화 상태 문자열."""
    try:
        ensure_schema()
        with _conn() as c:
            tickers = c.execute("SELECT COUNT(*) FROM tickers WHERE is_active=1").fetchone()[0]
            rows = c.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
            mind = c.execute("SELECT MIN(date), MAX(date) FROM ohlcv").fetchone()
            sigs = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        size_mb = DB_PATH.stat().st_size / 1024 / 1024 if DB_PATH.exists() else 0
        return (
            f"📊 ScreenerDB\n"
            f"path: {DB_PATH}\n"
            f"size: {size_mb:.1f} MB\n"
            f"active tickers: {tickers}\n"
            f"ohlcv rows: {rows}\n"
            f"date range: {mind[0]} ~ {mind[1]}\n"
            f"signal rows: {sigs}\n"
        )
    except Exception as e:
        return f"⚠️ status 실패: {e}"
