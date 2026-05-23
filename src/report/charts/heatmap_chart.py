"""S&P500 시총 가중 히트맵 (treemap 근사) — 시장 폭 판단용.

박스 크기 = 시총, 색 = 당일 등락률(빨강 상승 / 파랑 하락 — 한국 관습).
squarify 미설치 시 시총 상위 N개 grid 히트맵으로 graceful fallback.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def sp500_heatmap(
    caps: dict[str, int],
    changes: dict[str, float],
    sectors: dict[str, str],
    out_dir: Path,
    filename: str = "02_sp500_heatmap.png",
    top_n: int = 60,
) -> str | None:
    """시총 상위 top_n 종목 히트맵. caps/changes/sectors: {symbol: value}."""
    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib import cm, colors

    syms = [s for s in caps if s in changes]
    if not syms:
        return None
    syms = sorted(syms, key=lambda s: -caps[s])[:top_n]
    sizes = [caps[s] for s in syms]
    chg = [changes[s] for s in syms]

    # 색: -5%~+5% 클립 → 파랑(하락)~빨강(상승)
    norm = colors.Normalize(vmin=-5, vmax=5)
    cmap = colors.LinearSegmentedColormap.from_list("krrb", ["#1f77b4", "#dddddd", "#d62728"])
    box_colors = [cmap(norm(max(-5, min(5, c)))) for c in chg]

    try:
        import squarify  # type: ignore
        fig, ax = plt.subplots(figsize=(13, 8))
        labels = [f"{s}\n{changes[s]:+.1f}%" for s in syms]
        squarify.plot(sizes=sizes, label=labels, color=box_colors, ax=ax,
                      text_kwargs={"fontsize": 7}, pad=True)
        ax.set_axis_off()
    except Exception:
        # fallback: grid
        import math
        cols = int(math.ceil(math.sqrt(len(syms))))
        rows = int(math.ceil(len(syms) / cols))
        fig, ax = plt.subplots(figsize=(13, 8))
        for i, s in enumerate(syms):
            r, c = divmod(i, cols)
            ax.add_patch(plt.Rectangle((c, rows - r - 1), 0.95, 0.95, color=box_colors[i]))
            ax.text(c + 0.47, rows - r - 0.5, f"{s}\n{changes[s]:+.1f}%",
                    ha="center", va="center", fontsize=7)
        ax.set_xlim(0, cols); ax.set_ylim(0, rows); ax.set_axis_off()

    fig.suptitle(f"S&P500 시총 상위 {len(syms)} 히트맵 (크기=시총, 색=당일 등락)", fontsize=12)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
