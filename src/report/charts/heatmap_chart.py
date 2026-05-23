"""히트맵 — "한눈에 자금흐름" 핵심 시각 자산.

1) sp500_heatmap: finviz식 섹터 그룹 트리맵 (크기=시총, 색=당일 등락)
2) theme_rotation_heatmap: 테마/섹터 리더보드 히트맵 (색=5D 모멘텀, 1D/5D 라벨)
3) korea_flow_heatmap: 한국 시장×투자자 수급 방향 히트맵

색: 빨강=상승 / 파랑=하락 (한국 관습).
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def _diverging(vmin=-3.0, vmax=3.0):
    from matplotlib import colors
    cmap = colors.LinearSegmentedColormap.from_list(
        "krrb", ["#0d47a1", "#1f77b4", "#90a4ae", "#e0e0e0", "#ef9a9a", "#d62728", "#8b0000"])
    return colors.Normalize(vmin=vmin, vmax=vmax), cmap


def _text_color(rgba) -> str:
    r, g, b = rgba[0], rgba[1], rgba[2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "white" if lum < 0.55 else "#111111"


def sp500_heatmap(
    caps: dict[str, int],
    changes: dict[str, float],
    sectors: dict[str, str],
    out_dir: Path,
    filename: str = "02_sp500_heatmap.png",
    top_n: int = 140,
    date_iso: str | None = None,
) -> str | None:
    """시총 상위 top_n 종목을 섹터별로 묶은 트리맵. 크기=시총, 색=당일 등락."""
    theme.setup()
    import matplotlib.pyplot as plt

    syms = [s for s in caps if s in changes and caps.get(s)]
    if not syms:
        return None
    syms = sorted(syms, key=lambda s: -caps[s])[:top_n]
    norm, cmap = _diverging()

    # 섹터별 그룹
    groups: dict[str, list[str]] = {}
    for s in syms:
        sec = sectors.get(s) or "기타"
        groups.setdefault(sec, []).append(s)
    sec_order = sorted(groups, key=lambda sec: -sum(caps[s] for s in groups[sec]))

    W, H = 100.0, 62.0
    fig, ax = plt.subplots(figsize=(14, 8.6))
    ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")

    try:
        import squarify
        sec_sizes = squarify.normalize_sizes(
            [sum(caps[s] for s in groups[sec]) for sec in sec_order], W, H)
        sec_rects = squarify.squarify(sec_sizes, 0, 0, W, H)
        for sec, rect in zip(sec_order, sec_rects):
            stocks = sorted(groups[sec], key=lambda s: -caps[s])
            pad = 0.4
            x0, y0 = rect["x"] + pad, rect["y"] + pad
            dx, dy = max(rect["dx"] - 2 * pad, 0.1), max(rect["dy"] - 2 * pad, 0.1)
            st_sizes = squarify.normalize_sizes([caps[s] for s in stocks], dx, dy)
            st_rects = squarify.squarify(st_sizes, x0, y0, dx, dy)
            for s, r in zip(stocks, st_rects):
                col = cmap(norm(max(-8, min(8, changes[s]))))
                ax.add_patch(plt.Rectangle((r["x"], r["y"]), r["dx"], r["dy"],
                             facecolor=col, edgecolor="white", linewidth=0.5))
                if r["dx"] > 4.2 and r["dy"] > 3.0:
                    fs = max(5, min(10, int(r["dx"] * 0.9)))
                    ax.text(r["x"] + r["dx"] / 2, r["y"] + r["dy"] / 2,
                            f"{s}\n{changes[s]:+.1f}%", ha="center", va="center",
                            fontsize=fs, color=_text_color(col))
            # 섹터 라벨
            ax.text(rect["x"] + 0.6, rect["y"] + rect["dy"] - 0.4, sec,
                    ha="left", va="top", fontsize=8, color="#222", alpha=0.6, fontweight="bold")
    except Exception:
        log.warning("[heatmap] squarify 미사용 → grid fallback", exc_info=True)
        cols = int(math.ceil(math.sqrt(len(syms))))
        rows = int(math.ceil(len(syms) / cols))
        ax.set_xlim(0, cols); ax.set_ylim(0, rows)
        for i, s in enumerate(syms):
            r, c = divmod(i, cols)
            col = cmap(norm(max(-8, min(8, changes[s]))))
            ax.add_patch(plt.Rectangle((c, rows - r - 1), 0.95, 0.95, facecolor=col, edgecolor="white"))
            ax.text(c + 0.47, rows - r - 0.5, f"{s}\n{changes[s]:+.1f}%",
                    ha="center", va="center", fontsize=6, color=_text_color(col))

    up = sum(1 for s in syms if changes[s] > 0)
    fig.suptitle(f"S&P500 히트맵 — 상위 {len(syms)}종목 (크기=시총, 색=당일)  ·  상승 {up}/{len(syms)}",
                 fontsize=13)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def theme_rotation_heatmap(rows: list[dict], out_dir: Path, filename: str = "03_theme_rotation.png",
                           date_iso: str | None = None) -> str | None:
    """테마 리더보드 히트맵 — 5D 모멘텀 정렬, 색=5D, 라벨=테마+1D/5D. '돈이 어디로'를 한눈에."""
    theme.setup()
    import matplotlib.pyplot as plt
    rows = [r for r in rows if r.get("r5d") is not None]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r["r5d"], reverse=True)
    norm, cmap = _diverging(vmin=-6, vmax=6)

    n = len(rows)
    cols = 4 if n > 12 else (3 if n > 6 else 2)
    rws = math.ceil(n / cols)
    fig, ax = plt.subplots(figsize=(3.2 * cols, 1.5 * rws + 0.8))
    ax.set_xlim(0, cols); ax.set_ylim(0, rws); ax.axis("off")
    for i, r in enumerate(rows):
        rr, cc = divmod(i, cols)
        v5 = r["r5d"]; v1 = r.get("r1d")
        col = cmap(norm(max(-6, min(6, v5))))
        ax.add_patch(plt.Rectangle((cc, rws - rr - 1), 0.96, 0.96,
                     facecolor=col, edgecolor="white", linewidth=1.2))
        tc = _text_color(col)
        ax.text(cc + 0.48, rws - rr - 0.32, r["label"], ha="center", va="center",
                fontsize=8, color=tc, fontweight="bold")
        v1s = f"{v1:+.1f}%" if v1 is not None else "—"
        ax.text(cc + 0.48, rws - rr - 0.62, f"1D {v1s}  ·  5D {v5:+.1f}%",
                ha="center", va="center", fontsize=7, color=tc)
    fig.suptitle("테마·섹터 로테이션 히트맵 (색=5일 모멘텀)", fontsize=13)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def korea_flow_heatmap(flows: dict[str, object], out_dir: Path, filename: str = "31_kr_flow_heatmap.png",
                       window: int = 20, date_iso: str | None = None) -> str | None:
    """한국 시장(KOSPI/KOSDAQ) × 투자자(기관/외국인/개인) 최근 window일 누적순매수 방향 히트맵."""
    theme.setup()
    import matplotlib.pyplot as plt
    markets = [m for m in ("KOSPI", "KOSDAQ") if m in flows and flows[m] is not None]
    if not markets:
        return None
    investors = ["기관", "외국인", "개인"]
    norm, cmap = _diverging(vmin=-1, vmax=1)

    # 값: window일 합 (백만/억 단위 가변) → 부호+상대크기 색
    vals = {}
    mx = 1.0
    for m in markets:
        fdf = flows[m]
        for inv in investors:
            if inv in fdf.columns:
                v = float(fdf[inv].iloc[-window:].sum())
                vals[(m, inv)] = v
                mx = max(mx, abs(v))
    fig, ax = plt.subplots(figsize=(7.5, 1.4 * len(markets) + 1.4))
    ax.set_xlim(0, len(investors)); ax.set_ylim(0, len(markets)); ax.axis("off")
    for ri, m in enumerate(markets):
        for ci, inv in enumerate(investors):
            v = vals.get((m, inv))
            if v is None:
                continue
            col = cmap(norm(max(-1, min(1, v / mx))))
            ax.add_patch(plt.Rectangle((ci, len(markets) - ri - 1), 0.96, 0.96,
                         facecolor=col, edgecolor="white", linewidth=1.5))
            tc = _text_color(col)
            ax.text(ci + 0.48, len(markets) - ri - 0.36, f"{m}\n{inv}", ha="center", va="center",
                    fontsize=8, color=tc, fontweight="bold")
            arrow = "▲ 순매수" if v >= 0 else "▼ 순매도"
            ax.text(ci + 0.48, len(markets) - ri - 0.66, arrow, ha="center", va="center",
                    fontsize=7, color=tc)
    fig.suptitle(f"한국 수급 방향 히트맵 (최근 {window}일 누적순매수)", fontsize=12)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
