"""기술적 신호 계산 (시장 비의존 순수 함수).

KR/US 스크리너 공통. 단일 종목 OHLCV(asc) → 발현 신호 dict.

신호:
  1. 52주 신고가 — 종가가 과거 252영업일(1년) 최고가 초과
  2. 역사적 신고가 — 종가가 보유 데이터 전체(최대 1400일) 최고가 초과
  3. 52주 돌파 직전 — 종가가 52주 고점 95-99% AND 5일 거래량 증가 추세
  4. VCP 돌파 — 변동성 수축 base 형성 후 박스권 상단 돌파

제거됨:
  - 거래량 돌파 ≥2배 (노이즈 다수 — 사용자 요청 제거)
  - 일목구름 상방 돌파 (노이즈 다수)
  - 20일/60일 신고가 (단기 노이즈)

모든 entry에 chg_pct (전일 대비 %) 포함.
시장별 `compute_all` (KR `src/screener/signals.py`, US `src/us_screener/signals.py`)이
이 모듈의 함수를 호출해 종목별로 loop.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# 임계값 — env 오버라이드 가능 (KR/US 공통 키)
DEFAULT_NEAR_BREAKOUT_LOWER = 0.95
DEFAULT_NEAR_BREAKOUT_UPPER = 0.99

CATEGORIES = [
    "high_all",
    "high_52w",
    "vcp_breakout",
    "near_breakout_52w",
]


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

    # 1) 52주 신고가 (252영업일) — **종가 기준** (종가 > 과거 252일 종가들의 max).
    #    과거 intraday 고가가 아니라 종가와 비교 — "종가 기준 신고가"가 사용자 기대.
    #    (과거 장중 스파이크가 오늘 종가보다 높아도 종가 신고가면 발화)
    if len(df) >= 252:
        past_close_high_52w = df["close"].iloc[-252:-1].max()
        if today["close"] > past_close_high_52w and past_close_high_52w > 0:
            out["high_52w"] = {
                "close": int(today["close"]),
                "prev_high": int(past_close_high_52w),
                "pct": float((today["close"] / past_close_high_52w - 1) * 100),
                "chg_pct": chg_pct,
            }

    # 2) 역사적 신고가 (보유 데이터 전체 — 최대 1400일) — **종가 기준**.
    past_close_high_all = df["close"].iloc[:-1].max()
    if today["close"] > past_close_high_all and past_close_high_all > 0:
        out["high_all"] = {
            "close": int(today["close"]),
            "prev_high": int(past_close_high_all),
            "pct": float((today["close"] / past_close_high_all - 1) * 100),
            "chg_pct": chg_pct,
            "history_days": int(len(df)),
        }

    # 3) 52주 돌파 직전 (95-99%)
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

    # 4) VCP 돌파 (최근 2주 이내 — 러프 버전)
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

            vcp_base_max = _get_float_env("SCREENER_VCP_BASE_MAX", 1.40)
            vcp_atr_max = _get_float_env("SCREENER_VCP_ATR_MAX", 1.00)

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


def composite_score(item: dict, max_cap: float) -> float:
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
