"""미국 4대 지수 2x2 그리드 + 개별 ETF 차트."""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def us_indices_grid(dfs: dict[str, object], out_dir: Path, filename: str = "01_us_indices.png") -> str | None:
    """미국 4대 지수 2x2. 각 박스 등락률·종가. dfs={label: DataFrame}."""
    theme.setup()
    import matplotlib.pyplot as plt
    items = [(k, dfs[k]) for k in ("DOW", "NASDAQ", "S&P500", "Russell2000") if k in dfs]
    if not items:
        return None
    fig, axes = plt.subplots(2, 2, figsize=(13, 7.3))
    axes = axes.flatten()
    for i, (label, df) in enumerate(items[:4]):
        ax = axes[i]
        close = df["Close"].iloc[-120:]
        last = float(close.iloc[-1]); prev = float(close.iloc[-2]) if len(close) > 1 else last
        chg = (last / prev - 1) * 100 if prev else 0
        color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
        ax.plot(close.index, close.values, color=color, linewidth=1.6)
        if len(df) >= 20:
            ma20 = df["Close"].iloc[-120:].rolling(20).mean()
            ax.plot(ma20.index, ma20.values, color=theme.COLOR_MA[1], linewidth=0.9, alpha=0.8, label="20MA")
        ax.set_title(f"{label}  {last:,.0f}  ({chg:+.2f}%)", fontsize=12, color=color)
        ax.tick_params(labelsize=8)
    for j in range(len(items), 4):
        axes[j].set_axis_off()
    fig.suptitle("미국 4대 지수 (최근 120일)", fontsize=13)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def etf_chart(df, label: str, out_dir: Path, filename: str) -> str | None:
    """ETF/지수 단일 차트 — 종가 + 20/50/200MA + 52주 고점선."""
    theme.setup()
    import matplotlib.pyplot as plt
    if df is None or len(df) < 20:
        return None
    fig, ax = plt.subplots(figsize=(12, 6))
    close = df["Close"]
    ax.plot(close.index, close.values, color=theme.COLOR_NEUTRAL, linewidth=1.5, label=label)
    for w, c, name in ((20, theme.COLOR_MA[1], "20MA"), (50, theme.COLOR_MA[2], "50MA"), (200, theme.COLOR_MA[3], "200MA")):
        if len(close) >= w:
            ma = close.rolling(w).mean()
            ax.plot(ma.index, ma.values, color=c, linewidth=1.0, alpha=0.85, label=name)
    # 52주 고점 수평선
    win = close.iloc[-252:] if len(close) >= 252 else close
    hi = float(win.max())
    ax.axhline(hi, color=theme.COLOR_UP, linestyle="--", linewidth=0.8, alpha=0.6)
    last = float(close.iloc[-1]); prev = float(close.iloc[-2])
    chg = (last / prev - 1) * 100 if prev else 0
    ax.set_title(f"{label}  {last:,.2f}  ({chg:+.2f}%)  · 52주고점 {hi:,.2f}", fontsize=12)
    ax.legend(fontsize=8, loc="upper left")
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
