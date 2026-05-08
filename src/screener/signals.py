"""기술적 신호 계산 (순수 함수).

신호 (사용자 요청 — 노이즈 적은 핵심만):
  1. 52주 신고가 — 종가가 과거 252영업일(1년) 최고가 초과
  2. 역사적 신고가 — 종가가 보유 데이터 전체(280일) 최고가 초과
  3. 거래량 돌파 — 오늘 거래량 ≥ 20일 평균 × ratio AND 종가 상승
  4. 52주 돌파 직전 — 종가가 52주 고점 95-99% AND 5일 거래량 증가 추세

제거됨:
  - 일목구름 상방 돌파 (노이즈 다수)
  - 20일/60일 신고가 (단기 노이즈)

모든 entry에 chg_pct (전일 대비 %) 포함. 정렬은 시총·상승률 복합 점수.

pandas/numpy 사용. 호출자가 종목 단위로 loop하며 단일 종목 결과를 모음.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from src.screener import db, universe

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 임계값 — env 오버라이드 가능
DEFAULT_VOL_BREAKOUT_RATIO = 2.0
DEFAULT_NEAR_BREAKOUT_LOWER = 0.95
DEFAULT_NEAR_BREAKOUT_UPPER = 0.99
# 시가총액 필터 (원). 기본 3000억 — 사용자 요구사항.
DEFAULT_MIN_MARKET_CAP = 300_000_000_000


def _get_float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except ValueError:
        return default


def _import_pd():
    import pandas as pd  # type: ignore
    return pd


def compute_signals_for_ticker(rows: list[dict]) -> dict:
    """단일 종목 OHLCV (asc, 마지막 = today) → 발현 신호 dict.

    rows 길이 < 60이면 {} (신규상장).
    모든 신호 페이로드에 chg_pct(전일 대비 %)와 close가 공통 포함.
    """
    if len(rows) < 60:
        return {}
    pd = _import_pd()
    df = pd.DataFrame(rows)

    today = df.iloc[-1]
    prev = df.iloc[-2]

    chg_pct = 0.0
    if prev["close"] > 0:
        chg_pct = float((today["close"] / prev["close"] - 1) * 100)

    out: dict = {}

    # 1) 52주 신고가 (252영업일)
    if len(df) >= 252:
        past_high_52w = df["high"].iloc[-252:-1].max()
        if today["close"] > past_high_52w and past_high_52w > 0:
            out["high_52w"] = {
                "close": int(today["close"]),
                "prev_high": int(past_high_52w),
                "pct": float((today["close"] / past_high_52w - 1) * 100),
                "chg_pct": chg_pct,
            }

    # 2) 역사적 신고가 (보유 데이터 전체 — 280일 보존)
    past_high_all = df["high"].iloc[:-1].max()
    if today["close"] > past_high_all and past_high_all > 0:
        out["high_all"] = {
            "close": int(today["close"]),
            "prev_high": int(past_high_all),
            "pct": float((today["close"] / past_high_all - 1) * 100),
            "chg_pct": chg_pct,
            "history_days": int(len(df)),
        }

    # 3) 거래량 돌파
    vol_ratio_threshold = _get_float_env(
        "SCREENER_VOL_BREAKOUT_RATIO", DEFAULT_VOL_BREAKOUT_RATIO
    )
    if len(df) >= 22:
        vol_ma20 = df["volume"].rolling(20).mean().iloc[-2]
        if pd.notna(vol_ma20) and vol_ma20 > 0 and prev["close"] > 0:
            vol_ratio = float(today["volume"] / vol_ma20)
            if vol_ratio >= vol_ratio_threshold and chg_pct > 0:
                out["volume_breakout"] = {
                    "close": int(today["close"]),
                    "vol_ratio": vol_ratio,
                    "chg_pct": chg_pct,
                    "volume": int(today["volume"]),
                }

    # 4) 52주 돌파 직전 (95-99%)
    near_lo = _get_float_env(
        "SCREENER_NEAR_BREAKOUT_LOWER", DEFAULT_NEAR_BREAKOUT_LOWER
    )
    near_hi = _get_float_env(
        "SCREENER_NEAR_BREAKOUT_UPPER", DEFAULT_NEAR_BREAKOUT_UPPER
    )
    if len(df) >= 252:
        hi52 = df["high"].iloc[-252:-1].max()
        if hi52 > 0:
            proximity = float(today["close"] / hi52)
            if near_lo <= proximity <= near_hi:
                if len(df) >= 26:
                    recent5 = df["volume"].iloc[-6:-1].mean()
                    prior15 = df["volume"].iloc[-21:-6].mean()
                    if pd.notna(prior15) and prior15 > 0:
                        vol_trend = float(recent5 / prior15)
                        if vol_trend >= 1.3:
                            out["near_breakout_52w"] = {
                                "close": int(today["close"]),
                                "hi52": int(hi52),
                                "proximity_pct": proximity * 100,
                                "vol_trend": vol_trend,
                                "chg_pct": chg_pct,
                            }

    return out


CATEGORIES = [
    "high_all",
    "high_52w",
    "volume_breakout",
    "near_breakout_52w",
]


def _composite_score(item: dict, max_cap: float) -> float:
    """시총 + 상승률 복합 점수.

    score = 0.5 * normalized_chg_pct + 0.5 * normalized_log_cap
      - normalized_chg_pct: 0 ~ 30% 기준 0~1 (clip)
      - normalized_log_cap: log10(cap) / log10(max_cap)
    상승률 높고 시총 큰 종목이 우선. desc 정렬용 → 음수 반환.
    """
    chg = max(0.0, float(item.get("chg_pct", 0.0)))
    norm_chg = min(chg / 30.0, 1.0)
    cap = item.get("market_cap")
    if cap and cap > 0 and max_cap > 0:
        import math
        norm_cap = math.log10(cap) / math.log10(max_cap) if max_cap > 1 else 0.5
        norm_cap = max(0.0, min(1.0, norm_cap))
    else:
        norm_cap = 0.0
    score = 0.5 * norm_chg + 0.5 * norm_cap
    return -score


def compute_all() -> dict[str, list[dict]]:
    """전 종목 신호 계산 → 카테고리별 시총·상승률 복합 정렬.

    {category_key: [ {ticker, name, market, market_cap, sector, chg_pct, ...}, ... ]}

    시가총액 < SCREENER_MIN_MARKET_CAP (기본 3000억) 종목은 신호에서 제외.
    시총 정보 없는 (NULL) 종목은 보수적으로 통과.
    """
    db.ensure_schema()
    tickers = db.get_active_tickers()
    if not tickers:
        log.warning("[signals] universe 비어있음")
        return {}

    min_cap = _get_float_env("SCREENER_MIN_MARKET_CAP", DEFAULT_MIN_MARKET_CAP)

    by_cat: dict[str, list[dict]] = {k: [] for k in CATEGORIES}

    processed = 0
    skipped_cap = 0
    max_cap_seen = 0
    for tinfo in tickers:
        ticker = tinfo["ticker"]
        cap = tinfo.get("market_cap")
        if cap is not None and cap < min_cap:
            skipped_cap += 1
            continue
        rows = db.load_ohlcv(ticker, days=300)
        if len(rows) < 60:
            continue
        try:
            sigs = compute_signals_for_ticker(rows)
        except Exception:
            log.exception("[signals] %s 계산 실패", ticker)
            continue
        if cap and cap > max_cap_seen:
            max_cap_seen = cap
        for cat_key, payload in sigs.items():
            if cat_key not in by_cat:
                continue
            entry = {
                "ticker": ticker,
                "name": tinfo.get("name") or ticker,
                "market": tinfo.get("market") or "",
                "market_cap": cap,
                "sector": tinfo.get("sector") or "",
                **payload,
            }
            by_cat[cat_key].append(entry)
        processed += 1

    # 시총+상승률 복합 정렬
    for cat, items in by_cat.items():
        items.sort(key=lambda it: _composite_score(it, max_cap_seen))

    log.info(
        "[signals] processed=%d skipped_cap=%d (min=%.1f억) categories=%s",
        processed, skipped_cap, min_cap / 1e8,
        {k: len(v) for k, v in by_cat.items()},
    )
    return by_cat
