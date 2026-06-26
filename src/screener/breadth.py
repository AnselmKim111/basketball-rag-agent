"""시장 폭(breadth) 지표 — "신고가 부재 = 시장 탓 vs 코드 탓" 판별.

신고가 0인데 breadth가 약세(MA200 위 소수·하락 우위)면 정상(시장), 코드 이상 아님.
반대로 breadth가 강한데 신고가 0이면 스크리너 의심 → 조사 신호.

BreadthAccumulator는 compute_all 루프에 끼워 이미 로드된 rows를 먹임(이중 I/O 0).
compute_breadth()는 /breadth 명령용 독립 실행.
"""
from __future__ import annotations

import logging
from statistics import median

log = logging.getLogger(__name__)


def _ma(closes: list, n: int) -> float | None:
    if len(closes) < n:
        return None
    seg = closes[-n:]
    return sum(seg) / n


class BreadthAccumulator:
    """종목별 close 시계열을 add → 시장 폭 집계."""

    def __init__(self) -> None:
        self.t_ma200 = self.above_ma200 = 0
        self.t_ma50 = self.above_ma50 = 0
        self.t_ma20 = self.above_ma20 = 0
        self.t_52w = self.at_high = self.within5 = self.within10 = 0
        self.t_adv = self.adv = self.dec = 0
        self.r1m: list[float] = []
        self.r3m: list[float] = []

    def add(self, rows: list[dict]) -> None:
        closes = [r.get("close") or 0 for r in rows if (r.get("close") or 0) > 0]
        if len(closes) < 2:
            return
        cur = closes[-1]

        m200 = _ma(closes, 200)
        if m200:
            self.t_ma200 += 1
            if cur > m200:
                self.above_ma200 += 1
        m50 = _ma(closes, 50)
        if m50:
            self.t_ma50 += 1
            if cur > m50:
                self.above_ma50 += 1
        m20 = _ma(closes, 20)
        if m20:
            self.t_ma20 += 1
            if cur > m20:
                self.above_ma20 += 1

        # 52주 종가 신고가 / 근접 (과거 252일 종가 최고 기준)
        if len(closes) >= 252:
            past_high = max(closes[-252:-1])
            if past_high > 0:
                self.t_52w += 1
                if cur > past_high:
                    self.at_high += 1
                if cur >= past_high * 0.95:
                    self.within5 += 1
                if cur >= past_high * 0.90:
                    self.within10 += 1

        # advance/decline
        prev = closes[-2]
        if prev > 0:
            self.t_adv += 1
            if cur > prev:
                self.adv += 1
            elif cur < prev:
                self.dec += 1

        # 1M(21d)/3M(63d) 수익률
        if len(closes) >= 22 and closes[-22] > 0:
            self.r1m.append((cur / closes[-22] - 1) * 100)
        if len(closes) >= 64 and closes[-64] > 0:
            self.r3m.append((cur / closes[-64] - 1) * 100)

    def result(self) -> dict:
        def pct(a, b):
            return round(a / b * 100, 1) if b else None
        return {
            "universe": self.t_ma20,
            "above_ma200_pct": pct(self.above_ma200, self.t_ma200),
            "above_ma50_pct": pct(self.above_ma50, self.t_ma50),
            "above_ma20_pct": pct(self.above_ma20, self.t_ma20),
            "at_high_n": self.at_high,
            "at_high_pct": pct(self.at_high, self.t_52w),
            "within5_n": self.within5,
            "within10_n": self.within10,
            "advancers": self.adv,
            "decliners": self.dec,
            "adv_pct": pct(self.adv, self.t_adv),
            "median_1m": round(median(self.r1m), 1) if self.r1m else None,
            "median_3m": round(median(self.r3m), 1) if self.r3m else None,
        }


def compute_breadth() -> dict:
    """전 활성 종목 로드 → breadth (독립 실행, /breadth 명령용)."""
    from src.screener import db
    acc = BreadthAccumulator()
    for tinfo in db.get_active_tickers():
        rows = db.load_ohlcv(tinfo["ticker"], days=300)
        if rows:
            acc.add(rows)
    return acc.result()


def format_breadth_line(b: dict) -> str:
    """일일 메시지 헤더용 한 줄 (HTML 안전 — 특수문자 없음)."""
    if not b or not b.get("universe"):
        return ""
    parts = []
    if b.get("above_ma200_pct") is not None:
        parts.append(f"MA200위 {b['above_ma200_pct']:.0f}%")
    parts.append(f"52주신고 {b.get('at_high_n', 0)}종목")
    if b.get("adv_pct") is not None:
        parts.append(f"상승 {b['adv_pct']:.0f}%")
    if b.get("median_3m") is not None:
        parts.append(f"中3M {b['median_3m']:+.0f}%")
    return "🌡️ 시장 폭: " + " · ".join(parts)


def format_breadth_full(b: dict) -> str:
    """/breadth 명령용 상세."""
    if not b or not b.get("universe"):
        return "🌡️ 시장 폭: 데이터 부족."
    return "\n".join([
        f"🌡️ 시장 폭 (유니버스 {b['universe']}종목)",
        f"  MA200 위: {b.get('above_ma200_pct')}%  /  MA50 위: {b.get('above_ma50_pct')}%  /  MA20 위: {b.get('above_ma20_pct')}%",
        f"  52주 종가신고가: {b.get('at_high_n')}종목 ({b.get('at_high_pct')}%)",
        f"  52주고점 5% 이내: {b.get('within5_n')}종목 / 10% 이내: {b.get('within10_n')}종목",
        f"  상승 {b.get('advancers')} vs 하락 {b.get('decliners')} (상승 {b.get('adv_pct')}%)",
        f"  유니버스 中 1M: {b.get('median_1m')}% / 3M: {b.get('median_3m')}%",
    ])
