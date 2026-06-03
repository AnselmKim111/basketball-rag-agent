"""한국 수급 멀티패널 차트 — 리포트 [9]의 핵심 설득 자료.

5단 구조 (사용자 명세):
  1단: 가격 + 10/20/50/200일 이동평균
  2단: 이격도 (종가/20MA - 1, %)
  3단: 기관 누적 순매수
  4단: 외국인 누적 순매수
  5단: 개인 누적 순매수
각 수급 패널에 방향 화살표(우상향=매수지속 / 우하향=매도지속) 자동 표시.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def flow_multipanel(
    price_df,
    flows_df,
    title: str,
    out_dir: Path,
    filename: str,
) -> str | None:
    """price_df: 규모별 지수 OHLCV. flows_df: 기관/외국인/개인 일별 순매수.

    flows_df 없으면 가격+이격도 2단만 그림 (graceful).
    """
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    if price_df is None or len(price_df) < 20:
        return None

    has_flows = flows_df is not None and len(flows_df) > 5
    investors = [c for c in ("기관", "외국인", "개인") if has_flows and c in flows_df.columns]
    n_panels = 2 + len(investors)

    fig, axes = plt.subplots(n_panels, 1, figsize=(12, 2.4 * n_panels), sharex=False)
    if n_panels == 1:
        axes = [axes]

    n = min(180, len(price_df))
    close = price_df["Close"]
    # 1단: 일봉 캔들 + MA
    ax = axes[0]
    theme.candlestick(ax, price_df, n=n)
    x = np.arange(n)
    for w, c in ((10, theme.COLOR_MA[0]), (20, theme.COLOR_MA[1]), (50, theme.COLOR_MA[2]), (200, theme.COLOR_MA[3])):
        if len(close) >= w:
            ma = close.rolling(w).mean().iloc[-n:].values
            ax.plot(x, ma, color=c, linewidth=0.9, alpha=0.85, label=f"{w}MA")
    last = float(close.iloc[-1]); prev = float(close.iloc[-2])
    chg = (last / prev - 1) * 100 if prev else 0
    ax.set_title(f"{title}  {last:,.1f} ({chg:+.2f}%)", fontsize=12)
    ax.legend(fontsize=7, loc="upper left", ncol=5)
    ax.tick_params(labelsize=7)
    theme.date_xticks(ax, price_df.index, n=n, count=6)

    # 2단: 이격도 (종가/20MA - 1)
    ax = axes[1]
    if len(price_df["Close"]) >= 20:
        ma20 = price_df["Close"].rolling(20).mean()
        disp = (price_df["Close"] / ma20 - 1) * 100
        disp = disp.iloc[-180:]
        ax.fill_between(disp.index, disp.values, 0, color="#999999", alpha=0.5)
        ax.axhline(0, color="#333", linewidth=0.6)
        cur = float(disp.iloc[-1])
        note = "이격부담" if cur > 8 else ("과매도" if cur < -8 else "이격부담 없음")
        ax.set_title(f"20일 이격도 {cur:+.1f}% — {note}", fontsize=10)
    ax.tick_params(labelsize=7)

    # 3~5단: 투자자 누적 순매수
    for i, inv in enumerate(investors):
        ax = axes[2 + i]
        series = flows_df[inv].iloc[-120:].cumsum()  # 누적
        color = theme.COLOR_UP if float(series.iloc[-1]) >= 0 else theme.COLOR_DOWN
        ax.plot(series.index, series.values, color=color, linewidth=1.4)
        ax.axhline(0, color="#333", linewidth=0.5)
        # 방향 화살표 (최근 20일 추세)
        if len(series) >= 20:
            recent_slope = float(series.iloc[-1]) - float(series.iloc[-20])
            arrow = "↗ 매수 지속" if recent_slope > 0 else "↘ 매도 지속"
        else:
            arrow = ""
        ax.set_title(f"{inv} 누적 순매수  {arrow}", fontsize=10, color=color)
        ax.tick_params(labelsize=7)

    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def streak_alert_card(kr_flows: dict, out_dir: Path,
                      filename: str = "31_kr_streak_alert.png",
                      min_streak: int = 5,
                      date_iso: str | None = None) -> str | None:
    """외국인/기관/개인 연속 매도/매수 streak ≥min_streak 시 카드 표시.

    kr_flows: {market: DataFrame with columns 외국인/기관/개인}
    카드: ⚠ 진하게 + 종목·일수·방향 + thesis 격상 가능 여부.
    """
    if not kr_flows:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    from src.report.state import streak_count

    alerts = []  # (market, investor, streak, sum_amount)
    for mkt, fdf in kr_flows.items():
        if fdf is None or len(fdf) < 5:
            continue
        for inv in ("외국인", "기관", "개인"):
            if inv not in fdf.columns:
                continue
            s = streak_count(fdf[inv])
            if abs(s) >= min_streak:
                amount = float(fdf[inv].iloc[-abs(s):].sum())
                alerts.append((mkt, inv, s, amount))
    if not alerts:
        return None

    # 정렬: 가장 큰 streak 먼저
    alerts.sort(key=lambda x: -abs(x[2]))
    n = len(alerts)
    fig, ax = plt.subplots(figsize=(13, max(2.0, 0.85 * n + 1.3)))
    ax.set_xlim(0, 10); ax.set_ylim(0, n + 0.5); ax.axis("off")

    for i, (mkt, inv, s, amt) in enumerate(alerts):
        y = n - i - 0.5
        direction = "매도" if s < 0 else "매수"
        is_critical = abs(s) >= 7
        bg = "#c62828" if is_critical else "#ef6c00"
        ax.add_patch(FancyBboxPatch((0.2, y - 0.36), 9.6, 0.72, boxstyle="round,pad=0.06",
                     facecolor=bg, edgecolor="white", linewidth=1.2, alpha=0.92))
        badge = "⚠ 격상" if is_critical else "관찰"
        text = (f"{badge}  ·  {mkt} {inv}  ·  {abs(int(s))}거래일 연속 {direction}  ·  "
                f"누적 {amt:+,.0f}억원")
        if is_critical:
            text += "  →  단순 차익실현 → 추세 전환 신호 격상 가능"
        ax.text(0.5, y, text, ha="left", va="center", fontsize=10.5,
                fontweight="bold", color="white")

    ax.set_title("[한국 수급 streak alert] 5거래일 이상 연속 매도/매수 종목",
                 fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
