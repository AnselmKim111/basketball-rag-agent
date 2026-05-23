"""§4·§8 자금흐름 시각화.

- usdkrw_ewy_dual: 환율 급등 = 한국 ETF 약세 상관 (dual-axis)
- capital_flow_diagram: 박스+화살표 자금흐름도 (흐름폭=모멘텀, 데이터 기반)
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def usdkrw_ewy_dual(usdkrw_df, ewy_df, out_dir: Path, filename: str = "09_usdkrw_ewy.png",
                    days: int = 180, date_iso: str | None = None) -> str | None:
    """USD/KRW(좌축) + EWY(우축) dual-axis. 환율↑ + EWY↓ 동조 시각화."""
    theme.setup()
    import matplotlib.pyplot as plt
    if usdkrw_df is None or ewy_df is None or len(usdkrw_df) < 10 or len(ewy_df) < 10:
        return None
    fig, ax = plt.subplots(figsize=(12, 5.2))
    fx = usdkrw_df["Close"].iloc[-days:]
    ax.plot(fx.index, fx.values, color="#d62728", linewidth=1.6, label="USD/KRW(좌)")
    ax.set_ylabel("USD/KRW", color="#d62728", fontsize=9)
    ax.tick_params(axis="y", labelcolor="#d62728", labelsize=8)
    ax2 = ax.twinx()
    ey = ewy_df["Close"].iloc[-days:]
    ax2.plot(ey.index, ey.values, color="#1f77b4", linewidth=1.6, label="EWY 한국ETF(우)")
    ax2.set_ylabel("EWY", color="#1f77b4", fontsize=9)
    ax2.tick_params(axis="y", labelcolor="#1f77b4", labelsize=8)
    fxl = float(fx.iloc[-1]); fxc = (fxl / float(fx.iloc[-2]) - 1) * 100 if len(fx) > 1 else 0
    eyl = float(ey.iloc[-1]); eyc = (eyl / float(ey.iloc[-2]) - 1) * 100 if len(ey) > 1 else 0
    ax.set_title(f"환전 압력: USD/KRW {fxl:,.1f}({fxc:+.2f}%) ↔ EWY {eyl:,.2f}({eyc:+.2f}%)", fontsize=12)
    ax.tick_params(axis="x", labelsize=8)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def capital_flow_diagram(sources: list[tuple], destinations: list[tuple], out_dir: Path,
                         filename: str = "40_capital_flow.png",
                         date_iso: str | None = None) -> str | None:
    """박스+화살표 자금흐름도.

    sources: [(label, strength)] 자금 이탈처 (강할수록 굵은 화살표)
    destinations: [(label, strength)] 자금 유입처
    중앙 허브를 거쳐 source→dest 흐름을 시각화.
    """
    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
    if not sources or not destinations:
        return None

    def _norm(items):
        vals = [abs(s) for _, s in items] or [1]
        m = max(vals) or 1
        return [(lbl, abs(s) / m) for lbl, s in items]

    src = _norm(sources)
    dst = _norm(destinations)

    fig, ax = plt.subplots(figsize=(13, max(6, 0.7 * max(len(src), len(dst)) + 2)))
    ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")

    hub = (5.0, 5.0)
    # 허브
    ax.add_patch(FancyBboxPatch((4.2, 4.4), 1.6, 1.2, boxstyle="round,pad=0.1",
                 facecolor="#444", edgecolor="none"))
    ax.text(hub[0], hub[1], "자금\n재편", ha="center", va="center", color="white", fontsize=10, fontweight="bold")

    def _ys(n):
        if n == 1:
            return [5.0]
        return list(__import__("numpy").linspace(8.6, 1.4, n))

    sy = _ys(len(src))
    for (lbl, w), y in zip(src, sy):
        ax.add_patch(FancyBboxPatch((0.2, y - 0.45), 3.0, 0.9, boxstyle="round,pad=0.05",
                     facecolor="#1f77b4", alpha=0.18, edgecolor="#1f77b4"))
        ax.text(1.7, y, lbl, ha="center", va="center", fontsize=8)
        ax.add_patch(FancyArrowPatch((3.3, y), (4.2, hub[1]), arrowstyle="-|>",
                     mutation_scale=12, linewidth=0.8 + 4 * w, color="#1f77b4", alpha=0.55))
    ax.text(1.7, 9.4, "자금 이탈", ha="center", fontsize=10, color="#1f77b4", fontweight="bold")

    dy = _ys(len(dst))
    for (lbl, w), y in zip(dst, dy):
        ax.add_patch(FancyBboxPatch((6.8, y - 0.45), 3.0, 0.9, boxstyle="round,pad=0.05",
                     facecolor="#d62728", alpha=0.16, edgecolor="#d62728"))
        ax.text(8.3, y, lbl, ha="center", va="center", fontsize=8)
        ax.add_patch(FancyArrowPatch((5.8, hub[1]), (6.8, y), arrowstyle="-|>",
                     mutation_scale=12, linewidth=0.8 + 4 * w, color="#d62728", alpha=0.6))
    ax.text(8.3, 9.4, "자금 유입(확산)", ha="center", fontsize=10, color="#d62728", fontweight="bold")

    ax.set_title("종합 자금흐름 다이어그램 — 어디서 빠져 어디로 가는가", fontsize=13)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
