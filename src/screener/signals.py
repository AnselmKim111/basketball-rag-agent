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


def compute_signals_for_ticker(rows: list[dict], base_date: str | None = None) -> dict:
    """단일 종목 OHLCV (asc) → 발현 신호 dict. **base_date-anchored**.

    rows: date asc로 정렬된 OHLCV.
    base_date: "YYYY-MM-DD" — 이 날짜의 close를 today로 사용. 명시 안 하면 마지막 row.

    핵심 가드 (잘못된 신호 방지):
      - base_date 명시 시: 그 날짜 row가 없으면 {} 반환 (silent skip)
      - base_date 이후 row는 무시 (truncate) — 미래 데이터 누설 방지
      - rows 길이 < 60이면 {} (신규상장)
      - 결과 검증: prev_close > 0이고 today_close > 0이어야 신호 발생
    """
    if not rows:
        return {}
    pd = _import_pd()
    df = pd.DataFrame(rows)

    # base_date anchoring — 그 날짜의 row를 today로 explicit lookup
    if base_date is not None:
        base_idx = df.index[df["date"] == base_date].tolist()
        if not base_idx:
            return {}  # base_date 데이터 없는 종목은 신호 계산 skip
        # base_date 이후 row 제거 (안전성)
        df = df.iloc[: base_idx[0] + 1].reset_index(drop=True)

    if len(df) < 60:
        return {}

    today = df.iloc[-1]
    prev = df.iloc[-2]

    # today의 date가 진짜 base_date인지 sanity check (base_date 명시된 경우)
    if base_date is not None and str(today["date"]) != base_date:
        log.warning(
            "[signals] base_date 불일치 — expected=%s actual=%s — skip",
            base_date, today["date"],
        )
        return {}

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

    # 5) VCP 돌파 (최근 2주 이내 — 러프 버전)
    # "오늘 돌파"만 잡던 4중 AND를 완화: 변동성 수축 base 형성 후 최근 N영업일
    # (SCREENER_VCP_WINDOW, 기본 10=2주) 중 박스권 상단을 돌파한 종목.
    #   (a) base: 돌파 window 직전 50일 박스권 (high/low ≤ SCREENER_VCP_BASE_MAX, 1.25)
    #   (b) 변동성 수축: base 후반 20일 ATR ≤ 전반 30일 ATR × SCREENER_VCP_ATR_MAX (0.75)
    #   (c) 돌파: 최근 window일 중 종가가 base 박스권 high 초과한 날 존재 (거래량 조건은 러프하게 생략)
    vcp_window = int(_get_float_env("SCREENER_VCP_WINDOW", 10))
    if len(df) >= 50 + vcp_window:
        try:
            # base 구간: 돌파 window 직전 50일
            base = df.iloc[-(50 + vcp_window):-vcp_window]
            base_high = float(base["high"].max())
            base_low = float(base["low"].min())
            base_ratio = base_high / base_low if base_low > 0 else 999

            # ATR 수축 (base 구간 후반 20일 vs 그 전 30일)
            tr = (df["high"] - df["low"]).astype(float)
            atr_recent = float(tr.iloc[-(vcp_window + 20):-vcp_window].mean())
            atr_base = float(tr.iloc[-(vcp_window + 50):-(vcp_window + 20)].mean())
            atr_contraction = (atr_recent / atr_base) if atr_base > 0 else 999

            vcp_base_max = _get_float_env("SCREENER_VCP_BASE_MAX", 1.25)
            vcp_atr_max = _get_float_env("SCREENER_VCP_ATR_MAX", 0.75)

            # 최근 window일 중 종가가 base 박스권 상단 돌파한 날
            recent = df.iloc[-vcp_window:]
            above = recent[recent["close"] > base_high]

            if base_ratio <= vcp_base_max and atr_contraction <= vcp_atr_max and len(above) > 0:
                # 가장 최근 돌파일까지 며칠 전인지 (0 = 오늘/base_date)
                last_pos = above.index[-1]
                days_ago = int(df.index[-1] - last_pos)
                breakout_close = int(above["close"].iloc[-1])
                out["vcp_breakout"] = {
                    "close": int(today["close"]),
                    "base_high": int(base_high),
                    "base_low": int(base_low),
                    "base_ratio": base_ratio,
                    "atr_contraction": atr_contraction,
                    "days_ago": days_ago,
                    "breakout_close": breakout_close,
                    "chg_pct": chg_pct,
                }
        except Exception:
            log.exception("VCP 계산 실패")

    return out


CATEGORIES = [
    "high_all",
    "high_52w",
    "vcp_breakout",
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


def compute_all(base_date: str | None = None) -> tuple[dict[str, list[dict]], dict]:
    """전 종목 신호 계산 → 카테고리별 시총·상승률 복합 정렬.

    base_date: "YYYY-MM-DD" — 모든 종목이 이 날짜의 close를 today로 사용.
               None이면 db.latest_date() 자동 사용.

    반환: (results, stats) 튜플
      results: {category_key: [...]}
      stats: {"base_date": str, "processed": int, "skipped_cap": int,
              "skipped_no_base": int, "total_active": int}

    구조 변경 (잘못된 신호 영구 방지):
      - 모든 신호가 base_date의 close 기준 — 종목별 "today" 다른 날짜 가능성 차단
      - base_date 데이터 없는 종목 = silent skip + skipped_no_base 카운트
      - stats에 검증 정보 포함 → 사용자에게 명시적 표기
    """
    db.ensure_schema()
    tickers = db.get_active_tickers()
    if not tickers:
        log.warning("[signals] universe 비어있음")
        return {}, {"base_date": base_date, "processed": 0, "skipped_cap": 0,
                    "skipped_no_base": 0, "total_active": 0}

    if base_date is None:
        base_date = db.latest_date()
        log.info("[signals] base_date 자동 결정: %s", base_date)
    else:
        log.info("[signals] base_date 명시: %s", base_date)

    min_cap = _get_float_env("SCREENER_MIN_MARKET_CAP", DEFAULT_MIN_MARKET_CAP)

    by_cat: dict[str, list[dict]] = {k: [] for k in CATEGORIES}

    processed = 0
    skipped_cap = 0
    skipped_no_base = 0
    max_cap_seen = 0
    for tinfo in tickers:
        ticker = tinfo["ticker"]
        cap = tinfo.get("market_cap")
        if cap is not None and cap < min_cap:
            skipped_cap += 1
            continue
        rows = db.load_ohlcv(ticker, days=1300)
        if len(rows) < 60:
            continue
        # base_date row 보유 여부 검증
        has_base = any(r["date"] == base_date for r in rows)
        if not has_base:
            skipped_no_base += 1
            continue
        try:
            sigs = compute_signals_for_ticker(rows, base_date=base_date)
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

    stats = {
        "base_date": base_date,
        "processed": processed,
        "skipped_cap": skipped_cap,
        "skipped_no_base": skipped_no_base,
        "total_active": len(tickers),
    }
    log.info(
        "[signals] base_date=%s processed=%d skipped_cap=%d skipped_no_base=%d "
        "(min=%.1f억) categories=%s",
        base_date, processed, skipped_cap, skipped_no_base, min_cap / 1e8,
        {k: len(v) for k, v in by_cat.items()},
    )
    return by_cat, stats
