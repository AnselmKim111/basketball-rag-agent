"""섹터 로테이션 / 시장 색깔 분류.

ETF·지수 신호를 종합해 market_color 판정:
  Crowding(쏠림 심화) / De-crowding(쏠림 둔화) / Rotation(순환) /
  Risk-on / Defensive / Broadening(가치 확산)

핵심 비교축:
  - RSP(동일가중) vs SPY(시총가중): RSP 강세 = 상승 종목 폭 확산 = De-crowding
  - IWM/Russell2000 강세: 소형주 매기 = Risk-on / Broadening
  - QQQ만 강세 + RSP 약세: 대형 기술주 쏠림 = Crowding
"""
from __future__ import annotations

import logging

from src.report.analysis.technical_signals import analyze

log = logging.getLogger(__name__)


def classify(etf_dfs: dict[str, object], breadth: dict | None = None) -> dict:
    """{label: DataFrame} (QQQ/SPY/RSP/IWM 등) → {market_color, evidence, detail}.

    breadth: {"pct_above_200ma": float, "advancers": int, "total": int} (선택).
    """
    states = {k: analyze(v) for k, v in etf_dfs.items() if v is not None}

    def chg(label):
        return states.get(label, {}).get("chg_pct", 0.0)

    def newhigh(label):
        return states.get(label, {}).get("is_new_high", False)

    qqq, spy, rsp, iwm = chg("QQQ"), chg("SPY"), chg("RSP"), chg("IWM")
    evidence = []
    color = "Mixed"

    rsp_strong = (rsp > spy + 0.2) or newhigh("RSP")
    iwm_strong = iwm > spy + 0.3
    qqq_lead = qqq > spy + 0.3 and not rsp_strong

    if rsp_strong and iwm_strong:
        color = "Broadening"
        evidence = ["RSP>SPY", "IWM 강세"]
    elif rsp_strong:
        color = "De-crowding"
        evidence = ["RSP(동일가중)>SPY(시총가중)"]
    elif qqq_lead:
        color = "Crowding"
        evidence = ["QQQ 단독 강세", "RSP 부진"]
    elif iwm_strong:
        color = "Risk-on"
        evidence = ["소형주(IWM) 강세"]
    elif qqq < 0 and spy < 0 and rsp < 0:
        color = "Risk-off"
        evidence = ["전반 약세"]
    else:
        color = "Rotation"
        evidence = ["지수 혼조 — 내부 순환 가능성"]

    # breadth 보강 (% > 200MA, 상승종목 비율)
    if breadth:
        pct200 = breadth.get("pct_above_200ma")
        adv, tot = breadth.get("advancers"), breadth.get("total")
        if isinstance(pct200, (int, float)):
            evidence.append(f"200일선 위 {pct200:.0f}%")
            if pct200 >= 60 and color in ("Rotation", "Mixed"):
                color = "Broadening"
        if isinstance(adv, int) and isinstance(tot, int) and tot:
            ratio = adv / tot * 100
            evidence.append(f"상승 {adv}/{tot}({ratio:.0f}%)")
            if ratio >= 60 and color == "Crowding":
                color = "De-crowding"

    detail = {k: round(states.get(k, {}).get("chg_pct", 0.0), 2)
              for k in ("QQQ", "SPY", "RSP", "IWM")}
    log.info("[rotation] color=%s evidence=%s detail=%s", color, evidence, detail)
    return {"market_color": color, "evidence": evidence, "detail": detail}
