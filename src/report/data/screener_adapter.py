"""screener.db / us_screener.db 어댑터 — 버터대디봇이 동일 /data 볼륨에서 read.

스크리너봇이 매일 16:00 KST에 갱신한 OHLCV·시그널·메타를 버터대디봇 08:00 KST 리포트
파이프라인이 그대로 활용. 동일 컨테이너 (`/data/screener.db`, `/data/us_screener.db`)이라
read는 무네트워크·즉시.

API:
  - load_signals(market, date) -> {category: [{ticker, name, market_cap, sector, payload...}]}
  - load_active_tickers(market) -> [{ticker, name, market_cap, sector}]
  - load_ohlcv_df(ticker, market, days) -> pandas.DataFrame | None (Open/High/Low/Close/Volume)
  - latest_signal_date(market) -> str | None

market: "KR" | "US".
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

_KR_DB: Optional[Path] = None
_US_DB: Optional[Path] = None


def _resolve_db_paths() -> tuple[Path | None, Path | None]:
    """screener.db / us_screener.db 경로 — state_store._state_dir() 동일 경로."""
    global _KR_DB, _US_DB
    if _KR_DB is None or _US_DB is None:
        try:
            from src.state_store import _state_dir
            d = _state_dir()
            _KR_DB = d / "screener.db"
            _US_DB = d / "us_screener.db"
        except Exception:
            log.warning("[screener_adapter] _state_dir 미해석")
            return None, None
    return _KR_DB, _US_DB


def _db_for(market: str) -> Path | None:
    kr, us = _resolve_db_paths()
    if market.upper() == "KR":
        return kr if kr and kr.exists() else None
    if market.upper() == "US":
        return us if us and us.exists() else None
    return None


@contextmanager
def _conn(path: Path) -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(str(path), timeout=10.0)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
    finally:
        c.close()


def latest_signal_date(market: str) -> Optional[str]:
    """signals 테이블의 최신 date — 없으면 ohlcv 최신 date 폴백."""
    p = _db_for(market)
    if not p:
        return None
    try:
        with _conn(p) as c:
            row = c.execute("SELECT MAX(date) FROM signals").fetchone()
            if row and row[0]:
                return row[0]
            row = c.execute("SELECT MAX(date) FROM ohlcv").fetchone()
            return row[0] if row else None
    except Exception:
        log.exception("[screener_adapter] latest_signal_date %s 실패", market)
        return None


def load_active_tickers(market: str) -> list[dict]:
    """is_active=1 종목 (ticker/name/market/market_cap/sector). 빈 list 가능."""
    p = _db_for(market)
    if not p:
        return []
    try:
        with _conn(p) as c:
            cur = c.execute(
                "SELECT ticker, name, market, market_cap, sector FROM tickers "
                "WHERE is_active=1 ORDER BY ticker"
            )
            return [
                {
                    "ticker": r[0],
                    "name": r[1],
                    "market": r[2],
                    "market_cap": r[3],
                    "sector": r[4],
                }
                for r in cur.fetchall()
            ]
    except Exception:
        log.exception("[screener_adapter] load_active_tickers %s 실패", market)
        return []


def load_signals(market: str, date: str | None = None) -> dict[str, list[dict]]:
    """카테고리별 신호. date 미지정 시 최신.

    반환 형식: {category: [{ticker, name, market_cap, sector, **payload}, ...]}
    payload는 signals 테이블의 JSON (chg_pct·close·pct·hi52 등 포함).
    """
    p = _db_for(market)
    if not p:
        return {}
    if not date:
        date = latest_signal_date(market)
        if not date:
            return {}
    try:
        with _conn(p) as c:
            cur = c.execute(
                "SELECT s.signal, s.ticker, s.payload, t.name, t.market_cap, t.sector "
                "FROM signals s LEFT JOIN tickers t ON t.ticker = s.ticker "
                "WHERE s.date=?",
                (date,),
            )
            out: dict[str, list[dict]] = {}
            for sig, tkr, payload_json, name, cap, sector in cur.fetchall():
                try:
                    payload = json.loads(payload_json) if payload_json else {}
                except Exception:
                    payload = {}
                entry = {
                    "ticker": tkr,
                    "name": name or tkr,
                    "market_cap": cap,
                    "sector": sector or "",
                    **payload,
                }
                out.setdefault(sig, []).append(entry)
            return out
    except Exception:
        log.exception("[screener_adapter] load_signals %s date=%s 실패", market, date)
        return {}


def load_ohlcv_df(ticker: str, market: str, days: int = 260):
    """단일 종목 OHLCV → pandas.DataFrame (Open/High/Low/Close/Volume, index=date).

    screener.db의 가격 컬럼은 정수(원/센트 단위 보존 안 함, 일반 정수). float로 캐스팅.
    pandas 미import 환경 보호: import 실패 시 None.
    """
    p = _db_for(market)
    if not p:
        return None
    try:
        import pandas as pd  # type: ignore
    except Exception:
        return None
    try:
        with _conn(p) as c:
            cur = c.execute(
                "SELECT date, open, high, low, close, volume FROM ohlcv "
                "WHERE ticker=? ORDER BY date DESC LIMIT ?",
                (ticker, days),
            )
            rows = cur.fetchall()
    except Exception:
        log.exception("[screener_adapter] load_ohlcv_df %s %s 실패", market, ticker)
        return None
    if not rows:
        return None
    rows.reverse()
    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = df[col].astype(float)
    return df


def db_status() -> dict:
    """진단 — 두 DB가 마운트됐는지·종목 수·최신 날짜."""
    out: dict[str, Any] = {}
    for market in ("KR", "US"):
        p = _db_for(market)
        if not p:
            out[market] = {"available": False}
            continue
        try:
            with _conn(p) as c:
                n = c.execute("SELECT COUNT(*) FROM tickers WHERE is_active=1").fetchone()[0]
                d = c.execute("SELECT MAX(date) FROM ohlcv").fetchone()[0]
                sd = c.execute("SELECT MAX(date) FROM signals").fetchone()[0]
            out[market] = {
                "available": True,
                "path": str(p),
                "active_tickers": n,
                "latest_ohlcv": d,
                "latest_signal": sd,
            }
        except Exception as e:
            out[market] = {"available": False, "error": str(e)}
    return out
