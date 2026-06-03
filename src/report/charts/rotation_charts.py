"""§3 섹터 로테이션 맵 — 자금이 어디로 가는지 한눈에.

- sector_return_bars: 섹터/테마 ETF 1M·3M return 정렬 수평 바
- region_compare: 글로벌 지역 normalized + 1W return 바
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


_BUCKET_COLOR = {
    "주도지속": "#1b5e20",       # 진녹
    "새로 강해지는": "#43a047",   # 연녹
    "숨고르기": "#9e9e9e",       # 회색
    "소외·빈집": "#c62828",      # 빨강
}


def sector_return_bars(perf: list[dict], out_dir: Path, filename: str = "07_sector_bars.png",
                       title: str = "미국 섹터·테마 상대강도 (1M 정렬)",
                       date_iso: str | None = None) -> str | None:
    """perf: [{"label","r1m","r3m","r1d","bucket"?}] → 1M 정렬 수평 바.
    bucket 색상: 주도지속=진녹·새로강해지는=연녹·숨고르기=회·소외빈집=적.
    """
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch
    rows = [p for p in perf if p.get("r1m") is not None]
    if not rows:
        return None
    rows = sorted(rows, key=lambda p: p["r1m"])
    labels = [p["label"] for p in rows]
    r1m = [p["r1m"] for p in rows]
    r3m = [p.get("r3m") if p.get("r3m") is not None else 0.0 for p in rows]
    buckets = [p.get("bucket") for p in rows]
    y = np.arange(len(rows))
    h = 0.38
    fig, ax = plt.subplots(figsize=(11, max(4.5, 0.42 * len(rows) + 1.5)))
    c1 = [_BUCKET_COLOR.get(b) or (theme.COLOR_UP if v >= 0 else theme.COLOR_DOWN)
          for b, v in zip(buckets, r1m)]
    ax.barh(y + h / 2, r1m, height=h, color=c1)
    ax.barh(y - h / 2, r3m, height=h, color="#bbbbbb", label="3M", alpha=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.axvline(0, color="#333", linewidth=0.6)
    for yi, v in zip(y, r1m):
        ax.text(v + (0.3 if v >= 0 else -0.3), yi + h / 2, f"{v:+.1f}%",
                va="center", ha="left" if v >= 0 else "right", fontsize=7)
    ax.set_title(title, fontsize=12)
    # bucket 범례 + 3M 회색
    legend_handles = [Patch(color=col, label=name) for name, col in _BUCKET_COLOR.items()]
    legend_handles.append(Patch(color="#bbbbbb", label="3M"))
    ax.legend(handles=legend_handles, fontsize=7, loc="lower right", ncol=5,
              framealpha=0.9, columnspacing=1.0)
    ax.tick_params(axis="x", labelsize=8)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def region_compare(dfs: dict[str, object], out_dir: Path, filename: str = "08_region.png",
                   days: int = 252, date_iso: str | None = None) -> str | None:
    """글로벌 지역 ETF normalized(100) 라인 + 우측 1W return 바(서브패널)."""
    from src.report.data.fetch_prices import normalize_100, pct_return
    theme.setup()
    import matplotlib.pyplot as plt
    avail = {k: v for k, v in dfs.items() if v is not None and len(v) > 5}
    if not avail:
        return None
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13, 5.5), gridspec_kw={"width_ratios": [3, 1]})
    palette = ["#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]
    w1_rows = []
    for i, (label, df) in enumerate(avail.items()):
        s = normalize_100(df.iloc[-days:])
        if s is None:
            continue
        ax.plot(s.index, s.values, linewidth=1.4, color=palette[i % len(palette)], label=label)
        r1w = pct_return(df, 5)
        if r1w is not None:
            w1_rows.append((label, r1w, palette[i % len(palette)]))
    ax.axhline(100, color="#999", linewidth=0.6, linestyle=":")
    ax.set_title("글로벌 지역 비교 (1년, 100 리베이스)", fontsize=12)
    ax.legend(fontsize=8, loc="upper left")
    ax.tick_params(labelsize=8)
    theme.stamp(ax, date_iso)
    # 1W 바
    if w1_rows:
        w1_rows = sorted(w1_rows, key=lambda r: r[1])
        import numpy as np
        yy = np.arange(len(w1_rows))
        ax2.barh(yy, [r[1] for r in w1_rows],
                 color=[theme.COLOR_UP if r[1] >= 0 else theme.COLOR_DOWN for r in w1_rows])
        ax2.set_yticks(yy); ax2.set_yticklabels([r[0] for r in w1_rows], fontsize=7)
        ax2.axvline(0, color="#333", linewidth=0.6)
        ax2.set_title("주간 수익률(1W)", fontsize=10)
        ax2.tick_params(axis="x", labelsize=7)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
