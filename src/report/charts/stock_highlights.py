"""§6 개별 종목 하이라이트 — 스토리 종목 1Y 미니차트 그리드."""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def highlight_grid(dfs: dict[str, object], out_dir: Path, filename: str = "30_highlights.png",
                   days: int = 120, date_iso: str | None = None) -> str | None:
    """dfs: {label: DataFrame} → 2열 small-multiple. 각 셀: 일봉 캔들 + 20MA + 일변화 배지."""
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    items = [(k, v) for k, v in dfs.items() if v is not None and len(v) > 5]
    if not items:
        return None
    cols = 2
    rows = (len(items) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(13, 3.4 * rows))
    axes = axes.flatten() if rows * cols > 1 else [axes]
    for i, (label, df) in enumerate(items):
        ax = axes[i]
        n = min(days, len(df))
        close = df["Close"]
        last = float(close.iloc[-1]); prev = float(close.iloc[-2]) if len(close) > 1 else last
        chg = (last / prev - 1) * 100 if prev else 0
        color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
        theme.candlestick(ax, df, n=n)
        if len(close) >= 20:
            ma20 = close.rolling(20).mean().iloc[-n:].values
            ax.plot(np.arange(n), ma20, color=theme.COLOR_MA[1], linewidth=0.8, alpha=0.85)
        ax.set_title(f"{label}  ({chg:+.2f}%)", fontsize=9, color=color)
        ax.tick_params(labelsize=6)
        theme.date_xticks(ax, df.index, n=n, count=4)
    for j in range(len(items), len(axes)):
        axes[j].set_axis_off()
    fig.suptitle("개별 종목 하이라이트 (일봉, 최근 120일)", fontsize=13)
    theme.stamp(axes[len(items) - 1], date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
