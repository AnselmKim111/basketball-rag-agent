"""Pre-Call 보고서 캐시 — 콜 발표 전 합성한 기대치를 저장.

storage layout:
  {STATE_ROOT}/earnings_precall/{TICKER}/Q{q}_{y}.json

페이로드:
  {
    "ticker": "...", "year": 2026, "quarter": 1, "fiscal_label": "Q1 FY2026",
    "generated_at": ISO,
    "consensus": {...} | null,   # 그 시점 컨센서스 스냅샷
    "report_text": "한국어 markdown 4000-6000자",
    "key_questions": [...],      # §D 10개 질문 (있으면 LLM이 별도 추출)
    "pre_call_bias": "long ★★★ / ...",
  }

콜 transcript 등재 후 자동으로 "pre vs actual" diff에 사용 — 같은 분기 actual 보고서에 첨부.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_LOCK = threading.RLock()


def _root() -> Path:
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v) / "earnings_precall"
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                continue
    for candidate in ("/data", "/app/data", "/tmp"):
        try:
            p = Path(candidate) / "earnings_precall"
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return Path("/tmp")


def _path(ticker: str, year: int, quarter: int) -> Path:
    d = _root() / ticker.upper().strip()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"Q{quarter}_{year}.json"


def save(
    ticker: str, year: int, quarter: int, fiscal_label: str,
    report_text: str, consensus=None, key_questions: list | None = None,
    pre_call_bias: str = "",
) -> Path | None:
    if not ticker or not year or not quarter:
        return None
    payload = {
        "ticker": ticker.upper(),
        "year": int(year),
        "quarter": int(quarter),
        "fiscal_label": fiscal_label,
        "generated_at": datetime.now(KST).isoformat(),
        "consensus": _to_dict(consensus),
        "report_text": report_text or "",
        "key_questions": key_questions or [],
        "pre_call_bias": pre_call_bias or "",
    }
    p = _path(ticker, year, quarter)
    with _LOCK:
        try:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
            log.info("[earnings/precall] saved %s", p)
            return p
        except Exception:
            log.exception("[earnings/precall] save 실패: %s", p)
            return None


def load(ticker: str, year: int, quarter: int) -> dict | None:
    p = _path(ticker, year, quarter)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.exception("[earnings/precall] load 실패: %s", p)
        return None


def exists(ticker: str, year: int, quarter: int) -> bool:
    return _path(ticker, year, quarter).exists()


def _to_dict(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    try:
        return {k: _to_dict(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    except Exception:
        return str(obj)


def diag_log() -> None:
    root = _root()
    try:
        tickers = sorted({p.name for p in root.iterdir() if p.is_dir()})
        log.info("[earnings/precall] root=%s tickers=%d (%s)", root, len(tickers), ",".join(tickers[:10]))
    except Exception:
        log.info("[earnings/precall] root=%s (listing 실패)", root)
