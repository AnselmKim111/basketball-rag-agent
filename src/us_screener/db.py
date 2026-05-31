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

DB_PATH = _state_dir() / "us_screener.db"

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
    """첫 호출 시 스키마 생성. 이후 호출은 noop. ALTER는 idempotent."""
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
            CREATE TABLE IF NOT EXISTS fundamentals (
              ticker     TEXT PRIMARY KEY,
              eps_yoy    REAL,
              eps_asof   TEXT,
              updated_at TEXT NOT NULL
            );
            """
        )
        # market_cap, sector 컬럼 추가 (기존 DB 호환). SQLite ALTER는 IF NOT EXISTS 미지원.
        for col_def in (
            "ALTER TABLE tickers ADD COLUMN market_cap INTEGER",
            "ALTER TABLE tickers ADD COLUMN sector TEXT",
            "ALTER TABLE fundamentals ADD COLUMN shares REAL",
        ):
            try:
                c.execute(col_def)
            except sqlite3.OperationalError:
                pass  # 이미 존재
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


def ticker_data_lengths() -> dict:
    """종목별 OHLCV row 수 분포 진단. 52주 신고가 가능 여부(252일+) 점검용.

    반환: {"total_tickers", "ge_252", "ge_240", "ge_60", "median_len", "max_len"}.
    """
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT ticker, COUNT(*) AS n FROM ohlcv GROUP BY ticker")
        lengths = sorted(int(r[1]) for r in cur.fetchall())
    if not lengths:
        return {"total_tickers": 0, "ge_252": 0, "ge_240": 0, "ge_60": 0,
                "median_len": 0, "max_len": 0}
    n = len(lengths)
    return {
        "total_tickers": n,
        "ge_252": sum(1 for x in lengths if x >= 252),
        "ge_240": sum(1 for x in lengths if x >= 240),
        "ge_60": sum(1 for x in lengths if x >= 60),
        "median_len": lengths[n // 2],
        "max_len": lengths[-1],
    }


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


def recent_signals(days_back: int = 14, exclude_date: Optional[str] = None) -> list[dict]:
    """최근 days_back 영업일 내 신호 [{date,ticker,signal,payload}]. exclude_date 시 그날 제외(오늘)."""
    ensure_schema()
    with _conn() as c:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=int(days_back * 1.5))).isoformat()
        if exclude_date:
            cur = c.execute(
                "SELECT date, ticker, signal, payload FROM signals "
                "WHERE date >= ? AND date != ? ORDER BY date ASC, ticker, signal",
                (cutoff, exclude_date),
            )
        else:
            cur = c.execute(
                "SELECT date, ticker, signal, payload FROM signals "
                "WHERE date >= ? ORDER BY date ASC, ticker, signal",
                (cutoff,),
            )
        rows = cur.fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[3]) if r[3] else {}
        except Exception:
            payload = {}
        out.append({"date": r[0], "ticker": r[1], "signal": r[2], "payload": payload})
    return out


def close_after_n_business_days(ticker: str, start_date: str, n: int = 5) -> Optional[int]:
    """start_date로부터 n영업일 후 close(cents). 아직 안 됐으면 None."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT date, close FROM ohlcv WHERE ticker=? AND date >= ? "
            "ORDER BY date ASC LIMIT ?",
            (ticker, start_date, n + 1),
        )
        rows = cur.fetchall()
    if len(rows) < n + 1:
        return None
    return int(rows[n][1])


def delete_older_than(cutoff_date: str) -> int:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("DELETE FROM ohlcv WHERE date < ?", (cutoff_date,))
        return cur.rowcount or 0


# ------------------------------------------------------------------
# Tickers
# ------------------------------------------------------------------
def upsert_tickers(rows: Iterable[tuple]) -> int:
    """rows: (ticker, name, market, is_active, updated_at) 또는
    (ticker, name, market, is_active, updated_at, market_cap)."""
    ensure_schema()
    rows = list(rows)
    if not rows:
        return 0
    with _conn() as c:
        c.execute("BEGIN")
        # row 길이로 분기 (5: 시총 없음 / 6: 시총 포함)
        for r in rows:
            if len(r) == 6:
                c.execute(
                    "INSERT INTO tickers (ticker, name, market, is_active, updated_at, market_cap) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(ticker) DO UPDATE SET "
                    "name=excluded.name, market=excluded.market, "
                    "is_active=excluded.is_active, updated_at=excluded.updated_at, "
                    "market_cap=excluded.market_cap",
                    r,
                )
            else:
                c.execute(
                    "INSERT OR REPLACE INTO tickers (ticker, name, market, is_active, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    r,
                )
        c.execute("COMMIT")
    return len(rows)


def update_market_caps(caps: dict[str, int]) -> int:
    """{ticker: market_cap} 일괄 업데이트. 신규 ticker는 무시 (universe 빌드 후 호출)."""
    if not caps:
        return 0
    ensure_schema()
    with _conn() as c:
        c.execute("BEGIN")
        c.executemany(
            "UPDATE tickers SET market_cap=? WHERE ticker=?",
            [(int(v), k) for k, v in caps.items()],
        )
        c.execute("COMMIT")
    return len(caps)


def update_sectors(secs: dict[str, str]) -> int:
    """{ticker: sector} 일괄 업데이트."""
    if not secs:
        return 0
    ensure_schema()
    with _conn() as c:
        c.execute("BEGIN")
        c.executemany(
            "UPDATE tickers SET sector=? WHERE ticker=?",
            [(str(v), k) for k, v in secs.items()],
        )
        c.execute("COMMIT")
    return len(secs)


def get_active_tickers() -> list[dict]:
    ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT ticker, name, market, market_cap, sector FROM tickers "
            "WHERE is_active=1 ORDER BY ticker"
        )
        return [
            {
                "ticker": r[0], "name": r[1], "market": r[2],
                "market_cap": r[3], "sector": r[4],
            }
            for r in cur.fetchall()
        ]


def get_ticker_name(ticker: str) -> Optional[str]:
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT name FROM tickers WHERE ticker=?", (ticker,))
        row = cur.fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------
# Fundamentals 캐시 (EPS YoY) — SEC EDGAR 호출 부하 최소화
# ------------------------------------------------------------------
def fundamentals_get(ticker: str, max_age_days: int = 7) -> Optional[dict]:
    """캐시된 {eps_yoy, shares, eps_asof}. 없거나 max_age_days 초과면 None."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT eps_yoy, eps_asof, updated_at, shares FROM fundamentals WHERE ticker=?", (ticker,)
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        from datetime import datetime, timezone
        upd = datetime.fromisoformat(row[2])
        if upd.tzinfo is None:
            upd = upd.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - upd).total_seconds()
        if age > max_age_days * 86400:
            return None
    except Exception:
        return None
    return {"eps_yoy": row[0], "eps_asof": row[1], "shares": row[3]}


def fundamentals_put(ticker: str, eps_yoy: Optional[float], eps_asof: Optional[str],
                     shares: Optional[float] = None) -> None:
    ensure_schema()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO fundamentals (ticker, eps_yoy, eps_asof, updated_at, shares) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, eps_yoy, eps_asof, now, shares),
        )


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
