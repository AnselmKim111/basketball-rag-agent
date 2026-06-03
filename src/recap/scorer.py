"""신호 정확도·PnL 계산 — RecapBot Stage 2 (LLM 없음, 순수 산술).

입력: load_signals_in_range로 받은 signals 리스트 + ticker별 OHLCV.
출력: 신호 유형별 평균 수익률·win rate·top/bottom 등.

설계
----
- D+5 horizon 기본 (영업일 기준 가장 가까운 5개 거래일). hit_date의 close 대비
  D+5 close 변화. D+5가 데이터에 없으면 가장 최근 거래일까지 fill (graceful).
- 빈 입력·NaN 입력은 안전하게 처리 — 호출자 봇이 죽지 않게.

순수 함수만 — 호출자(aggregator)가 ohlcv를 책임지고 fetch.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass
class TickerHit:
    """signals 테이블 한 행 + 사후 가격 채워진 형태."""
    ticker: str
    name: str = ""             # screener.db tickers 매핑 시 채움
    signal: str = ""           # high_all / high_52w / volume_breakout / vcp_breakout / near_breakout_52w
    hit_date: str = ""         # YYYY-MM-DD
    hit_close: float = 0.0
    horizon_close: float = 0.0  # D+5 close (없으면 가장 최근)
    horizon_date: str = ""

    @property
    def pnl_pct(self) -> float | None:
        """수익률 % (hit_close 0 또는 horizon 없으면 None)."""
        if not self.hit_close or self.hit_close <= 0:
            return None
        if not self.horizon_close or self.horizon_close <= 0:
            return None
        return (self.horizon_close - self.hit_close) / self.hit_close * 100.0


@dataclass
class SignalStats:
    """한 신호 유형의 사후 통계."""
    signal: str
    hit_count: int = 0
    avg_pnl: float = 0.0
    win_rate: float = 0.0       # (+0%) 비율 — 0.0~1.0
    top: list[TickerHit] = field(default_factory=list)     # 상위 3
    bottom: list[TickerHit] = field(default_factory=list)  # 하위 3
    median_pnl: float = 0.0


def compute_signal_stats(hits: Iterable[TickerHit], top_n: int = 3) -> list[SignalStats]:
    """신호 유형별로 그룹핑 → 평균·win rate·top/bottom 계산.

    pnl_pct가 None인 hit는 통계 계산에서 제외(hit_count만 셈). 빈 그룹은
    SignalStats(hit_count=0, ...) 반환 — 호출자가 안내 메시지 분기.
    """
    grouped: dict[str, list[TickerHit]] = {}
    for h in hits:
        grouped.setdefault(h.signal, []).append(h)

    out: list[SignalStats] = []
    for sig, group in grouped.items():
        scored = [h for h in group if h.pnl_pct is not None]
        stats = SignalStats(signal=sig, hit_count=len(group))
        if not scored:
            out.append(stats)
            continue
        pnls = [h.pnl_pct for h in scored]
        stats.avg_pnl = sum(pnls) / len(pnls)
        stats.median_pnl = statistics.median(pnls)
        stats.win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        # top/bottom — pnl 기준
        scored_sorted = sorted(scored, key=lambda h: h.pnl_pct or 0, reverse=True)
        stats.top = scored_sorted[:top_n]
        stats.bottom = list(reversed(scored_sorted[-top_n:])) if len(scored_sorted) >= top_n else []
        out.append(stats)
    # signal 알파벳 순 — 텔레그램 메시지 안정성
    out.sort(key=lambda s: s.signal)
    return out


def overall_summary(stats: list[SignalStats]) -> dict:
    """모든 신호 유형 합쳐서 한 줄 요약 — sonnet 합성 인풋용.

    반환: {total_hits, scored_hits, overall_avg_pnl, overall_win_rate,
           best_signal, worst_signal}.
    빈 입력은 0으로.
    """
    total_hits = sum(s.hit_count for s in stats)
    scored = [(s.signal, s.avg_pnl, s.win_rate) for s in stats if s.hit_count > 0 and s.avg_pnl != 0.0]
    if not scored:
        return {
            "total_hits": total_hits,
            "scored_hits": 0,
            "overall_avg_pnl": 0.0,
            "overall_win_rate": 0.0,
            "best_signal": "",
            "worst_signal": "",
        }
    weighted_avg = sum(s.avg_pnl for s in stats if s.hit_count > 0) / max(1, len(scored))
    weighted_win = sum(s.win_rate for s in stats if s.hit_count > 0) / max(1, len(scored))
    best = max(scored, key=lambda x: x[1])
    worst = min(scored, key=lambda x: x[1])
    return {
        "total_hits": total_hits,
        "scored_hits": sum(1 for s in stats if s.hit_count > 0),
        "overall_avg_pnl": weighted_avg,
        "overall_win_rate": weighted_win,
        "best_signal": best[0],
        "worst_signal": worst[0],
    }
