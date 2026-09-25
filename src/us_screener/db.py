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
            CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);
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
        # 스키마 버전 시드 — 향후 migration 토대 (현재는 활용 없음, 기록만).
        c.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1')"
        )
    _INITIALIZED = True
    log.info("[screener.db] 스키마 준비 완료 path=%s", DB_PATH)


def get_connection():
    """RLock + WAL 적용 DB 커넥션 context manager. `_conn()` public alias."""
    return _conn()


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


def load_signals_in_range(
    start_date: str, end_date: str,
    exclude_date: Optional[str] = None,
) -> list[dict]:
    """signals 테이블에서 date BETWEEN start AND end 행 전부.

    `idx_signals_date` 인덱스 사용. exclude_date 지정 시 그 날짜 제외 (오늘 제외 패턴).
    반환: [{date, ticker, signal, payload(dict)}]. JSON 디코딩 실패 시 {"raw": text}.
    """
    ensure_schema()
    out: list[dict] = []
    with _conn() as c:
        if exclude_date:
            cur = c.execute(
                "SELECT date, ticker, signal, payload FROM signals "
                "WHERE date >= ? AND date <= ? AND date != ? "
                "ORDER BY date ASC, signal ASC, ticker ASC",
                (start_date, end_date, exclude_date),
            )
        else:
            cur = c.execute(
                "SELECT date, ticker, signal, payload FROM signals "
                "WHERE date >= ? AND date <= ? "
                "ORDER BY date ASC, signal ASC, ticker ASC",
                (start_date, end_date),
            )
        for date_v, ticker, signal, payload in cur.fetchall():
            try:
                payload_obj = json.loads(payload) if payload else {}
            except (json.JSONDecodeError, TypeError):
                payload_obj = {"raw": payload}
            out.append({"date": date_v, "ticker": ticker, "signal": signal, "payload": payload_obj})
    return out


def recent_signals(days_back: int = 14, exclude_date: Optional[str] = None) -> list[dict]:
    """**Deprecated** — `load_signals_in_range(start, end, exclude_date)` 사용 권장.

    호환 위해 보존. days_back×1.5 calendar days 근사 오버페치를 제거하고 표준에 위임.
    """
    from datetime import date as _date, timedelta as _td
    start = (_date.today() - _td(days=int(days_back * 1.5))).isoformat()
    end = _date.today().isoformat()
    return load_signals_in_range(start, end, exclude_date=exclude_date)


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


def closes_from_date(ticker: str, start_date: str, n: int = 5) -> list[int]:
    """start_date부터 n+1 영업일치 close(cents) 리스트(거래일 오름차순). 부족하면 빈 리스트.
    회고 수익률의 스케일 브레이크 검사용."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT close FROM ohlcv WHERE ticker=? AND date >= ? "
            "ORDER BY date ASC LIMIT ?",
            (ticker, start_date, n + 1),
        )
        rows = cur.fetchall()
    if len(rows) < n + 1:
        return []
    return [int(r[0]) for r in rows]


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
                    "market_cap=COALESCE(excluded.market_cap, tickers.market_cap)",
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


def deactivate_missing(active: set[str]) -> int:
    """active 집합에 없는 종목을 is_active=0 (상장폐지·합병·시총 기준 이탈). 반환: 비활성화 수."""
    if not active:
        return 0
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT ticker FROM tickers WHERE is_active=1")
        stale = [r[0] for r in cur.fetchall() if r[0] not in active]
        if stale:
            c.execute("BEGIN")
            c.executemany("UPDATE tickers SET is_active=0 WHERE ticker=?", [(t,) for t in stale])
            c.execute("COMMIT")
    return len(stale)


def ticker_row_stats(tickers: Iterable[str] | None = None) -> dict[str, tuple[int, Optional[str]]]:
    """{ticker: (row_count, latest_date)} — 활성 종목 기준 (tickers 주면 그 집합만)."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT ticker, COUNT(*), MAX(date) FROM ohlcv GROUP BY ticker")
        out = {r[0]: (int(r[1]), r[2]) for r in cur.fetchall()}
    if tickers is not None:
        ts = set(tickers)
        out = {t: out.get(t, (0, None)) for t in ts}
    return out


def dates_for_ticker(ticker: str, since: str) -> set[str]:
    """ticker의 since 이후 보유 날짜 집합 — gap 보충 중복 호출 방지용."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT date FROM ohlcv WHERE ticker=? AND date>=?", (ticker, since))
        return {r[0] for r in cur.fetchall()}


def closes_for_ticker(ticker: str, since: str) -> dict[str, int]:
    """{date: close} (since 이후) — 분할 감지용."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute("SELECT date, close FROM ohlcv WHERE ticker=? AND date>=?", (ticker, since))
        return {r[0]: int(r[1]) for r in cur.fetchall()}


def date_row_count(date_str: str) -> int:
    ensure_schema()
    with _conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM ohlcv WHERE date=?", (date_str,)).fetchone()[0])


def latest_date_with_coverage(min_frac: float = 0.5, max_date: Optional[str] = None) -> Optional[str]:
    """활성 종목의 min_frac 이상이 보유한 가장 최근 날짜 (≤ max_date).

    base_date를 전역 MAX(date)로 잡으면 소수 종목만 가진 날짜(장중 봉·백필 잔여)로 앞당겨져
    나머지 전 종목이 'base_date 누락'이 된다 — 2026-09-25 리뷰 지적.
    """
    ensure_schema()
    with _conn() as c:
        active = int(c.execute("SELECT COUNT(*) FROM tickers WHERE is_active=1").fetchone()[0]) or 1
        q = ("SELECT o.date, COUNT(*) FROM ohlcv o JOIN tickers t ON t.ticker=o.ticker "
             "WHERE t.is_active=1 AND o.date >= ? ")
        args: list = []
        # 최근 30일만 스캔 (전체 GROUP BY는 수백만 행)
        since = c.execute("SELECT MAX(date) FROM ohlcv").fetchone()[0]
        if not since:
            return None
        from datetime import date as _d, timedelta as _td
        args.append((_d.fromisoformat(since) - _td(days=30)).isoformat())
        if max_date:
            q += "AND o.date <= ? "
            args.append(max_date)
        q += "GROUP BY o.date ORDER BY o.date DESC"
        for d, n in c.execute(q, args).fetchall():
            if n >= active * min_frac:
                return d
    return None


def replace_ticker_history(ticker: str, rows: list[tuple]) -> int:
    """종목 이력 전체 교체 (분할·비율변경 재구축). 삭제+삽입을 한 트랜잭션으로 — 실패 시 원복."""
    if not rows:
        return 0
    ensure_schema()
    with _conn() as c:
        c.execute("BEGIN")
        try:
            c.execute("DELETE FROM ohlcv WHERE ticker=?", (ticker,))
            c.executemany(
                "INSERT OR REPLACE INTO ohlcv (ticker, date, open, high, low, close, volume, value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
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


def get_ticker_row(ticker: str) -> Optional[dict]:
    """활성 여부 무관 단일 종목 조회 (/diag 진단용)."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT ticker, name, market, is_active, market_cap, sector "
            "FROM tickers WHERE ticker=?",
            (ticker,),
        )
        r = cur.fetchone()
    if not r:
        return None
    return {"ticker": r[0], "name": r[1], "market": r[2],
            "is_active": r[3], "market_cap": r[4], "sector": r[5]}


def search_tickers_by_name(substr: str, limit: int = 5) -> list[tuple]:
    """종목명/티커 부분일치 검색 → [(ticker, name), ...] (/diag 입력 지원)."""
    ensure_schema()
    with _conn() as c:
        cur = c.execute(
            "SELECT ticker, name FROM tickers WHERE name LIKE ? OR ticker LIKE ? "
            "ORDER BY ticker LIMIT ?",
            (f"%{substr}%", f"%{substr.upper()}%", limit),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


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
