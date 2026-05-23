"""§6 개별 종목 하이라이트 — 스토리 종목 1Y 미니차트 그리드."""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def highlight_grid(dfs: dict[str, object], out_dir: Path, filename: str = "30_highlights.png",
                   days: int = 252, date_iso: str | None = None) -> str | None:
    """dfs: {label: DataFrame} → 3열 small-multiple. 각 셀: 종가 라인 + 일변화 배지."""
    theme.setup()
    import matplotlib.pyplot as plt
    items = [(k, v) for k, v in dfs.items() if v is not None and len(v) > 5]
    if not items:
        return None
    cols = 3
    rows = (len(items) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(13, 2.7 * rows))
    axes = axes.flatten() if rows * cols > 1 else [axes]
    for i, (label, df) in enumerate(items):
        ax = axes[i]
        s = df["Close"].iloc[-days:]
        last = float(s.iloc[-1]); prev = float(s.iloc[-2]) if len(s) > 1 else last
        chg = (last / prev - 1) * 100 if prev else 0
        color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
        ax.plot(s.index, s.values, color=color, linewidth=1.3)
        ax.fill_between(s.index, s.values, float(s.min()), color=color, alpha=0.07)
        ax.set_title(f"{label}  ({chg:+.2f}%)", fontsize=9, color=color)
        ax.tick_params(labelsize=6)
        ax.margins(x=0)
    for j in range(len(items), len(axes)):
        axes[j].set_axis_off()
    fig.suptitle("개별 종목 하이라이트 (최근 1년)", fontsize=13)
    theme.stamp(axes[len(items) - 1], date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
