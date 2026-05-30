"""데이터 카테고리별 전일 last-good 캐시 — 단일소스(FDR/FRED 등) 실패 시 섹션이
조용히 비는 것을 방지하는 최종 안전망.

각 카테고리 fetch 결과(주로 DataFrame dict)를 /data/report_cache 에 pickle 저장.
fetch가 비거나 예외나면 가장 최근 캐시를 재사용하고, 호출측이 stale로 표기.

저장 위치: /data/report_cache (Railway 볼륨) → 없으면 reports/_cache 폴백.
"""
from __future__ import annotations

import logging
import pickle
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _cache_dir() -> Path:
    for cand in ("/data/report_cache", "reports/_cache"):
        p = Path(cand)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    p = Path("reports/_cache")
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_put(key: str, obj, date_iso: str) -> None:
    """obj를 {date, value}로 pickle 저장. 실패해도 조용히 무시(보조 기능)."""
    try:
        path = _cache_dir() / f"{key}.pkl"
        with open(path, "wb") as f:
            pickle.dump({"date": date_iso, "value": obj}, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        log.warning("[report.cache] put 실패: %s", key)


def cache_get(key: str, max_age_days: int = 7):
    """(value, asof_date) 반환. 없거나 너무 오래됐으면 None."""
    try:
        path = _cache_dir() / f"{key}.pkl"
        if not path.exists():
            return None
        if (time.time() - path.stat().st_mtime) > max_age_days * 86400:
            return None
        with open(path, "rb") as f:
            d = pickle.load(f)
        return d.get("value"), d.get("date")
    except Exception:
        log.warning("[report.cache] get 실패: %s", key)
        return None


def fetch_or_cache(key: str, fetch_fn, date_iso: str, stale: list | None = None,
                   is_empty=None):
    """fetch_fn() 시도 → 성공/비어있지 않으면 캐시에 저장하고 반환.
    실패/빈 결과면 캐시 재사용 + stale 리스트에 (key, asof) 기록.

    is_empty(obj)->bool: 결과가 '비었다'고 볼 판정(기본: falsy 또는 빈 컨테이너).
    """
    def _empty(o):
        if is_empty is not None:
            return is_empty(o)
        if o is None:
            return True
        try:
            return len(o) == 0
        except TypeError:
            return False

    try:
        obj = fetch_fn()
    except Exception:
        log.exception("[report.cache] fetch 예외: %s", key)
        obj = None

    if not _empty(obj):
        cache_put(key, obj, date_iso)
        return obj

    cached = cache_get(key)
    if cached is not None:
        value, asof = cached
        if stale is not None:
            stale.append({"key": key, "asof": asof})
        log.warning("[report.cache] %s fetch 실패 → 전일 캐시 사용 (asof=%s)", key, asof)
        return value
    log.warning("[report.cache] %s fetch 실패 + 캐시 없음", key)
    return obj
