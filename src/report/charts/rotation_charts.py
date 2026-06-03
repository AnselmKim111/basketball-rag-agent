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
                       date_iso: str | None = None,
                       is_korea: bool = False) -> str | None:
    """perf: [{"label","r1m","r3m","r1d","bucket"?}] → 1M 정렬 수평 바.

    is_korea=False (미국): bucket 색상 (주도지속=진녹·새로강해지는=연녹·숨고르기=회·소외빈집=적).
    is_korea=True (KR): bucket 없으니 한국식 양수=적·음수=청 + 별도 legend.
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
    if is_korea:
        c1 = [theme.COLOR_UP if v >= 0 else theme.COLOR_DOWN for v in r1m]
    else:
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
    if is_korea:
        # 한국식 legend — 양수(적)·음수(청)·3M(회)
        legend_handles = [
            Patch(color=theme.COLOR_UP, label="1M 양수(적)"),
            Patch(color=theme.COLOR_DOWN, label="1M 음수(청)"),
            Patch(color="#bbbbbb", label="3M"),
        ]
        ax.legend(handles=legend_handles, fontsize=7, loc="lower right", ncol=3,
                  framealpha=0.9, columnspacing=1.0)
    else:
        # 미국 bucket 범례 + 3M 회색
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


# 미국 ETF ↔ 한국 ETF 페어 매핑 (1M 비교)
_US_KR_SECTOR_PAIRS = [
    ("반도체", "SOXX", "091160"),
    ("방산", "ITA", "449450"),
    ("2차전지", "LIT", "305540"),
    ("신재생", "ICLN", "442320"),
    ("자동차", "CARZ", "091180"),
    ("헬스케어", "XLV", "144200"),
]


def us_kr_sector_pairs(theme_rows: list[dict], kr_perf: list[dict], out_dir: Path,
                       filename: str = "07c_us_kr_pairs.png",
                       date_iso: str | None = None) -> str | None:
    """미국 ↔ 한국 sector 페어 5D 차이 — 디커플링·동조 시각화.

    theme_rows: 미국 테마 모멘텀 list ({label, r5d, ...}).
    kr_perf: 한국 섹터 list ({label, r5d, ...}) (fetch_kr_sectors).
    """
    if not theme_rows or not kr_perf:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np

    # 미국 5D dict (label substring 매칭)
    def _find_us(kw: str) -> float | None:
        for r in theme_rows:
            lbl = (r.get("label") or "").upper()
            if kw.upper() in lbl:
                v = r.get("r5d")
                return float(v) if v is not None else None
        return None

    def _find_kr(code: str) -> float | None:
        for r in kr_perf:
            if r.get("ticker") == code:
                v = r.get("r5d")
                return float(v) if v is not None else None
        return None

    rows = []
    for name, us_kw, kr_code in _US_KR_SECTOR_PAIRS:
        us_v = _find_us(us_kw)
        kr_v = _find_kr(kr_code)
        if us_v is None or kr_v is None:
            continue
        rows.append((name, us_v, kr_v, us_v - kr_v))
    if not rows:
        return None

    fig, ax = plt.subplots(figsize=(13, max(4.2, 0.65 * len(rows) + 1.8)))
    y = np.arange(len(rows))
    h = 0.36
    us_vals = [r[1] for r in rows]
    kr_vals = [r[2] for r in rows]
    diff_vals = [r[3] for r in rows]

    ax.barh(y + h / 2, us_vals, height=h, color="#1f77b4", alpha=0.85, label="미국 5D")
    ax.barh(y - h / 2, kr_vals, height=h, color="#d62728", alpha=0.85, label="한국 5D")
    ax.set_yticks(y)
    ax.set_yticklabels([r[0] for r in rows], fontsize=10, fontweight="bold")
    ax.invert_yaxis()
    ax.axvline(0, color="#333", linewidth=0.6)
    ax.set_xlabel("5D 등락률 (%)", fontsize=9)
    ax.grid(axis="x", linestyle=":", alpha=0.4)
    ax.legend(fontsize=9, loc="lower right")

    # 우측 diff annotation + 분류
    mx = max([abs(v) for v in us_vals + kr_vals] + [1])
    for i, (name, us_v, kr_v, diff) in enumerate(rows):
        if abs(diff) >= 5:
            tag = "디커플링 ↑↓" if diff > 0 else "디커플링 ↓↑"
            col = "#c62828"
        elif abs(diff) >= 2:
            tag = "약한 디커플링"
            col = "#ef6c00"
        else:
            tag = "동조"
            col = "#2e7d32"
        ax.text(mx * 1.05, i, f"Δ {diff:+.1f}%  ·  {tag}",
                ha="left", va="center", fontsize=9, fontweight="bold", color=col)

    ax.set_title("[미국 ↔ 한국 섹터 페어] 5D 등락 차이 — 디커플링·동조 분류",
                 fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
