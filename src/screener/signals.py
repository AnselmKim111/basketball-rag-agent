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
# 0.95→0.90 확장 (Phase 2) — 백테스트 edge 최고 신호(+2.3%p@10일)를 넓게 포착.
# 확장 구간(0.90~0.95)은 compute_all에서 RS rank 게이트로 노이즈 차단.
DEFAULT_NEAR_BREAKOUT_LOWER = 0.90
DEFAULT_NEAR_BREAKOUT_UPPER = 0.99
# 확장 구간 RS 게이트 / RS 리더 선정 임계 (백분위)
DEFAULT_RS_GATE_PCT = 60.0
DEFAULT_RS_LEADER_PCT = 90.0
DEFAULT_RS_LEADER_MAX = 10
# 수급 유입(volume_surge) 표시 게이트 — 원신호 volume_breakout(2×)은 저장용으로 유지,
# 표시는 강한 것만 (거래량 3× + 종가 +3% + RS 상위 50%)
DEFAULT_VOL_SURGE_RATIO = 3.0
DEFAULT_VOL_SURGE_CHG = 3.0
DEFAULT_VOL_SURGE_RS = 50.0
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

    # 1) 52주 신고가 (252영업일) — 종가기준 (오늘 종가 > 과거 252일 종가 최고)
    if len(df) >= 252:
        past_close_high_52w = df["close"].iloc[-252:-1].max()
        if today["close"] > past_close_high_52w and past_close_high_52w > 0:
            out["high_52w"] = {
                "close": int(today["close"]),
                "prev_high": int(past_close_high_52w),
                "pct": float((today["close"] / past_close_high_52w - 1) * 100),
                "chg_pct": chg_pct,
            }

    # 1.5) 6개월(126거래일) 신고가 — 종가기준. 장기 하락 후 회복 국면 조기 포착
    # (52주 고점에선 멀지만 6개월 박스권을 뚫는 GS건설류). 52주 신고가와 독립 발화 —
    # 중복 표시는 formatter dedup이 처리, 백테스트는 원신호 그대로 측정.
    if len(df) >= 127:
        past_close_high_26w = df["close"].iloc[-127:-1].max()
        if today["close"] > past_close_high_26w and past_close_high_26w > 0:
            out["high_26w"] = {
                "close": int(today["close"]),
                "prev_high": int(past_close_high_26w),
                "pct": float((today["close"] / past_close_high_26w - 1) * 100),
                "chg_pct": chg_pct,
            }

    # 2) 역사적 신고가 (보유 데이터 전체) — 종가기준 (오늘 종가 > 과거 모든 종가)
    past_close_high_all = df["close"].iloc[:-1].max()
    if today["close"] > past_close_high_all and past_close_high_all > 0:
        out["high_all"] = {
            "close": int(today["close"]),
            "prev_high": int(past_close_high_all),
            "pct": float((today["close"] / past_close_high_all - 1) * 100),
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

    # 5) VCP 돌파 (최근 1주 이내 — 러프 버전)
    # "오늘 돌파"만 잡던 4중 AND를 완화: 변동성 수축 base 형성 후 최근 N영업일
    # (SCREENER_VCP_WINDOW, 기본 5=1주) 중 박스권 상단을 돌파한 종목.
    #   (a) base: 돌파 window 직전 50일 박스권 (high/low ≤ SCREENER_VCP_BASE_MAX, 1.25)
    #   (b) 변동성 수축: base 후반 20일 ATR ≤ 전반 30일 ATR × SCREENER_VCP_ATR_MAX (0.75)
    #   (c) 돌파: 최근 window일 중 종가가 base 박스권 high 초과한 날 존재 (거래량 조건은 러프하게 생략)
    vcp_window = int(_get_float_env("SCREENER_VCP_WINDOW", 5))
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

    # ------------------------------------------------------------------
    # 6~9) Trend reversal 4종 — 추세가 돌기 시작한 종목 포착 (버터대디봇 watchlist용)
    # ------------------------------------------------------------------
    try:
        close = df["close"].astype(float)

        # 6) ma_golden_cross — MA20 × MA50 cross-up, 최근 5일 이내
        if len(df) >= 51:
            ma20 = close.rolling(20).mean()
            ma50 = close.rolling(50).mean()
            for back in range(0, 5):
                idx = -1 - back
                if (
                    pd.notna(ma20.iloc[idx - 1]) and pd.notna(ma50.iloc[idx - 1])
                    and pd.notna(ma20.iloc[idx]) and pd.notna(ma50.iloc[idx])
                    and ma20.iloc[idx - 1] <= ma50.iloc[idx - 1]
                    and ma20.iloc[idx] > ma50.iloc[idx]
                ):
                    out["ma_golden_cross"] = {
                        "close": int(today["close"]),
                        "days_ago": int(back),
                        "ma20": float(ma20.iloc[-1]),
                        "ma50": float(ma50.iloc[-1]),
                        "chg_pct": chg_pct,
                    }
                    break

        # 7) rsi_oversold_recovery — RSI14 <30 → 45~60 회복, 최근 7일 이내
        if len(df) >= 21:
            delta = close.diff()
            gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
            rs = gain / loss.replace(0, float("nan"))
            rsi = 100 - (100 / (1 + rs))
            cur_rsi = rsi.iloc[-1]
            if pd.notna(cur_rsi) and 45.0 <= float(cur_rsi) <= 60.0:
                past = rsi.iloc[-15:-1]
                if (past < 30).any():
                    out["rsi_oversold_recovery"] = {
                        "close": int(today["close"]),
                        "rsi": float(cur_rsi),
                        "min_rsi_recent": float(past.min()),
                        "chg_pct": chg_pct,
                    }

        # 8) base_hold_after_breakout — 52w 돌파 후 5~15일 base hold (pullback ≤3%)
        if len(df) >= 252:
            past_for_52w = df["high"].iloc[-252:-15]
            if len(past_for_52w) > 0:
                hi252_pre = float(past_for_52w.max())
                bo_window = df.iloc[-20:-5]
                bo_days = bo_window[bo_window["close"] > hi252_pre]
                if len(bo_days) > 0 and hi252_pre > 0:
                    recent_hi = float(df["high"].iloc[-5:].max())
                    if recent_hi >= hi252_pre * 0.97 and today["close"] >= hi252_pre * 0.95:
                        days_since = int(len(df) - 1 - bo_days.index[-1])
                        out["base_hold_after_breakout"] = {
                            "close": int(today["close"]),
                            "breakout_high": int(hi252_pre),
                            "days_since_breakout": days_since,
                            "chg_pct": chg_pct,
                        }

        # 9) downtrend_exit — 3M r3m <-5% + MA50 상향 돌파 (최근 5일)
        if len(df) >= 63:
            r3m = float((close.iloc[-1] / close.iloc[-63] - 1) * 100)
            ma50_series = close.rolling(50).mean()
            cur_ma50 = ma50_series.iloc[-1]
            if pd.notna(cur_ma50) and r3m < -5.0 and close.iloc[-1] > cur_ma50:
                for back in range(1, 6):
                    idx = -1 - back
                    prev_close = close.iloc[idx]
                    prev_ma = ma50_series.iloc[idx]
                    if pd.notna(prev_ma) and prev_close <= prev_ma:
                        out["downtrend_exit"] = {
                            "close": int(today["close"]),
                            "r3m_pct": r3m,
                            "ma50": float(cur_ma50),
                            "days_ago": int(back),
                            "chg_pct": chg_pct,
                        }
                        break
    except Exception:
        log.exception("trend_reversal 신호 계산 실패")

    return out


CATEGORIES = [
    "high_all",
    "high_52w",
    "high_26w",
    "vcp_breakout",
    "volume_breakout",
    "volume_surge",
    "near_breakout_52w",
    # 신규 trend_reversal 4종 — 버터대디봇 watchlist용
    "ma_golden_cross",
    "rsi_oversold_recovery",
    "base_hold_after_breakout",
    "downtrend_exit",
    # 유니버스 상대강도 리더 (compute_all에서 cross-sectional 산출 — per-ticker 아님)
    "rs_leaders",
]


def _composite_score(item: dict, max_cap: float) -> float:
    """시총 + 상승률 복합 점수.

    score = 0.5 * normalized_chg_pct + 0.5 * normalized_log_cap
      - normalized_chg_pct: 0 ~ 30% 기준 0~1 (clip)
      - normalized_log_cap: log10(cap) / log10(max_cap)
      - normalized_rs: rs_3m_rank/100 (유니버스 상대강도, 없으면 0.5 중립)
    score = 0.4·chg + 0.3·cap + 0.3·rs. desc 정렬용 → 음수 반환.
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
    # RS 항 (Phase 2) — 시장을 이기는 종목 우선. rank 없으면 중립 0.5.
    rs = item.get("rs_3m_rank")
    norm_rs = (rs / 100.0) if isinstance(rs, (int, float)) else 0.5
    score = 0.4 * norm_chg + 0.3 * norm_cap + 0.3 * norm_rs
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
    skipped_short = 0
    no_base_tickers: list[str] = []
    short_tickers: list[str] = []
    max_cap_seen = 0
    rs_meta: dict[str, dict] = {}  # ticker → 상대강도 원료 (base_date 기준)
    from src.screener.breadth import BreadthAccumulator
    breadth_acc = BreadthAccumulator()
    for tinfo in tickers:
        ticker = tinfo["ticker"]
        cap = tinfo.get("market_cap")
        if cap is not None and cap < min_cap:
            skipped_cap += 1
            continue
        rows = db.load_ohlcv(ticker, days=1300)
        # 시장 폭 누산 (이중 I/O 없이 — 이미 로드된 rows). <60행도 자체 길이 가드 있음.
        breadth_acc.add(rows)
        if len(rows) < 60:
            skipped_short += 1
            if len(short_tickers) < 30:
                short_tickers.append(f"{ticker}({len(rows)})")
            continue
        # base_date row 보유 여부 검증
        base_idx = next((i for i, r in enumerate(rows) if r["date"] == base_date), None)
        if base_idx is None:
            skipped_no_base += 1
            if len(no_base_tickers) < 30:
                no_base_tickers.append(f"{ticker}({tinfo.get('name') or '?'})")
            continue
        try:
            sigs = compute_signals_for_ticker(rows, base_date=base_date)
        except Exception:
            log.exception("[signals] %s 계산 실패", ticker)
            continue
        # 유니버스 상대강도(RS) 원료 — base_date 기준 truncate된 종가로 계산
        closes = [r.get("close") or 0 for r in rows[: base_idx + 1]]
        if len(closes) >= 64 and closes[-64] > 0 and closes[-1] > 0:
            cur = closes[-1]
            meta = {
                "r3m_pct": (cur / closes[-64] - 1) * 100,
                "r1m_pct": (cur / closes[-22] - 1) * 100 if closes[-22] > 0 else None,
                "above_ma50": len(closes) >= 50 and cur > sum(closes[-50:]) / 50,
                "chg_pct": (cur / closes[-2] - 1) * 100 if closes[-2] > 0 else 0.0,
                "close": cur,
            }
            if len(closes) >= 252:
                past_close_hi = max(closes[-252:-1])
                meta["near_high_15"] = past_close_hi > 0 and cur >= past_close_hi * 0.85
            else:
                meta["near_high_15"] = False
            meta.update({"name": tinfo.get("name") or ticker,
                         "market": tinfo.get("market") or "",
                         "market_cap": cap, "sector": tinfo.get("sector") or ""})
            rs_meta[ticker] = meta
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

    # ------------------------------------------------------------------
    # 유니버스 상대강도(RS) — 3M 수익률 백분위 rank (0~100)
    # ------------------------------------------------------------------
    rs_rank: dict[str, float] = {}
    ranked = sorted(rs_meta.items(), key=lambda kv: kv[1]["r3m_pct"])
    n_rank = len(ranked)
    if n_rank >= 2:
        for i, (t, _) in enumerate(ranked):
            rs_rank[t] = round(i / (n_rank - 1) * 100, 1)

    # 모든 신호 entry에 rs_3m_rank 부여
    for items in by_cat.values():
        for it in items:
            it["rs_3m_rank"] = rs_rank.get(it["ticker"])

    # near_breakout 확장 구간(proximity < 95%) — RS 게이트로 노이즈 차단.
    # 코어 구간(95~99%)은 백테스트 검증된 그대로 무조건 유지.
    rs_gate = _get_float_env("SCREENER_RS_GATE_PCT", DEFAULT_RS_GATE_PCT)
    nb = by_cat.get("near_breakout_52w") or []
    if nb:
        kept = [it for it in nb
                if (it.get("proximity_pct") or 0) >= 95.0
                or (it.get("rs_3m_rank") or 0) >= rs_gate]
        gated = len(nb) - len(kept)
        if gated:
            log.info("[signals] near_breakout 확장구간 RS 게이트: %d종목 제외", gated)
        by_cat["near_breakout_52w"] = kept

    # 수급 유입(volume_surge) — volume_breakout 중 강한 것만 표시용 복제
    # (원신호는 저장·버터대디용 유지). 게이트: 거래량 3× + 종가 +3% + RS 상위 50%.
    vs_ratio = _get_float_env("SCREENER_VOL_SURGE_RATIO", DEFAULT_VOL_SURGE_RATIO)
    vs_chg = _get_float_env("SCREENER_VOL_SURGE_CHG", DEFAULT_VOL_SURGE_CHG)
    vs_rs = _get_float_env("SCREENER_VOL_SURGE_RS", DEFAULT_VOL_SURGE_RS)
    by_cat["volume_surge"] = [
        dict(it) for it in (by_cat.get("volume_breakout") or [])
        if (it.get("vol_ratio") or 0) >= vs_ratio
        and (it.get("chg_pct") or 0) >= vs_chg
        and (it.get("rs_3m_rank") or 0) >= vs_rs
    ]

    # RS 리더 — 시장 대비 상위 + 기술적 양호 (약세장 리더십 watchlist)
    leader_pct = _get_float_env("SCREENER_RS_LEADER_PCT", DEFAULT_RS_LEADER_PCT)
    leader_max = int(_get_float_env("SCREENER_RS_LEADER_MAX", DEFAULT_RS_LEADER_MAX))
    leaders = []
    for t, m in rs_meta.items():
        rank = rs_rank.get(t)
        if rank is None or rank < leader_pct:
            continue
        if not m.get("above_ma50") or not m.get("near_high_15"):
            continue
        leaders.append({
            "ticker": t, "name": m["name"], "market": m["market"],
            "market_cap": m["market_cap"], "sector": m["sector"],
            "close": int(m["close"]), "chg_pct": round(m["chg_pct"], 2),
            "rs_3m_rank": rank, "r3m_pct": round(m["r3m_pct"], 1),
        })
    leaders.sort(key=lambda x: -x["rs_3m_rank"])
    by_cat["rs_leaders"] = leaders[:leader_max]

    # 시총+상승률+RS 복합 정렬 (rs_leaders는 이미 RS rank desc — 유지)
    for cat, items in by_cat.items():
        if cat == "rs_leaders":
            continue
        items.sort(key=lambda it: _composite_score(it, max_cap_seen))

    stats = {
        "base_date": base_date,
        "processed": processed,
        "skipped_cap": skipped_cap,
        "skipped_no_base": skipped_no_base,
        "skipped_short": skipped_short,
        "total_active": len(tickers),
        "breadth": breadth_acc.result(),
    }
    log.info(
        "[signals] base_date=%s processed=%d skipped_cap=%d skipped_no_base=%d "
        "skipped_short=%d (min=%.1f억) categories=%s",
        base_date, processed, skipped_cap, skipped_no_base, skipped_short, min_cap / 1e8,
        {k: len(v) for k, v in by_cat.items()},
    )
    if no_base_tickers:
        log.warning("[signals] base_date 누락 종목: %s", ", ".join(no_base_tickers))
    if short_tickers:
        log.warning("[signals] <60행 skip 종목: %s", ", ".join(short_tickers))
    return by_cat, stats
