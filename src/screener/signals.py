"""4가지 기술적 신호 계산 (순수 함수).

신호:
  1. 신고가 (20/60/252일) — 종가가 과거 N-1일 최고가 초과
  2. 일목구름 상방 돌파 — 표준 9/26/52, 어제 cloud_top 이하 → 오늘 cloud_top 초과
  3. 거래량 돌파 — 오늘 거래량 ≥ 20일 평균 × ratio AND 종가 상승
  4. 돌파 직전 — 종가가 52주 고점 95-99% AND 5일 거래량 증가 추세

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
    """
    if len(rows) < 60:
        return {}
    pd = _import_pd()
    df = pd.DataFrame(rows)

    today = df.iloc[-1]
    prev = df.iloc[-2]

    out: dict = {}

    # 1) 신고가 20/60/252
    for window, key in [(20, "high_20"), (60, "high_60"), (252, "high_52w")]:
        if len(df) >= window:
            past_high = df["high"].iloc[-window:-1].max()
            if today["close"] > past_high and past_high > 0:
                out[key] = {
                    "close": int(today["close"]),
                    "prev_high": int(past_high),
                    "pct": float((today["close"] / past_high - 1) * 100),
                }

    # 2) 일목구름 상방 돌파 (9/26/52)
    if len(df) >= 78:
        try:
            tenkan = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
            kijun = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
            senkou_a = ((tenkan + kijun) / 2).shift(26)
            senkou_b = (
                (df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2
            ).shift(26)
            cloud_top = pd.concat([senkou_a, senkou_b], axis=1).max(axis=1)
            ct_today = cloud_top.iloc[-1]
            ct_prev = cloud_top.iloc[-2]
            if pd.notna(ct_today) and pd.notna(ct_prev):
                if prev["close"] <= ct_prev and today["close"] > ct_today:
                    out["ichimoku_breakout"] = {
                        "close": int(today["close"]),
                        "cloud_top": float(ct_today),
                        "pct_above": float((today["close"] / ct_today - 1) * 100),
                    }
        except Exception:
            log.exception("ichimoku 계산 실패")

    # 3) 거래량 돌파
    vol_ratio_threshold = _get_float_env(
        "SCREENER_VOL_BREAKOUT_RATIO", DEFAULT_VOL_BREAKOUT_RATIO
    )
    if len(df) >= 22:
        vol_ma20 = df["volume"].rolling(20).mean().iloc[-2]
        if pd.notna(vol_ma20) and vol_ma20 > 0 and prev["close"] > 0:
            vol_ratio = float(today["volume"] / vol_ma20)
            chg_pct = float((today["close"] / prev["close"] - 1) * 100)
            if vol_ratio >= vol_ratio_threshold and chg_pct > 0:
                out["volume_breakout"] = {
                    "close": int(today["close"]),
                    "vol_ratio": vol_ratio,
                    "chg_pct": chg_pct,
                    "volume": int(today["volume"]),
                }

    # 4) 돌파 직전 (52주 고점 95-99%)
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
                            }

    return out


# 카테고리 정렬 키 (pct/등락률 desc)
def _sort_key(category: str, item: dict) -> float:
    if category in ("high_20", "high_60", "high_52w"):
        return -float(item.get("pct", 0))
    if category == "ichimoku_breakout":
        return -float(item.get("pct_above", 0))
    if category == "volume_breakout":
        return -float(item.get("chg_pct", 0))
    if category == "near_breakout_52w":
        return -float(item.get("proximity_pct", 0))
    return 0


CATEGORIES = [
    "high_52w",
    "ichimoku_breakout",
    "volume_breakout",
    "near_breakout_52w",
    "high_60",
    "high_20",
]


def compute_all() -> dict[str, list[dict]]:
    """전 종목 신호 계산 → 카테고리별 정렬된 리스트.

    {category_key: [ {ticker, name, market, market_cap, ...신호 페이로드}, ... ]}

    시가총액 < SCREENER_MIN_MARKET_CAP (기본 3000억) 종목은 신호에서 제외.
    시총 정보 없는 (NULL) 종목은 보수적으로 통과 (신규상장/시총 fetch 실패 대비).
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
    for tinfo in tickers:
        ticker = tinfo["ticker"]
        cap = tinfo.get("market_cap")
        # 시총 필터 (NULL은 통과)
        if cap is not None and cap < min_cap:
            skipped_cap += 1
            continue
        rows = db.load_ohlcv(ticker, days=260)
        if len(rows) < 60:
            continue
        try:
            sigs = compute_signals_for_ticker(rows)
        except Exception:
            log.exception("[signals] %s 계산 실패", ticker)
            continue
        for cat_key, payload in sigs.items():
            entry = {
                "ticker": ticker,
                "name": tinfo.get("name") or ticker,
                "market": tinfo.get("market") or "",
                "market_cap": cap,
                **payload,
            }
            by_cat.setdefault(cat_key, []).append(entry)
        processed += 1

    # 정렬
    for cat, items in by_cat.items():
        items.sort(key=lambda it: _sort_key(cat, it))

    log.info(
        "[signals] processed=%d skipped_cap=%d (min=%.1f억) categories=%s",
        processed, skipped_cap, min_cap / 1e8,
        {k: len(v) for k, v in by_cat.items()},
    )
    return by_cat
