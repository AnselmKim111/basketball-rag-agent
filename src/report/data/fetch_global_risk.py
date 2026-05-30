"""글로벌 자산 위험선호 매트릭스 데이터.

22개 자산 클래스 대표 ETF + Risk-On/Off 게이지 점수.
- 주식 글로벌(6): SPY QQQ EZU EWJ EEM EWY
- 채권(5): TLT IEF HYG LQD AGG
- 원자재(5): GLD SLV USO COPX DBA
- 통화(4): DXY FXY FXE FXC
- 암호(1): BTC-USD

Risk-On/Off 게이지 (0~100, 50=중립): 6개 시그널 정규화 평균.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

ASSET_GROUPS: dict[str, list[str]] = {
    "주식": ["SPY", "QQQ", "EZU", "EWJ", "EEM", "EWY"],
    "채권": ["TLT", "IEF", "HYG", "LQD", "AGG"],
    "원자재": ["GLD", "SLV", "USO", "COPX", "DBA"],
    "통화": ["DXY", "FXY", "FXE", "FXC"],
    "암호": ["BTC-USD"],
}

ASSET_LABELS: dict[str, str] = {
    "SPY": "미국 S&P", "QQQ": "미국 나스닥", "EZU": "유로존", "EWJ": "일본",
    "EEM": "신흥국", "EWY": "한국",
    "TLT": "미 장기채", "IEF": "미 중기채", "HYG": "하이일드",
    "LQD": "투자등급 채권", "AGG": "미 종합채",
    "GLD": "금", "SLV": "은", "USO": "WTI 유가", "COPX": "구리광산", "DBA": "농산물",
    "DXY": "달러 인덱스", "FXY": "엔", "FXE": "유로", "FXC": "캐나다달러",
    "BTC-USD": "비트코인",
}

# FDR/Yahoo 심볼 매핑 (특수 심볼 변환)
_YAHOO_SYMS = {"DXY": "DX-Y.NYB"}


def _yahoo(sym: str) -> str:
    return _YAHOO_SYMS.get(sym, sym)


def load_risk_map(days: int = 220):
    """(rows, gauge) 반환.
    rows: [{symbol, label, group, last, d1, d5}]
    gauge: {score: 0-100, label: "Risk-On"/"약 Risk-On"/"약 Risk-Off"/"Risk-Off", signals: dict}
    """
    from src.report.data.fetch_prices import fetch_many, day_change
    syms: list[str] = []
    for lst in ASSET_GROUPS.values():
        syms.extend(lst)
    fetch_dict = {s: _yahoo(s) for s in syms}
    dfs = fetch_many(fetch_dict, days=days, workers=10)

    rows: list[dict] = []
    for g, lst in ASSET_GROUPS.items():
        for s in lst:
            df = dfs.get(s)
            if df is None or len(df) < 6:
                log.warning("[risk] %s 가격 미확보", s)
                continue
            close = df["Close"]
            try:
                last = float(close.iloc[-1])
                d1 = day_change(df)
                d5 = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else None
            except Exception:
                continue
            rows.append({"symbol": s, "label": ASSET_LABELS.get(s, s),
                         "group": g, "last": last, "d1": d1, "d5": d5})

    gauge = _risk_gauge(dfs)
    log.info("[risk] %d/%d ETF·게이지 %d(%s)",
             len(rows), len(syms), gauge["score"], gauge["label"])
    return rows, gauge


def _risk_gauge(dfs: dict) -> dict:
    """6개 위험선호 시그널(각 1/0) 평균 → 0-100 점수.
    1: SPY > 200MA, 2: HYG/LQD 5D↑, 3: GLD/SPY 5D↓, 4: DXY 5D↓, 5: EEM 5D↑, 6: BTC 5D↑.
    """
    signals: dict[str, int] = {}
    score = 0; n = 0

    def _ret5(sym: str):
        df = dfs.get(sym)
        if df is None or len(df) < 6:
            return None
        try:
            return float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-6]) - 1
        except Exception:
            return None

    # 1) SPY > 200MA
    spy = dfs.get("SPY")
    if spy is not None and len(spy) >= 200:
        try:
            ma200 = float(spy["Close"].rolling(200).mean().iloc[-1])
            s = 1 if float(spy["Close"].iloc[-1]) > ma200 else 0
            signals["SPY>200MA"] = s; score += s; n += 1
        except Exception:
            pass

    # 2) HYG 강세 = HYG/LQD 5일 상승 (하이일드 vs IG)
    hyg = dfs.get("HYG"); lqd = dfs.get("LQD")
    if hyg is not None and lqd is not None and len(hyg) >= 6 and len(lqd) >= 6:
        try:
            r_now = float(hyg["Close"].iloc[-1]) / float(lqd["Close"].iloc[-1])
            r_5d = float(hyg["Close"].iloc[-6]) / float(lqd["Close"].iloc[-6])
            s = 1 if r_now > r_5d else 0
            signals["HY 강세(HYG/LQD↑)"] = s; score += s; n += 1
        except Exception:
            pass

    # 3) GLD/SPY 5일 ↓ = 금 약세 → Risk-on
    gld = dfs.get("GLD")
    if gld is not None and spy is not None and len(gld) >= 6 and len(spy) >= 6:
        try:
            r_now = float(gld["Close"].iloc[-1]) / float(spy["Close"].iloc[-1])
            r_5d = float(gld["Close"].iloc[-6]) / float(spy["Close"].iloc[-6])
            s = 1 if r_now < r_5d else 0
            signals["금/주식 약세(GLD/SPY↓)"] = s; score += s; n += 1
        except Exception:
            pass

    # 4) DXY 5일 약세 = 달러 약세 → Risk-on
    d5_dxy = _ret5("DXY")
    if d5_dxy is not None:
        s = 1 if d5_dxy < 0 else 0
        signals["달러 약세"] = s; score += s; n += 1

    # 5) EEM 5일 상승 = 신흥국 강세
    d5_eem = _ret5("EEM")
    if d5_eem is not None:
        s = 1 if d5_eem > 0 else 0
        signals["EM 강세"] = s; score += s; n += 1

    # 6) BTC 5일 상승 = 위험자산 선호
    d5_btc = _ret5("BTC-USD")
    if d5_btc is not None:
        s = 1 if d5_btc > 0 else 0
        signals["BTC 강세"] = s; score += s; n += 1

    norm = int(round(score / n * 100)) if n else 50
    if norm >= 75:
        label = "Risk-On"
    elif norm >= 55:
        label = "약 Risk-On"
    elif norm >= 35:
        label = "약 Risk-Off"
    else:
        label = "Risk-Off"
    return {"score": norm, "label": label, "signals": signals}
