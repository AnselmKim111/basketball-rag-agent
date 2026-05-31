"""어닝콜 longitudinal memory — 분기별 추출·컨센서스·검증 결과 영속화.

storage layout:
  {STATE_ROOT}/earnings_history/{TICKER}/FY{year}Q{q}.json
  {STATE_ROOT}/earnings_history/{TICKER}/_index.json   # quarter list cache

각 파일 페이로드:
  {
    "ticker": "...", "year": 2026, "quarter": 1, "fiscal_label": "Q1 FY2026",
    "saved_at": ISO,
    "extract": {...},                # transcripts.fetch_and_extract 결과 (Track A 확장 스키마)
    "consensus": {...} | null,       # ConsensusSnapshot as dict (Track B)
    "consensus_deltas": [...],       # [{metric, actual, consensus, delta_pct, direction, message}]
    "verify": [...],                 # [{ticker, metric, status, message}]
    "synthesis_excerpt": "...",      # synthesis 본문(있으면)에서 그 ticker 관련 부분 추출
    "counter_excerpt": "...",        # counter-thesis 본문
  }

idea_cache 패턴을 참고해 STATE_DIR/Railway 볼륨 우선 → fallback 체인.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_LOCK = threading.RLock()


# ------------------------------------------------------------------
# 경로 helper
# ------------------------------------------------------------------
def _root() -> Path:
    """earnings_history 루트. STATE_DIR/볼륨 우선, 없으면 /data, /tmp 폴백."""
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v) / "earnings_history"
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                continue
    for candidate in ("/data", "/app/data", "/tmp"):
        try:
            p = Path(candidate) / "earnings_history"
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return Path("/tmp")


def _ticker_dir(ticker: str) -> Path:
    p = _root() / ticker.upper().strip()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _quarter_file(ticker: str, year: int, quarter: int) -> Path:
    return _ticker_dir(ticker) / f"FY{year}Q{quarter}.json"


# ------------------------------------------------------------------
# 직렬화
# ------------------------------------------------------------------
def _to_jsonable(obj: Any) -> Any:
    """dataclass/객체를 dict로. 그 외엔 그대로."""
    if obj is None:
        return None
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)):
        return obj
    # ConsensusSnapshot 등 임의 객체
    try:
        return {k: _to_jsonable(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    except Exception:
        return str(obj)


# ------------------------------------------------------------------
# 공개 API
# ------------------------------------------------------------------
def save_call(
    ticker: str,
    year: int | None,
    quarter: int | None,
    fiscal_label: str,
    extract: dict | None,
    consensus: Any | None = None,
    consensus_deltas: list | None = None,
    verify_results: list | None = None,
    synthesis_excerpt: str = "",
    counter_excerpt: str = "",
) -> Path | None:
    """한 콜 분량을 저장. year/quarter 없으면 skip (저장 위치를 잡을 수 없음)."""
    if not ticker or not year or not quarter:
        log.info("[earnings/history] save skip — ticker/year/quarter 부족 (%s/%s/%s)", ticker, year, quarter)
        return None
    payload = {
        "ticker": ticker.upper(),
        "year": int(year),
        "quarter": int(quarter),
        "fiscal_label": fiscal_label,
        "saved_at": datetime.now(KST).isoformat(),
        "extract": _to_jsonable(extract),
        "consensus": _to_jsonable(consensus),
        "consensus_deltas": _to_jsonable(consensus_deltas or []),
        "verify": _to_jsonable(verify_results or []),
        "synthesis_excerpt": synthesis_excerpt or "",
        "counter_excerpt": counter_excerpt or "",
    }
    path = _quarter_file(ticker, year, quarter)
    with _LOCK:
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            log.info("[earnings/history] saved %s", path)
        except Exception:
            log.exception("[earnings/history] save 실패: %s", path)
            return None
    # Phase 7F — knowledge_db에도 백업 (검색·digest용). 실패해도 JSON은 이미 저장됨.
    try:
        from src.earnings import knowledge_db
        ext = extract if isinstance(extract, dict) else {}
        knowledge_db.upsert_call(
            ticker=ticker, year=year, quarter=quarter,
            call_date=(ext.get("call_date") if isinstance(ext, dict) else "") or "",
            source=(ext.get("source") if isinstance(ext, dict) else "") or "",
            grounded=bool((ext.get("grounded") if isinstance(ext, dict) else False)),
            transcript_chars=int((ext.get("transcript_chars") if isinstance(ext, dict) else 0) or 0),
            extract=ext,
            synthesis_text=synthesis_excerpt or "",
            counter_text=counter_excerpt or "",
            retrospective_text=(ext.get("_retrospective") if isinstance(ext, dict) else "") or "",
            quality=(ext.get("_gate_extract") if isinstance(ext, dict) else None),
        )
    except Exception:
        log.exception("[earnings/history] knowledge_db upsert 실패 (JSON은 정상 저장)")
    return path


def load_call(ticker: str, year: int, quarter: int) -> dict | None:
    p = _quarter_file(ticker, year, quarter)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.exception("[earnings/history] load 실패: %s", p)
        return None


def list_quarters(ticker: str) -> list[tuple[int, int]]:
    """그 ticker의 저장된 (year, quarter) 리스트 — 신규순 정렬."""
    d = _ticker_dir(ticker)
    out: list[tuple[int, int]] = []
    if not d.exists():
        return out
    for f in d.glob("FY*Q*.json"):
        stem = f.stem  # FY2026Q1
        try:
            y = int(stem[2:6])
            q = int(stem[7:])
            out.append((y, q))
        except Exception:
            continue
    out.sort(reverse=True)  # 최신 분기 먼저
    return out


def load_recent(ticker: str, n: int = 4, exclude: tuple[int, int] | None = None) -> list[dict]:
    """최근 n개 분기 페이로드 (신규순). exclude로 현재 분기 제외 가능."""
    qs = list_quarters(ticker)
    if exclude:
        qs = [q for q in qs if q != exclude]
    out: list[dict] = []
    for y, q in qs[:n]:
        item = load_call(ticker, y, q)
        if item:
            out.append(item)
    return out


def diag_log() -> None:
    """루트 위치 + 저장된 ticker 수를 INFO 로그. orchestrator 부팅 진단용."""
    root = _root()
    try:
        tickers = sorted({p.name for p in root.iterdir() if p.is_dir()})
        log.info("[earnings/history] root=%s tickers=%d (%s)", root, len(tickers), ",".join(tickers[:10]))
    except Exception:
        log.info("[earnings/history] root=%s (listing 실패)", root)
