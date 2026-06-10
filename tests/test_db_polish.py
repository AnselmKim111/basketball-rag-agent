"""src/screener/db.py 폴리싱 변경 검증.

실제 임시 SQLite 파일로 ensure_schema + 인덱스 + alias 동작 확인.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_screener_db(monkeypatch, tmp_path):
    """매 테스트마다 새 SQLite 파일. import 캐시·_INITIALIZED 플래그·DB_PATH 모두 리셋."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for mod_name in list(sys.modules):
        if mod_name in ("src.screener.db", "src.state_store"):
            del sys.modules[mod_name]
    from src.screener import db as fresh_db
    # state_store._state_dir 결과가 캐시될 가능성 + DB_PATH는 module load 시 한 번
    # 평가 → 직접 덮어쓰기로 격리 보장.
    fresh_db.DB_PATH = Path(tmp_path) / "screener.db"
    fresh_db._INITIALIZED = False
    yield fresh_db


def test_ensure_schema_creates_signals_date_index(tmp_screener_db):
    db = tmp_screener_db
    db.ensure_schema()
    with db.get_connection() as c:
        rows = c.execute("PRAGMA index_list('signals')").fetchall()
    index_names = {r[1] for r in rows}
    assert "idx_signals_date" in index_names, (
        f"signals(date) 인덱스가 없습니다 — rows={rows}"
    )


def test_ensure_schema_seeds_schema_version(tmp_screener_db):
    db = tmp_screener_db
    db.ensure_schema()
    with db.get_connection() as c:
        cur = c.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "1"


def test_get_connection_is_public_alias(tmp_screener_db):
    """get_connection() 과 _conn() 가 같은 컨텍스트 매니저 자원이어야 한다.

    동시 호출은 RLock으로 직렬화되므로 단일 스레드에서는 sequential 사용 안전.
    """
    db = tmp_screener_db
    db.ensure_schema()
    with db.get_connection() as c1:
        c1.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('probe', 'ok')")
    with db._conn() as c2:  # 내부 함수 — 비교용으로만 사용
        v = c2.execute("SELECT value FROM meta WHERE key='probe'").fetchone()
    assert v[0] == "ok"


def test_recent_signals_delegates_to_load_signals_in_range(tmp_screener_db):
    """recent_signals(days_back)이 load_signals_in_range와 동일 결과를 내야 한다.

    내부 위임이라 함수 자체가 사라지진 않지만, 동작은 일관."""
    db = tmp_screener_db
    db.ensure_schema()
    today = date.today()
    db.save_signals(today.isoformat(), {
        "high_52w": [{"ticker": "AAA", "close": 100}],
    })
    db.save_signals((today - timedelta(days=2)).isoformat(), {
        "volume_breakout": [{"ticker": "BBB", "close": 50}],
    })
    a = db.recent_signals(days_back=7)
    # 같은 날짜 범위로 load_signals_in_range 직접 호출
    start = (today - timedelta(days=int(7 * 1.5))).isoformat()
    end = today.isoformat()
    b = db.load_signals_in_range(start, end)
    assert len(a) == len(b) == 2
    a_keys = {(r["date"], r["ticker"], r["signal"]) for r in a}
    b_keys = {(r["date"], r["ticker"], r["signal"]) for r in b}
    assert a_keys == b_keys


def test_load_signals_in_range_exclude_date(tmp_screener_db):
    """exclude_date 인자로 특정 날짜 제외."""
    db = tmp_screener_db
    db.ensure_schema()
    today = date.today()
    yest = (today - timedelta(days=1)).isoformat()
    db.save_signals(today.isoformat(), {"high_52w": [{"ticker": "T1", "close": 100}]})
    db.save_signals(yest, {"high_52w": [{"ticker": "T2", "close": 200}]})
    all_rows = db.load_signals_in_range((today - timedelta(days=5)).isoformat(),
                                         today.isoformat())
    excl_rows = db.load_signals_in_range((today - timedelta(days=5)).isoformat(),
                                          today.isoformat(),
                                          exclude_date=today.isoformat())
    assert len(all_rows) == 2
    assert len(excl_rows) == 1
    assert excl_rows[0]["ticker"] == "T2"
