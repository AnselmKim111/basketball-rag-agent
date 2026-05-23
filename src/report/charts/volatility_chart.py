"""매크로/변동성 라인 차트 — 금리·유가·VIX 등."""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def macro_line(df, label: str, out_dir: Path, filename: str) -> str | None:
    """단일 매크로 지표 라인 (최근 180일) + 최근값 주석."""
    theme.setup()
    import matplotlib.pyplot as plt
    if df is None or len(df) < 5:
        return None
    fig, ax = plt.subplots(figsize=(12, 4.5))
    s = df["Close"].iloc[-180:]
    last = float(s.iloc[-1]); prev = float(s.iloc[-2]) if len(s) > 1 else last
    chg = (last / prev - 1) * 100 if prev else 0
    color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
    ax.plot(s.index, s.values, color=color, linewidth=1.5)
    ax.axhline(float(s.iloc[-1]), color="#bbbbbb", linewidth=0.6, linestyle=":")
    ax.set_title(f"{label}  {last:,.2f}  ({chg:+.2f}%)", fontsize=12, color=color)
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def macro_grid(macro: dict[str, object], out_dir: Path, filename: str = "20_macro_grid.png") -> str | None:
    """주요 매크로 6종 그리드 (금리/유가/달러/VIX/비트코인 등)."""
    theme.setup()
    import matplotlib.pyplot as plt
    keys = [k for k in macro.keys()][:6]
    if not keys:
        return None
    n = len(keys)
    rows = (n + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=(13, 3.2 * rows))
    axes = axes.flatten() if n > 1 else [axes]
    for i, k in enumerate(keys):
        ax = axes[i]
        s = macro[k]["Close"].iloc[-180:]
        last = float(s.iloc[-1]); prev = float(s.iloc[-2]) if len(s) > 1 else last
        chg = (last / prev - 1) * 100 if prev else 0
        color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
        ax.plot(s.index, s.values, color=color, linewidth=1.3)
        ax.set_title(f"{k}  {last:,.2f} ({chg:+.1f}%)", fontsize=10, color=color)
        ax.tick_params(labelsize=7)
    for j in range(n, len(axes)):
        axes[j].set_axis_off()
    fig.suptitle("매크로 대시보드 (최근 180일)", fontsize=13)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
