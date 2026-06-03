"""사용자 portfolio dashboard — 현황 카드 + 종목별 mini-card grid.

3 차트:
  1. summary_card: 총 비용·평가·PnL·종목 수 한 박스
  2. positions_grid: 종목별 카드 (매수가·현재가·PnL·5D·1M·thesis 분류)
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def portfolio_summary_card(summary: dict, out_dir: Path,
                           filename: str = "20_portfolio_summary.png",
                           date_iso: str | None = None) -> str | None:
    """보유 종목 현황 한 박스 — 종목 수·비용·가치·PnL."""
    if not summary or summary.get("total_positions", 0) == 0:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    n = summary.get("total_positions", 0)
    us = summary.get("us_count", 0); kr = summary.get("kr_count", 0)
    pnl_pct = summary.get("total_pnl_pct")
    pnl_amount = summary.get("total_pnl_amount", 0)
    cost = summary.get("total_cost", 0)
    value = summary.get("total_value", 0)

    # 손익 색상
    if isinstance(pnl_pct, (int, float)):
        if pnl_pct > 0:
            pnl_col = "#1b5e20"; pnl_bg = "#c8e6c9"
        elif pnl_pct < 0:
            pnl_col = "#c62828"; pnl_bg = "#ffcdd2"
        else:
            pnl_col = "#616161"; pnl_bg = "#eeeeee"
    else:
        pnl_col = "#616161"; pnl_bg = "#eeeeee"
        pnl_pct = 0

    fig, ax = plt.subplots(figsize=(13, 4.5))
    ax.set_xlim(0, 10); ax.set_ylim(0, 5); ax.axis("off")

    # 4 카드 (종목수 · 비용 · 평가 · PnL)
    cards = [
        ("보유 종목", f"{n}", f"US {us} · KR {kr}", "#37474f", "#cfd8dc"),
        ("총 매수금", f"{cost:,.0f}", "비용 합계", "#1565c0", "#bbdefb"),
        ("총 평가금", f"{value:,.0f}", "현재 가치", "#6a1b9a", "#e1bee7"),
        ("손익", f"{pnl_pct:+.2f}%", f"{pnl_amount:+,.0f}", pnl_col, pnl_bg),
    ]
    for i, (label, big, small, fg, bg) in enumerate(cards):
        x0 = 0.2 + i * 2.45
        ax.add_patch(FancyBboxPatch((x0, 0.5), 2.25, 4.0, boxstyle="round,pad=0.08",
                     facecolor=bg, edgecolor=fg, linewidth=1.6))
        ax.text(x0 + 1.125, 3.7, label, ha="center", va="center", fontsize=12,
                color=fg, fontweight="bold")
        ax.text(x0 + 1.125, 2.4, big, ha="center", va="center", fontsize=20,
                color=fg, fontweight="bold")
        ax.text(x0 + 1.125, 1.3, small, ha="center", va="center", fontsize=10,
                color=fg)

    ax.set_title("[내 보유 종목 현황]", fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def portfolio_positions_grid(positions: list[dict], out_dir: Path,
                             filename: str = "21_portfolio_grid.png",
                             date_iso: str | None = None) -> str | None:
    """종목별 mini-card grid. 매수가·현재가·PnL·5D·1M·thesis 분류·기술."""
    if not positions:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    # 손익 % 큰 순 정렬 (강세 위)
    items = sorted(positions, key=lambda p: -(p.get("pnl_pct") or 0))
    n = len(items)
    cols = 2 if n >= 2 else 1
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(13, 3.3 * rows))
    if rows * cols == 1:
        axes = [axes]
    else:
        axes = list(axes.flatten()) if hasattr(axes, "flatten") else list(axes)

    cls_col = {"강화": ("#1b5e20", "#c8e6c9"), "약화": ("#c62828", "#ffcdd2"),
               "디커플링": ("#ef6c00", "#ffe0b2"), "유지": ("#616161", "#eeeeee")}

    for i, p in enumerate(items):
        ax = axes[i]
        ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")

        ticker = p.get("ticker") or ""
        market = p.get("market") or "US"
        bp = p.get("buy_price") or 0
        cur = p.get("current_price") or 0
        pnl = p.get("pnl_pct")
        pnl_amt = p.get("pnl_amount")
        chg_5d = p.get("chg_5d")
        chg_1m = p.get("chg_1m")
        tech = p.get("technical") or {}
        cls = p.get("thesis_class") or "유지"
        fg, bg = cls_col.get(cls, cls_col["유지"])

        # 박스
        ax.add_patch(FancyBboxPatch((0.2, 0.3), 9.6, 9.4, boxstyle="round,pad=0.1",
                     facecolor=bg, edgecolor=fg, linewidth=1.5))
        # 헤더: ticker + market + 분류 배지
        ax.text(0.5, 9.0, f"{ticker} ({market})", ha="left", va="center",
                fontsize=14, fontweight="bold", color=fg)
        ax.add_patch(FancyBboxPatch((7.6, 8.6), 2.0, 0.8, boxstyle="round,pad=0.05",
                     facecolor=fg, edgecolor="none"))
        ax.text(8.6, 9.0, cls, ha="center", va="center", fontsize=10,
                fontweight="bold", color="white")

        # 매수가·현재가·PnL
        unit = "원" if market == "KR" else "$"
        ax.text(0.5, 7.8, f"매수가 {bp:,.2f}{unit}", ha="left", va="center",
                fontsize=10, color="#333")
        ax.text(0.5, 7.0, f"현재가 {cur:,.2f}{unit}" if cur else "현재가 미확보",
                ha="left", va="center", fontsize=10, color="#333")
        pnl_str = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "—"
        amt_str = f"({pnl_amt:+,.0f})" if isinstance(pnl_amt, (int, float)) else ""
        ax.text(5.0, 7.4, f"PnL {pnl_str} {amt_str}", ha="left", va="center",
                fontsize=12, fontweight="bold", color=fg)

        # 등락
        c5 = f"{chg_5d:+.1f}%" if isinstance(chg_5d, (int, float)) else "—"
        c1 = f"{chg_1m:+.1f}%" if isinstance(chg_1m, (int, float)) else "—"
        ax.text(0.5, 5.5, f"5D {c5}  ·  1M {c1}", ha="left", va="center",
                fontsize=10, color="#222")

        # 기술
        tech_parts = []
        pct_52w = tech.get("pct_52w_high")
        if isinstance(pct_52w, (int, float)):
            tech_parts.append(f"52w {pct_52w:.0f}%")
        ma_parts = []
        if tech.get("above_ma200") is True:
            ma_parts.append("MA200↑")
        elif tech.get("above_ma200") is False:
            ma_parts.append("MA200↓")
        if tech.get("above_ma50") is True:
            ma_parts.append("MA50↑")
        elif tech.get("above_ma50") is False:
            ma_parts.append("MA50↓")
        if ma_parts:
            tech_parts.append(" · ".join(ma_parts))
        if tech_parts:
            ax.text(0.5, 4.3, "  ·  ".join(tech_parts), ha="left", va="center",
                    fontsize=10, color="#444")

        # 수량
        shares = p.get("shares") or 0
        ax.text(0.5, 1.5, f"{shares:,.0f}주 보유", ha="left", va="center",
                fontsize=9, color="#666")
        added = p.get("added_at")
        if added:
            ax.text(9.6, 1.5, f"등록 {added}", ha="right", va="center",
                    fontsize=8, color="#888", style="italic")

    for j in range(len(items), len(axes)):
        axes[j].set_axis_off()

    fig.suptitle("[내 보유 종목] 매수가·현재가·PnL·thesis 분류", fontsize=13, fontweight="bold")
    theme.stamp(axes[len(items) - 1] if items else axes[0], date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
