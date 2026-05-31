"""히트맵 — "한눈에 자금흐름" 핵심 시각 자산 (TradingView 스타일).

1) sp500_heatmap: 시총 가중 트리맵 (크기=시총, 색=당일 등락). TradingView/finviz 룩어라이크.
2) theme_rotation_heatmap: 테마/섹터 로테이션 트리맵 (크기=모멘텀 강도, 색=5D).
3) korea_flow_heatmap: 한국 시장×투자자 수급 방향 히트맵.

색 컨벤션(히트맵 한정): **녹=상승 / 적=하락 (TradingView 글로벌 표준)**.
개별 캔들/라인 차트는 한국식(적=상승) 유지 — 각 히트맵 타이틀에 "녹=상승" 명시.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)

# TradingView 다크 테마
_TV_BG = "#131722"        # 차트 배경
_TV_FG = "#d1d4dc"        # 보조 텍스트
_TV_EDGE = "#131722"      # 타일 경계(배경색 → 얇은 간격 효과)


def _tv_norm_cmap(vmin: float = -3.0, vmax: float = 3.0):
    """TradingView 히트맵 팔레트 — 적(하락)↔다크슬레이트(보합)↔녹(상승). 녹=상승."""
    from matplotlib import colors
    cmap = colors.LinearSegmentedColormap.from_list(
        "tvheat",
        ["#f7525f", "#a93b40", "#2a2e39", "#2f7d63", "#22ab94"])
    return colors.Normalize(vmin=vmin, vmax=vmax), cmap


def _tv_legend(fig, cmap, vmin: float, vmax: float, label: str = "Performance %"):
    """TradingView식 하단 컬러 범례 스트립 (그라데이션 + % 눈금)."""
    import numpy as np
    cax = fig.add_axes([0.355, 0.022, 0.29, 0.016])
    grad = np.linspace(vmin, vmax, 256).reshape(1, -1)
    cax.imshow(grad, aspect="auto", cmap=cmap, extent=[vmin, vmax, 0, 1])
    cax.set_yticks([])
    ticks = [vmin, vmin / 2, 0, vmax / 2, vmax]
    cax.set_xticks(ticks)
    cax.set_xticklabels([f"{t:+.0f}%" if t else "0%" for t in ticks], fontsize=7, color=_TV_FG)
    cax.tick_params(length=0, pad=2)
    for s in cax.spines.values():
        s.set_visible(False)
    cax.set_title(label, fontsize=7.5, color=_TV_FG, pad=2)


def _diverging(vmin=-3.0, vmax=3.0):
    """레거시(한국식 적=상승) — 일부 비-TV 사용처 호환용으로 유지."""
    from matplotlib import colors
    cmap = colors.LinearSegmentedColormap.from_list(
        "krrb", ["#0d47a1", "#1f77b4", "#90a4ae", "#e0e0e0", "#ef9a9a", "#d62728", "#8b0000"])
    return colors.Normalize(vmin=vmin, vmax=vmax), cmap


def _text_color(rgba) -> str:
    r, g, b = rgba[0], rgba[1], rgba[2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "white" if lum < 0.6 else "#111111"


def _fmt_krw(v: float) -> str:
    """원 단위 → 조/억 가독 포맷 (입력은 백만원 가정)."""
    won = v * 1_000_000
    a = abs(won)
    if a >= 1e12:
        return f"{won/1e12:+.1f}조"
    if a >= 1e8:
        return f"{won/1e8:+.0f}억"
    return f"{won/1e6:+.0f}백만"


def sp500_heatmap(
    caps: dict[str, int],
    changes: dict[str, float],
    sectors: dict[str, str],
    out_dir: Path,
    filename: str = "02_sp500_heatmap.png",
    top_n: int = 500,
    date_iso: str | None = None,
    industries: dict[str, str] | None = None,
) -> str | None:
    """TradingView식 개별 종목 히트맵 — 시총 상위 top_n 종목을 섹터→세부산업으로 묶어
    크기=시총·색=당일등락(녹=상승)으로 트리맵. 수백 종목을 한눈에.

    caps가 비어도 changes만 있으면 |등락| 기반 균등 트리맵으로 렌더(빈 화면 없음).
    """
    theme.setup()
    import matplotlib.pyplot as plt
    industries = industries or {}

    if not changes:
        log.warning("[heatmap] sp500: 등락 데이터 0개 — 스킵")
        return None
    cap_weighted = bool(caps) and sum(1 for s in changes if caps.get(s)) >= max(10, len(changes) // 3)
    # 시총 가중 모드면 시총 있는 종목만 (시총 없는 종목이 작게 잘못 표시되는 것 방지)
    syms = [s for s in changes if caps.get(s)] if cap_weighted else list(changes.keys())

    def size_of(s: str) -> float:
        if cap_weighted and caps.get(s):
            return float(caps[s])
        return abs(changes[s]) + 0.4

    syms = sorted(syms, key=lambda s: -size_of(s))[:top_n]
    norm, cmap = _tv_norm_cmap()

    sec_groups: dict[str, list[str]] = {}
    for s in syms:
        sec_groups.setdefault((sectors.get(s) or "기타").strip(), []).append(s)
    sec_order = sorted(sec_groups, key=lambda k: -sum(size_of(s) for s in sec_groups[k]))

    W, H = 100.0, 62.0
    fig = plt.figure(figsize=(17, 10))
    fig.patch.set_facecolor(_TV_BG)
    ax = fig.add_axes([0.004, 0.06, 0.992, 0.905])
    ax.set_facecolor(_TV_BG)
    ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")

    def draw_stock(s, r):
        col = cmap(norm(max(-6, min(6, changes[s]))))
        ax.add_patch(plt.Rectangle((r["x"], r["y"]), r["dx"], r["dy"],
                     facecolor=col, edgecolor=_TV_BG, linewidth=1.0))
        if r["dx"] > 1.8 and r["dy"] > 1.4:
            # 종목명 살짝 더 크게. 등락 %는 종목명과 동일 크기·볼드.
            fs = max(9, min(50, int(min(r["dx"] * 1.7, r["dy"] * 2.3))))
            cx = r["x"] + r["dx"] / 2; cy = r["y"] + r["dy"] / 2
            show_pct = r["dy"] > 2.3
            if show_pct:
                ax.text(cx, cy + r["dy"] * 0.18, s, ha="center", va="center",
                        fontsize=fs, color="white", fontweight="bold")
                ax.text(cx, cy - r["dy"] * 0.20, f"{changes[s]:+.2f}%", ha="center", va="center",
                        fontsize=fs, color="white", fontweight="bold")
            else:
                ax.text(cx, cy, s, ha="center", va="center",
                        fontsize=fs, color="white", fontweight="bold")

    try:
        import squarify
        sec_sizes = squarify.normalize_sizes(
            [sum(size_of(s) for s in sec_groups[k]) for k in sec_order], W, H)
        sec_rects = squarify.squarify(sec_sizes, 0, 0, W, H)
        for sec, rect in zip(sec_order, sec_rects):
            hdr = 1.5 if rect["dy"] > 7 else 0.0
            pad = 0.12
            sx = rect["x"] + pad; sy = rect["y"] + pad
            sdx = max(rect["dx"] - 2 * pad, 0.1); sdy = max(rect["dy"] - hdr - 2 * pad, 0.1)
            stocks = sec_groups[sec]
            if industries:
                ind_groups: dict[str, list[str]] = {}
                for s in stocks:
                    ind_groups.setdefault((industries.get(s) or sec).strip(), []).append(s)
                ind_order = sorted(ind_groups, key=lambda k: -sum(size_of(s) for s in ind_groups[k]))
            else:
                ind_groups = {sec: stocks}; ind_order = [sec]
            ind_sizes = squarify.normalize_sizes(
                [sum(size_of(s) for s in ind_groups[k]) for k in ind_order], sdx, sdy)
            ind_rects = squarify.squarify(ind_sizes, sx, sy, sdx, sdy)
            for ind, irect in zip(ind_order, ind_rects):
                ihdr = 0.85 if (industries and irect["dy"] > 4 and irect["dx"] > 10) else 0.0
                ip = 0.08
                ix = irect["x"] + ip; iy = irect["y"] + ip
                idx = max(irect["dx"] - 2 * ip, 0.1); idy = max(irect["dy"] - ihdr - 2 * ip, 0.1)
                ist = sorted(ind_groups[ind], key=lambda s: -size_of(s))
                st_sizes = squarify.normalize_sizes([size_of(s) for s in ist], idx, idy)
                st_rects = squarify.squarify(st_sizes, ix, iy, idx, idy)
                for s, r in zip(ist, st_rects):
                    draw_stock(s, r)
                if ihdr:
                    ax.text(irect["x"] + 0.25, irect["y"] + irect["dy"] - 0.2, ind.upper()[:26],
                            ha="left", va="top", fontsize=8.5, color="#aab0bb", alpha=0.9)
            if hdr:
                ax.text(rect["x"] + 0.45, rect["y"] + rect["dy"] - 0.32, sec.upper(),
                        ha="left", va="top", fontsize=13, color="#e2e6ee", alpha=0.97,
                        fontweight="bold")
    except Exception:
        log.warning("[heatmap] squarify 미사용 → grid fallback", exc_info=True)
        cols = int(math.ceil(math.sqrt(len(syms))))
        rows = int(math.ceil(len(syms) / cols))
        ax.set_xlim(0, cols); ax.set_ylim(0, rows)
        for i, s in enumerate(syms):
            r, c = divmod(i, cols)
            col = cmap(norm(max(-6, min(6, changes[s]))))
            ax.add_patch(plt.Rectangle((c, rows - r - 1), 0.95, 0.95,
                         facecolor=col, edgecolor=_TV_BG, linewidth=0.8))
            ax.text(c + 0.47, rows - r - 0.5, s, ha="center", va="center",
                    fontsize=6, color="white", fontweight="bold")

    up = sum(1 for s in syms if changes[s] > 0)
    title = (f"S&P500 히트맵 — 개별 종목 시총 가중 ({len(syms)}종목)  ·  상승 {up}/{len(syms)}"
             + (f"  ·  기준 {date_iso}" if date_iso else ""))
    fig.suptitle(title, fontsize=14, color="white", fontweight="bold", y=0.987)
    _tv_legend(fig, cmap, -3, 3, label="당일 등락 % (녹=상승)")
    return theme.save_fig(fig, out_dir, filename)


def theme_rotation_heatmap(rows: list[dict], out_dir: Path, filename: str = "03_theme_rotation.png",
                           date_iso: str | None = None) -> str | None:
    """테마 로테이션 트리맵 (TradingView 룩) — 크기=|1M| 강도, 색=5D 모멘텀(녹=상승)."""
    theme.setup()
    import matplotlib.pyplot as plt
    rows = [r for r in rows if r.get("r5d") is not None]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r["r5d"], reverse=True)
    norm, cmap = _tv_norm_cmap(vmin=-6, vmax=6)

    def size_of(r) -> float:
        base = abs(r.get("r1m") or 0.0)
        return base + 2.0  # 최소 크기 보장(작은 테마도 보이게)

    W, H = 100.0, 58.0
    fig = plt.figure(figsize=(15, 8.8))
    fig.patch.set_facecolor(_TV_BG)
    ax = fig.add_axes([0.004, 0.07, 0.992, 0.89])
    ax.set_facecolor(_TV_BG)
    ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")

    drawn = False
    try:
        import squarify
        sizes = squarify.normalize_sizes([size_of(r) for r in rows], W, H)
        rects = squarify.squarify(sizes, 0, 0, W, H)
        for r, rect in zip(rows, rects):
            v5 = r["r5d"]
            col = cmap(norm(max(-6, min(6, v5))))
            ax.add_patch(plt.Rectangle((rect["x"], rect["y"]), rect["dx"], rect["dy"],
                         facecolor=col, edgecolor=_TV_BG, linewidth=1.4))
            if rect["dx"] > 5 and rect["dy"] > 3.5:
                fs = max(10, min(26, int(min(rect["dx"] * 0.62, rect["dy"] * 1.4))))
                v1 = r.get("r1d"); v1m = r.get("r1m")
                ax.text(rect["x"] + rect["dx"] / 2, rect["y"] + rect["dy"] / 2 + rect["dy"] * 0.17,
                        r["label"], ha="center", va="center", fontsize=fs,
                        color="white", fontweight="bold")
                v1s = f"{v1:+.1f}" if v1 is not None else "—"
                v1ms = f"{v1m:+.1f}" if v1m is not None else "—"
                # 좁은 타일은 5D만(라벨 오버플로 방지), 넓은 타일은 1D·5D·1M 전체
                sub = (f"1D {v1s} · 5D {v5:+.1f} · 1M {v1ms}" if rect["dx"] > 12
                       else f"5D {v5:+.1f}%")
                ax.text(rect["x"] + rect["dx"] / 2, rect["y"] + rect["dy"] / 2 - rect["dy"] * 0.22,
                        sub, ha="center", va="center",
                        fontsize=max(8, int(fs * 0.55)), color="white")
        drawn = True
    except Exception:
        log.warning("[heatmap] theme squarify 미사용 → grid fallback", exc_info=True)

    if not drawn:
        n = len(rows)
        cols = 4 if n > 12 else (3 if n > 6 else 2)
        rws = math.ceil(n / cols)
        ax.set_xlim(0, cols); ax.set_ylim(0, rws)
        for i, r in enumerate(rows):
            rr, cc = divmod(i, cols)
            v5 = r["r5d"]
            col = cmap(norm(max(-6, min(6, v5))))
            ax.add_patch(plt.Rectangle((cc, rws - rr - 1), 0.96, 0.96,
                         facecolor=col, edgecolor=_TV_EDGE, linewidth=1.2))
            ax.text(cc + 0.48, rws - rr - 0.34, r["label"], ha="center", va="center",
                    fontsize=8, color="white", fontweight="bold")
            v1 = r.get("r1d"); v1s = f"{v1:+.1f}" if v1 is not None else "—"
            ax.text(cc + 0.48, rws - rr - 0.64, f"1D {v1s} · 5D {v5:+.1f}%",
                    ha="center", va="center", fontsize=7, color="white")

    title = ("테마·섹터 로테이션 — 크기=1M 강도, 색=5D 모멘텀"
             + (f"  ·  기준 {date_iso}" if date_iso else ""))
    fig.suptitle(title, fontsize=14, color="white", fontweight="bold", y=0.985)
    _tv_legend(fig, cmap, -6, 6, label="5D 모멘텀 % (녹=상승)")
    return theme.save_fig(fig, out_dir, filename)


def korea_flow_heatmap(flows: dict[str, object], out_dir: Path, filename: str = "31_kr_flow_heatmap.png",
                       window: int = 20, date_iso: str | None = None) -> str | None:
    """한국 시장(KOSPI/KOSDAQ)×투자자(기관/외국인/개인) 최근 window일 누적순매수 히트맵.

    TradingView 룩(다크·녹=순매수/적=순매도). 각 타일에 실제 누적 순매수 값 표기.
    """
    theme.setup()
    import matplotlib.pyplot as plt
    markets = [m for m in ("KOSPI", "KOSDAQ") if m in flows and flows[m] is not None]
    if not markets:
        return None
    investors = ["기관", "외국인", "개인"]
    norm, cmap = _tv_norm_cmap(vmin=-1, vmax=1)

    vals: dict[tuple, float] = {}
    mx = 1.0
    for m in markets:
        fdf = flows[m]
        for inv in investors:
            if inv in fdf.columns:
                v = float(fdf[inv].iloc[-window:].sum())
                vals[(m, inv)] = v
                mx = max(mx, abs(v))

    fig, ax = plt.subplots(figsize=(9.5, 1.7 * len(markets) + 1.4))
    fig.patch.set_facecolor(_TV_BG)
    ax.set_facecolor(_TV_BG)
    ax.set_xlim(0, len(investors)); ax.set_ylim(0, len(markets)); ax.axis("off")
    for ri, m in enumerate(markets):
        for ci, inv in enumerate(investors):
            v = vals.get((m, inv))
            if v is None:
                continue
            # 작은 값도 방향색이 또렷하게: 강도 하한 0.55 + sqrt 부스트
            frac = (abs(v) / mx) ** 0.5
            inten = (0.55 + 0.45 * min(1.0, frac)) * (1 if v >= 0 else -1)
            col = cmap(norm(inten))
            ax.add_patch(plt.Rectangle((ci + 0.02, len(markets) - ri - 0.98), 0.96, 0.96,
                         facecolor=col, edgecolor=_TV_EDGE, linewidth=2.0))
            cx, cy = ci + 0.5, len(markets) - ri - 0.5
            ax.text(cx, cy + 0.26, f"{m} {inv}", ha="center", va="center",
                    fontsize=10, color="white", fontweight="bold")
            arrow = "▲ 순매수" if v >= 0 else "▼ 순매도"
            ax.text(cx, cy - 0.02, arrow, ha="center", va="center",
                    fontsize=9, color="white")
            ax.text(cx, cy - 0.28, _fmt_krw(v), ha="center", va="center",
                    fontsize=8.5, color="white", alpha=0.9)
    title = (f"한국 수급 히트맵 — 최근 {window}일 누적순매수  ·  녹=순매수"
             + (f"  ·  기준 {date_iso}" if date_iso else ""))
    fig.suptitle(title, fontsize=12.5, color="white", fontweight="bold")
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
