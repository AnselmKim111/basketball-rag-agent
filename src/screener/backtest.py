"""신호 forward 수익률 백테스트 — "신호가 진짜 먹히는가"를 검증.

프로덕션 신호 함수(compute_signals_for_ticker)를 과거 임의 일자로 재호출해
k영업일 후 수익률을 집계한다. 시장 drift(baseline) 대비 edge로 신호의 실효성을 측정.

성능: 종목별 OHLCV를 1회 로드해 메모리에서 forward 수익률 계산(인접일 종가 슬라이스)
→ DB 쿼리는 종목당 1회. 신호 재계산은 compute_signals_for_ticker가 비용 대부분이므로
sample_every/lookback로 런타임 조절. executor에서 비동기 실행 권장.

생존 편향 주의: 보유 OHLCV(현재 활성 종목)만 — 상폐/신규상장 미반영 → edge는 보수적 하한.
"""
from __future__ import annotations

import logging
from statistics import mean, median

from src.screener import db, signals
from src.screener_common.sanity import KR_DAILY_HI, KR_DAILY_LO, series_is_continuous

log = logging.getLogger(__name__)

# 백테스트 대상 카테고리 (CATEGORIES 전체 — formatter 미표시 신호도 검증)
DEFAULT_HORIZONS = (5, 10, 20)


def backtest_signals(
    lookback: int = 120,
    sample_every: int = 3,
    horizons: tuple = DEFAULT_HORIZONS,
    categories: tuple | None = None,
    max_tickers: int | None = None,
) -> dict:
    """최근 lookback 거래일(sample_every 간격)에서 신호 재계산 → horizon별 forward 수익률.

    반환: {
      "lookback", "sample_every", "eval_dates", "horizons", "tickers",
      "baseline_n": {h: n},
      "categories": {cat: {h: {n, win, avg, median, baseline, edge}}},
    }  데이터 부족 시 빈 dict.
    """
    max_h = max(horizons)
    all_dates = db.recent_trading_dates(lookback + max_h + 5)
    if len(all_dates) < max_h + 20:
        log.warning("[backtest] 거래일 부족 (%d) — 스킵", len(all_dates))
        return {}
    # 마지막 max_h 거래일은 forward 데이터 없음 → 제외, 나머지를 sample_every 간격으로
    eval_pool = all_dates[:-max_h]
    eval_dates = eval_pool[::sample_every]
    eval_set = set(eval_dates)

    tickers = db.get_active_tickers()
    if max_tickers:
        tickers = tickers[:max_tickers]

    # 누산기
    baseline: dict[int, list] = {h: [] for h in horizons}
    per_cat: dict[str, dict[int, list]] = {}

    for tinfo in tickers:
        ticker = tinfo["ticker"]
        rows = db.load_ohlcv(ticker, days=1300)
        if len(rows) < 70:
            continue
        closes = [r.get("close") or 0 for r in rows]
        date_to_idx = {r["date"]: i for i, r in enumerate(rows)}
        # 이 종목이 평가 대상으로 보유한 일자만
        local_dates = [d for d in eval_dates if d in date_to_idx]
        for d in local_dates:
            idx = date_to_idx[d]
            fwd: dict[int, float] = {}
            for h in horizons:
                j = idx + h
                if j >= len(closes):
                    continue
                seg = closes[idx:j + 1]
                if not series_is_continuous(seg, KR_DAILY_LO, KR_DAILY_HI):
                    continue  # 분할/조정 글리치 표본 제외
                c0, cN = closes[idx], closes[j]
                if c0 <= 0 or cN <= 0:
                    continue
                fwd[h] = (cN - c0) / c0 * 100.0
            if not fwd:
                continue
            # baseline — 신호 유무 무관 전 cell (시장 drift)
            for h, r in fwd.items():
                baseline[h].append(r)
            # 신호 재계산
            try:
                sigs = signals.compute_signals_for_ticker(rows, base_date=d)
            except Exception:
                continue
            for cat in sigs:
                if categories and cat not in categories:
                    continue
                cm = per_cat.setdefault(cat, {h: [] for h in horizons})
                for h, r in fwd.items():
                    cm[h].append(r)

    out_cats: dict[str, dict] = {}
    for cat, hmap in per_cat.items():
        out_cats[cat] = {}
        for h in horizons:
            rets = hmap[h]
            if not rets:
                continue
            base = baseline[h]
            base_avg = round(mean(base), 2) if base else None
            avg = round(mean(rets), 2)
            out_cats[cat][h] = {
                "n": len(rets),
                "win": round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1),
                "avg": avg,
                "median": round(median(rets), 2),
                "baseline": base_avg,
                "edge": round(avg - base_avg, 2) if base_avg is not None else None,
            }

    result = {
        "lookback": lookback,
        "sample_every": sample_every,
        "eval_dates": len(eval_dates),
        "horizons": list(horizons),
        "tickers": len(tickers),
        "baseline_n": {h: len(baseline[h]) for h in horizons},
        "categories": out_cats,
    }
    log.info("[backtest] eval_dates=%d tickers=%d cats=%s",
             len(eval_dates), len(tickers), list(out_cats.keys()))
    return result


_CAT_LABEL = {
    "high_all": "역사적 신고가",
    "high_52w": "52주 신고가",
    "high_26w": "6개월 신고가",
    "vcp_breakout": "VCP 돌파",
    "volume_breakout": "거래량 돌파",
    "near_breakout_52w": "52주 돌파 직전",
    "ma_golden_cross": "골든크로스",
    "rsi_oversold_recovery": "RSI 회복",
    "base_hold_after_breakout": "돌파후 베이스홀드",
    "downtrend_exit": "하락추세 이탈",
}


def format_backtest(result: dict) -> str:
    """백테스트 결과 → 텔레그램 스코어카드 텍스트 (plain)."""
    if not result or not result.get("categories"):
        return "📊 백테스트: 집계할 신호 표본이 없습니다 (데이터/lookback 부족)."
    horizons = result["horizons"]
    lines = [
        "📊 신호 백테스트 (forward 수익률 vs 시장)",
        f"최근 {result['lookback']}거래일 · {result['eval_dates']}개 일자 샘플 · {result['tickers']}종목",
        f"baseline(시장평균) n={result['baseline_n']}",
        "각 항목: n · 승률 · 평균(edge=시장대비)",
        "",
    ]
    # 표시 순서: CATEGORIES 순
    order = [c for c in signals.CATEGORIES if c in result["categories"]]
    for cat in order:
        hmap = result["categories"][cat]
        lines.append(f"━ {_CAT_LABEL.get(cat, cat)}")
        for h in horizons:
            d = hmap.get(h)
            if not d:
                continue
            edge = d["edge"]
            edge_s = f"{edge:+.1f}%p" if edge is not None else "—"
            lines.append(
                f"  {h}일: n={d['n']} 승률{d['win']:.0f}% 평균{d['avg']:+.1f}% (edge {edge_s})"
            )
    lines.append("")
    lines.append("※ 생존편향(상폐 미반영) — edge는 보수적 하한.")
    return "\n".join(lines)
