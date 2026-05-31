"""Predictive Track Record (Phase 7C) — earnings 봇이 출력한 예측의 추적·검증.

테이블:
  predictions(id, ticker, year, quarter, type, statement_text, conviction,
              metric_threshold_json, payload_excerpt, created_at)
    type ∈ counter|trigger|action|guidance|question
  verifications(id, prediction_id, verified_year, verified_quarter, verified_at,
                outcome, evidence_text, model_used)
    outcome ∈ correct|wrong|partial|unverifiable

screener.db 패턴 차용 (WAL mode, idempotent schema, thread-safe RLock).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _db_path() -> Path:
    """STATE_DIR 우선 → /data → /tmp 폴백."""
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v)
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p / "earnings_predictions.db"
            except Exception:
                continue
    for candidate in ("/data", "/app/data", "/tmp"):
        try:
            p = Path(candidate)
            p.mkdir(parents=True, exist_ok=True)
            return p / "earnings_predictions.db"
        except Exception:
            continue
    return Path("/tmp/earnings_predictions.db")


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
    """첫 호출 시 schema 생성. idempotent."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS predictions (
              id                    INTEGER PRIMARY KEY AUTOINCREMENT,
              ticker                TEXT NOT NULL,
              year                  INTEGER NOT NULL,
              quarter               INTEGER NOT NULL,
              type                  TEXT NOT NULL,
              statement_text        TEXT NOT NULL,
              conviction            INTEGER,
              metric_threshold_json TEXT,
              payload_excerpt       TEXT,
              created_at            TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pred_ticker_q ON predictions(ticker, year, quarter);
            CREATE INDEX IF NOT EXISTS idx_pred_type ON predictions(type);

            CREATE TABLE IF NOT EXISTS verifications (
              id                INTEGER PRIMARY KEY AUTOINCREMENT,
              prediction_id     INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
              verified_year     INTEGER NOT NULL,
              verified_quarter  INTEGER NOT NULL,
              verified_at       TEXT NOT NULL,
              outcome           TEXT NOT NULL,
              evidence_text     TEXT,
              model_used        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_verif_pred ON verifications(prediction_id);
            CREATE INDEX IF NOT EXISTS idx_verif_outcome ON verifications(outcome);
            """
        )
    _INITIALIZED = True
    log.info("[earnings/predictions_db] schema ready at %s", p)


# ------------------------------------------------------------------
# INSERT
# ------------------------------------------------------------------
def insert_prediction(
    ticker: str, year: int, quarter: int, type_: str,
    statement_text: str, conviction: int | None = None,
    metric_threshold: dict | None = None, payload_excerpt: str = "",
) -> int | None:
    """1건 INSERT. id 반환."""
    if not (ticker and statement_text and type_):
        return None
    ensure_schema()
    now = datetime.now(KST).isoformat()
    try:
        with _conn() as c:
            cur = c.execute(
                """INSERT INTO predictions
                   (ticker, year, quarter, type, statement_text, conviction,
                    metric_threshold_json, payload_excerpt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticker.upper(), int(year), int(quarter), type_,
                    statement_text[:2000], conviction,
                    json.dumps(metric_threshold or {}, ensure_ascii=False) if metric_threshold else None,
                    (payload_excerpt or "")[:1500],
                    now,
                ),
            )
            return cur.lastrowid
    except Exception:
        log.exception("[earnings/predictions_db] insert_prediction 실패")
        return None


def insert_predictions_bulk(rows: list[dict]) -> int:
    """rows의 각 dict는 insert_prediction 인자와 동일. 성공 건수 반환."""
    n = 0
    for r in rows:
        pid = insert_prediction(
            ticker=r.get("ticker", ""),
            year=r.get("year", 0),
            quarter=r.get("quarter", 0),
            type_=r.get("type", "counter"),
            statement_text=r.get("statement_text", ""),
            conviction=r.get("conviction"),
            metric_threshold=r.get("metric_threshold"),
            payload_excerpt=r.get("payload_excerpt", ""),
        )
        if pid:
            n += 1
    return n


def insert_verification(
    prediction_id: int, verified_year: int, verified_quarter: int,
    outcome: str, evidence_text: str = "", model_used: str = "",
) -> int | None:
    ensure_schema()
    now = datetime.now(KST).isoformat()
    try:
        with _conn() as c:
            cur = c.execute(
                """INSERT INTO verifications
                   (prediction_id, verified_year, verified_quarter, verified_at,
                    outcome, evidence_text, model_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(prediction_id), int(verified_year), int(verified_quarter), now,
                    outcome, (evidence_text or "")[:1500], (model_used or "")[:100],
                ),
            )
            return cur.lastrowid
    except Exception:
        log.exception("[earnings/predictions_db] insert_verification 실패")
        return None


# ------------------------------------------------------------------
# QUERY
# ------------------------------------------------------------------
def list_unverified_predictions(ticker: str, before_year: int, before_quarter: int) -> list[dict]:
    """지정 분기 이전의 verifications 없는 prediction 모음.

    (year < before_year) OR (year == before_year AND quarter < before_quarter)
    """
    ensure_schema()
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT p.id, p.ticker, p.year, p.quarter, p.type, p.statement_text,
                          p.conviction, p.metric_threshold_json, p.payload_excerpt, p.created_at
                   FROM predictions p
                   LEFT JOIN verifications v ON v.prediction_id = p.id
                   WHERE p.ticker = ?
                     AND v.id IS NULL
                     AND (p.year < ? OR (p.year = ? AND p.quarter < ?))
                   ORDER BY p.year DESC, p.quarter DESC, p.id ASC""",
                (ticker.upper(), int(before_year), int(before_year), int(before_quarter)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[earnings/predictions_db] list_unverified 실패")
        return []


def credibility_score(ticker: str) -> dict:
    """누적 적중률 + 최근 5개 verifications."""
    ensure_schema()
    out: dict = {"ticker": ticker.upper(), "total": 0, "correct": 0, "wrong": 0,
                 "partial": 0, "unverifiable": 0, "recent": [], "score": None}
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT v.outcome FROM verifications v
                   JOIN predictions p ON p.id = v.prediction_id
                   WHERE p.ticker = ?""",
                (ticker.upper(),),
            ).fetchall()
            for r in rows:
                out["total"] += 1
                key = r["outcome"]
                if key in ("correct", "wrong", "partial", "unverifiable"):
                    out[key] = out.get(key, 0) + 1
            recents = c.execute(
                """SELECT p.ticker, p.year, p.quarter, p.type, p.statement_text,
                          v.outcome, v.evidence_text, v.verified_at
                   FROM verifications v
                   JOIN predictions p ON p.id = v.prediction_id
                   WHERE p.ticker = ?
                   ORDER BY v.verified_at DESC LIMIT 5""",
                (ticker.upper(),),
            ).fetchall()
            out["recent"] = [dict(r) for r in recents]
        if out["total"]:
            scored = out["correct"] + 0.5 * out["partial"]
            out["score"] = round(scored / max(1, out["total"]), 3)
    except Exception:
        log.exception("[earnings/predictions_db] credibility_score 실패")
    return out


def fmt_credibility(score: dict) -> str:
    """텔레그램용 한국어 요약."""
    if not score:
        return ""
    if score.get("total", 0) == 0:
        return f"📈 {score.get('ticker','?')}: 아직 검증된 예측 없음 (분기 후 누적 시작)"
    lines = [
        f"📈 {score['ticker']} 봇 적중률",
        f"  · 누적 점수: {score.get('score'):.1%} ({score['correct']}/{score['total']} 정확, "
        f"{score['partial']} 부분, {score['wrong']} 오답)",
    ]
    recents = score.get("recent") or []
    if recents:
        lines.append("  · 최근 5건:")
        for r in recents:
            badge = {"correct": "✅", "partial": "🟡", "wrong": "❌"}.get(r.get("outcome"), "❓")
            qy = f"FY{r.get('year')}Q{r.get('quarter')}"
            t = (r.get("statement_text") or "")[:90]
            lines.append(f"    {badge} {qy} [{r.get('type','?')}]: {t}")
    return "\n".join(lines)


def diag_log() -> None:
    """orchestrator 부팅 진단용."""
    try:
        ensure_schema()
        with _conn() as c:
            np = c.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()["n"]
            nv = c.execute("SELECT COUNT(*) AS n FROM verifications").fetchone()["n"]
        log.info("[earnings/predictions_db] path=%s predictions=%d verifications=%d",
                 _db_path(), np, nv)
    except Exception:
        log.exception("[earnings/predictions_db] diag 실패")
