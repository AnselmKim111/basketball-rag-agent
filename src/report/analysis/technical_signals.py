"""기술적 신호 탐지 — DataFrame(OHLCV) → 신호 dict.

각 자산의 가격 상태를 정량화: 등락률, MA 대비 위치, 52주 위치, 추세, 이격.
리포트 차트 캡션·LLM 입력으로 사용.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def analyze(df) -> dict:
    """단일 자산 DataFrame → 상태 dict. 데이터 부족이면 부분 채움."""
    if df is None or len(df) < 2:
        return {}
    import pandas as pd  # noqa
    close = df["Close"]
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    out = {
        "close": last,
        "chg_pct": (last / prev - 1) * 100 if prev else 0.0,
        "len": len(df),
    }
    for w, key in ((20, "ma20"), (50, "ma50"), (200, "ma200")):
        if len(close) >= w:
            ma = float(close.iloc[-w:].mean())
            out[key] = ma
            out[f"above_{key}"] = last > ma
            out[f"gap_{key}_pct"] = (last / ma - 1) * 100 if ma else 0.0
    # 52주 위치
    if len(close) >= 252:
        hi = float(df["High"].iloc[-252:].max())
        lo = float(df["Low"].iloc[-252:].min())
    else:
        hi = float(df["High"].max())
        lo = float(df["Low"].min())
    out["high_52w"] = hi
    out["low_52w"] = lo
    out["pct_of_52w_high"] = (last / hi * 100) if hi else 0.0
    out["is_new_high"] = last >= hi * 0.999
    # 단기 추세 (20일 기울기 방향)
    if len(close) >= 20:
        out["trend_up"] = float(close.iloc[-1]) > float(close.iloc[-20])
    return out


def detect_signals(df, label: str) -> list[dict]:
    """자산 1개에 대한 MarketSignal-like dict 리스트."""
    a = analyze(df)
    if not a:
        return []
    sigs: list[dict] = []

    def add(stype, strength, evidence, comment):
        sigs.append({"asset": label, "signal_type": stype, "strength": strength,
                     "evidence": evidence, "comment": comment})

    if a.get("is_new_high"):
        add("breakout", 5, ["new_52w_high"], f"{label} 52주 신고가 근접/돌파")
    if a.get("above_ma200") and a.get("above_ma50") and a.get("above_ma20"):
        add("uptrend", 4, ["above_20_50_200"], f"{label} 정배열(20·50·200선 위)")
    elif a.get("above_ma200") is False:
        add("risk_off", 3, ["below_ma200"], f"{label} 200일선 하회")
    g20 = a.get("gap_ma20_pct", 0)
    if g20 > 12:
        add("overextended", 3, [f"gap_ma20={g20:.0f}%"], f"{label} 20일선 대비 이격 부담")
    pct52 = a.get("pct_of_52w_high", 0)
    if 95 <= pct52 < 99.9 and not a.get("is_new_high"):
        add("near_breakout", 4, [f"{pct52:.0f}%_of_52w"], f"{label} 52주 고점 {pct52:.0f}% — 돌파 직전")
    return sigs
