"""테마/섹터 모멘텀 계산 + 로테이션 버킷 분류.

히트맵·바차트·자금흐름 다이어그램·LLM 공통 입력을 생성한다.
각 테마 ETF DataFrame → 1D/5D/1M/3M return, MA위치, 52주위치, 신고가/정배열 →
버킷(주도지속 / 새로 강해지는 / 소외·빈집) 분류.
"""
from __future__ import annotations

import logging

from src.report.analysis.technical_signals import analyze
from src.report.data.fetch_prices import pct_return

log = logging.getLogger(__name__)


def compute(theme_dfs: dict[str, object]) -> list[dict]:
    """{label: DataFrame} → [{label, r1d, r5d, r1m, r3m, pct_52w, new_high, aligned, bucket}]."""
    rows: list[dict] = []
    for label, df in theme_dfs.items():
        if df is None or len(df) < 5:
            continue
        a = analyze(df)
        row = {
            "label": label,
            "r1d": pct_return(df, 1),
            "r5d": pct_return(df, 5),
            "r1m": pct_return(df, 21),
            "r3m": pct_return(df, 63),
            "pct_52w": round(a.get("pct_of_52w_high", 0.0), 1),
            "new_high": bool(a.get("is_new_high")),
            "aligned": bool(a.get("above_ma20") and a.get("above_ma50") and a.get("above_ma200")),
            "above_ma200": bool(a.get("above_ma200")),
        }
        row["bucket"] = _bucket(row)
        rows.append(row)
    rows.sort(key=lambda r: (r.get("r1m") if r.get("r1m") is not None else -999), reverse=True)
    return rows


def _bucket(row: dict) -> str:
    """주도지속 / 새로 강해지는 / 숨고르기 / 소외·빈집."""
    r1m = row.get("r1m") or 0.0
    r5d = row.get("r5d") or 0.0
    r3m = row.get("r3m") or 0.0
    new_high = row.get("new_high")
    above200 = row.get("above_ma200")

    if not above200 and r3m < 0:
        return "소외·빈집"
    if new_high and r1m > 0 and r3m > 8:
        return "주도지속"
    if r5d > 1.5 and r1m > 0 and (r3m <= 8 or not new_high):
        return "새로 강해지는"
    if r3m > 5 and r5d < 0:
        return "숨고르기"
    if r1m < -3:
        return "소외·빈집"
    return "중립"


def flow_endpoints(rows: list[dict], n: int = 6) -> tuple[list[tuple], list[tuple]]:
    """자금흐름 다이어그램용 (이탈처, 유입처) 생성.

    이탈처 = 숨고르기/주도지속 중 단기 약세(r5d<0) 강한 것.
    유입처 = 새로 강해지는/주도지속 중 r5d 강한 것.
    반환: ([(label, strength)], [(label, strength)])
    """
    outflow = sorted(
        [r for r in rows if (r.get("r5d") or 0) < 0],
        key=lambda r: (r.get("r5d") or 0))[:n]
    inflow = sorted(
        [r for r in rows if (r.get("r5d") or 0) > 0],
        key=lambda r: -(r.get("r5d") or 0))[:n]
    src = [(r["label"], abs(r.get("r5d") or 1.0)) for r in outflow]
    dst = [(r["label"], abs(r.get("r5d") or 1.0)) for r in inflow]
    return src, dst


def summarize(rows: list[dict]) -> dict:
    """LLM/스냅샷용 요약: 버킷별 라벨 + hot/cold."""
    buckets: dict[str, list[str]] = {}
    for r in rows:
        buckets.setdefault(r["bucket"], []).append(r["label"])
    hot = [r["label"] for r in rows[:5]]
    cold = [r["label"] for r in sorted(rows, key=lambda r: (r.get("r1m") or 0))[:5]]
    return {"buckets": buckets, "hot": hot, "cold": cold}
