"""§2 쏠림 둔화 시그널 — 리포트의 메인 시각 자산.

- deconcentration_signal: SPY/RSP/QQQ/MAGS normalized + RSP 신고가 마커
- rsp_spy_ratio: 동일가중/시총가중 ratio (>변곡 = 폭 확산 = 쏠림 둔화)
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def deconcentration_signal(dfs: dict[str, object], out_dir: Path,
                           filename: str = "05_deconcentration.png",
                           days: int = 252, date_iso: str | None = None) -> str | None:
    """dfs: {SPY,RSP,QQQ,MAGS: DataFrame} normalized(100) 비교 + RSP 신고가 annotation."""
    from src.report.data.fetch_prices import normalize_100
    from src.report.analysis.technical_signals import analyze
    theme.setup()
    import matplotlib.pyplot as plt
    avail = {k: v for k, v in dfs.items() if v is not None and len(v) > 5}
    if "RSP" not in avail and "SPY" not in avail:
        return None
    fig, ax = plt.subplots(figsize=(12, 6))
    style = {"SPY": ("#1f77b4", 1.4), "RSP": ("#d62728", 2.2),
             "QQQ": ("#2ca02c", 1.2), "MAGS": ("#9467bd", 1.2)}
    for label, df in avail.items():
        s = normalize_100(df.iloc[-days:])
        if s is None:
            continue
        col, lw = style.get(label, ("#888", 1.0))
        last = float(s.iloc[-1])
        ax.plot(s.index, s.values, color=col, linewidth=lw, label=f"{label} ({last - 100:+.1f}%)")
    # RSP 신고가 마커
    rsp = avail.get("RSP")
    if rsp is not None:
        a = analyze(rsp)
        if a.get("is_new_high"):
            s = normalize_100(rsp.iloc[-days:])
            ax.scatter([s.index[-1]], [float(s.iloc[-1])], color="#d62728", s=80, zorder=5,
                       marker="*")
            ax.annotate("RSP 신고가 — 쏠림 둔화", xy=(s.index[-1], float(s.iloc[-1])),
                        xytext=(-150, 14), textcoords="offset points", fontsize=11,
                        color="#d62728", fontweight="bold",
                        arrowprops=dict(arrowstyle="->", color="#d62728"))
    ax.axhline(100, color="#999", linewidth=0.6, linestyle=":")
    ax.set_title("쏠림 둔화 시그널 — 동일가중(RSP) vs 시총가중(SPY/QQQ/M7) [100 리베이스]", fontsize=12)
    ax.legend(fontsize=9, loc="upper left")
    ax.tick_params(labelsize=8)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def rsp_spy_ratio(rsp_df, spy_df, out_dir: Path, filename: str = "06_rsp_spy_ratio.png",
                  days: int = 252, date_iso: str | None = None) -> str | None:
    """RSP/SPY ratio 차트. 50MA 상향/하향 cross 마커 + outperform streak fact."""
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    if rsp_df is None or spy_df is None or len(rsp_df) < 30 or len(spy_df) < 30:
        return None
    ratio = (rsp_df["Close"] / spy_df["Close"]).dropna().iloc[-days:]
    if len(ratio) < 10:
        return None
    ma50 = ratio.rolling(50).mean()
    fig, ax = plt.subplots(figsize=(12, 4.8))
    ax.plot(ratio.index, ratio.values, color="#d62728", linewidth=1.6, label="RSP/SPY")
    ax.plot(ma50.index, ma50.values, color="#888", linewidth=1.0, alpha=0.8, label="50MA")

    # 50MA cross 시점 마커 (마지막 60일 내)
    aligned = ratio.align(ma50, join="inner")
    r_vals = aligned[0].values
    m_vals = aligned[1].values
    idx_vals = aligned[0].index
    cross_marks_up = []
    cross_marks_dn = []
    for i in range(1, len(r_vals)):
        if np.isnan(m_vals[i]) or np.isnan(m_vals[i-1]):
            continue
        if r_vals[i-1] < m_vals[i-1] and r_vals[i] >= m_vals[i]:
            cross_marks_up.append(i)
        elif r_vals[i-1] > m_vals[i-1] and r_vals[i] <= m_vals[i]:
            cross_marks_dn.append(i)
    cutoff = max(0, len(r_vals) - 60)
    for i in cross_marks_up:
        if i >= cutoff:
            ax.scatter(idx_vals[i], r_vals[i], color="#2e7d32", s=60, zorder=5,
                       marker="^", edgecolor="white", linewidth=1.2)
    for i in cross_marks_dn:
        if i >= cutoff:
            ax.scatter(idx_vals[i], r_vals[i], color="#c62828", s=60, zorder=5,
                       marker="v", edgecolor="white", linewidth=1.2)

    # outperform streak (RSP가 SPY 대비 5D % outperform 일수)
    rsp_ret = rsp_df["Close"].pct_change().dropna()
    spy_ret = spy_df["Close"].pct_change().dropna()
    rel = (rsp_ret - spy_ret).dropna()
    streak = 0
    if len(rel) > 0:
        last_sign = 1 if rel.iloc[-1] > 0 else -1
        for v in rel.values[::-1]:
            if (v > 0 and last_sign > 0) or (v < 0 and last_sign < 0):
                streak += 1
            else:
                break
        streak *= last_sign

    up = float(ratio.iloc[-1]) >= float(ratio.iloc[-20]) if len(ratio) >= 20 else True
    above_ma = bool(r_vals[-1] >= m_vals[-1]) if not np.isnan(m_vals[-1]) else None
    parts = []
    if streak != 0:
        s_dir = "RSP outperform" if streak > 0 else "SPY outperform"
        parts.append(f"{s_dir} {abs(streak)}일")
    if above_ma is True:
        parts.append("50MA 위")
    elif above_ma is False:
        parts.append("50MA 아래")
    note = "↗ 동일가중 우위(폭 확산)" if up else "↘ 시총가중 우위(쏠림)"
    if parts:
        note += "  ·  " + " · ".join(parts)
    ax.fill_between(ratio.index, ratio.values, float(ratio.min()),
                    color="#d62728" if up else "#1f77b4", alpha=0.07)
    ax.set_title(f"RSP/SPY 비율 — {note}", fontsize=12,
                 color=theme.COLOR_UP if up else theme.COLOR_DOWN)
    ax.legend(fontsize=8, loc="upper left")
    ax.tick_params(labelsize=8)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
