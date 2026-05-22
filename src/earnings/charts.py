"""어닝콜 PDF용 비교 차트 (matplotlib).

차트 종류:
  1) CapEx 6년치 절대값 grouped bar
  2) CapEx YoY 증감률 (%) line
  3) FCF 6년치 비교 grouped bar
  4) OCF/CapEx 비율 line (1보다 크면 흑자 흐름)
  5) Revenue 6년치 grouped bar
  6) Capex intensity (Capex/Revenue %) line

모든 함수는 matplotlib Figure 반환. PDF 모듈이 PdfPages.savefig(fig)로 저장.
실패 시 None 반환.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


def _setup_matplotlib():
    """matplotlib + 영문 폰트 + 색 팔레트 셋업. import는 본문 안에서 (cold-start 가드)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.dpi"] = 110
    plt.rcParams["savefig.dpi"] = 110
    plt.rcParams["font.size"] = 10
    # "$1.5B" 같은 축 라벨의 '$'가 LaTeX 수식으로 파싱되지 않게 (matplotlib ≥3.7)
    plt.rcParams["text.parse_math"] = False
    return plt


# 색 팔레트 — 6개 회사까지 구별
PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
]


def _ticker_color(idx: int) -> str:
    return PALETTE[idx % len(PALETTE)]


def _format_billions(x, _):
    if abs(x) >= 1e9:
        return f"${x/1e9:.1f}B"
    if abs(x) >= 1e6:
        return f"${x/1e6:.0f}M"
    return f"${x:.0f}"


# ------------------------------------------------------------------
# 차트 빌더
# ------------------------------------------------------------------
def chart_capex_absolute(financials_by_ticker: dict[str, Any]) -> Optional[Any]:
    """CapEx 6년치 grouped bar. financials_by_ticker: {ticker: CompanyFinancials}."""
    try:
        plt = _setup_matplotlib()
        from matplotlib.ticker import FuncFormatter

        years_set: set[int] = set()
        for fin in financials_by_ticker.values():
            for p in fin.capex:
                years_set.add(p.fy)
        if not years_set:
            return None
        years = sorted(years_set)[-6:]
        tickers = list(financials_by_ticker.keys())

        fig, ax = plt.subplots(figsize=(11, 6.5))
        n = len(tickers)
        bar_w = 0.8 / max(n, 1)
        import numpy as np
        xs = np.arange(len(years))

        for i, t in enumerate(tickers):
            fin = financials_by_ticker[t]
            by_fy = {p.fy: p.val for p in fin.capex}
            vals = [by_fy.get(fy, 0) for fy in years]
            ax.bar(
                xs + i * bar_w - (bar_w * (n - 1) / 2),
                vals,
                bar_w * 0.95,
                label=t,
                color=_ticker_color(i),
                edgecolor="black",
                linewidth=0.4,
            )

        ax.set_xticks(xs)
        ax.set_xticklabels([f"FY{fy}" for fy in years])
        ax.set_ylabel("CapEx (USD)")
        ax.set_title("CapEx — 6-Year Comparison", fontsize=13, fontweight="bold", pad=12)
        ax.yaxis.set_major_formatter(FuncFormatter(_format_billions))
        ax.grid(True, axis="y", alpha=0.25, linestyle="--")
        ax.legend(loc="best", fontsize=9, ncol=min(n, 4))
        fig.tight_layout()
        return fig
    except Exception:
        log.exception("chart_capex_absolute 실패")
        return None


def chart_capex_yoy(financials_by_ticker: dict[str, Any]) -> Optional[Any]:
    """CapEx YoY 증감률 (%) line."""
    try:
        plt = _setup_matplotlib()
        fig, ax = plt.subplots(figsize=(11, 6.5))
        any_drawn = False
        for i, (t, fin) in enumerate(financials_by_ticker.items()):
            yoy = fin.capex_yoy_change()
            if not yoy:
                continue
            xs = [p.fy for p in yoy]
            ys = [p.val for p in yoy]
            ax.plot(
                xs, ys, marker="o", linewidth=2,
                color=_ticker_color(i), label=t, markersize=7,
            )
            for x, y in zip(xs, ys):
                ax.annotate(
                    f"{y:+.0f}%", (x, y),
                    xytext=(0, 8), textcoords="offset points",
                    ha="center", fontsize=8, color=_ticker_color(i),
                )
            any_drawn = True

        if not any_drawn:
            import matplotlib.pyplot as plt2
            plt2.close(fig)
            return None

        ax.axhline(y=0, color="black", linewidth=0.6, alpha=0.5)
        ax.set_xlabel("Fiscal Year")
        ax.set_ylabel("CapEx YoY Change (%)")
        ax.set_title("CapEx — Year-over-Year Growth Rate", fontsize=13, fontweight="bold", pad=12)
        ax.grid(True, alpha=0.25, linestyle="--")
        ax.legend(loc="best", fontsize=9, ncol=min(len(financials_by_ticker), 4))
        fig.tight_layout()
        return fig
    except Exception:
        log.exception("chart_capex_yoy 실패")
        return None


def chart_fcf(financials_by_ticker: dict[str, Any]) -> Optional[Any]:
    """FCF 6년치 grouped bar."""
    try:
        plt = _setup_matplotlib()
        from matplotlib.ticker import FuncFormatter
        import numpy as np

        fcf_by_t = {t: fin.fcf() for t, fin in financials_by_ticker.items()}
        years_set: set[int] = set()
        for pts in fcf_by_t.values():
            for p in pts:
                years_set.add(p.fy)
        if not years_set:
            return None
        years = sorted(years_set)[-6:]
        tickers = list(fcf_by_t.keys())

        fig, ax = plt.subplots(figsize=(11, 6.5))
        n = len(tickers)
        bar_w = 0.8 / max(n, 1)
        xs = np.arange(len(years))

        for i, t in enumerate(tickers):
            by_fy = {p.fy: p.val for p in fcf_by_t[t]}
            vals = [by_fy.get(fy, 0) for fy in years]
            ax.bar(
                xs + i * bar_w - (bar_w * (n - 1) / 2),
                vals,
                bar_w * 0.95,
                label=t,
                color=_ticker_color(i),
                edgecolor="black",
                linewidth=0.4,
            )

        ax.set_xticks(xs)
        ax.set_xticklabels([f"FY{fy}" for fy in years])
        ax.set_ylabel("FCF (USD) = OCF − CapEx")
        ax.set_title("Free Cash Flow — 6-Year Comparison", fontsize=13, fontweight="bold", pad=12)
        ax.yaxis.set_major_formatter(FuncFormatter(_format_billions))
        ax.axhline(y=0, color="black", linewidth=0.6, alpha=0.5)
        ax.grid(True, axis="y", alpha=0.25, linestyle="--")
        ax.legend(loc="best", fontsize=9, ncol=min(n, 4))
        fig.tight_layout()
        return fig
    except Exception:
        log.exception("chart_fcf 실패")
        return None


def chart_ocf_capex_ratio(financials_by_ticker: dict[str, Any]) -> Optional[Any]:
    """OCF / CapEx 비율 line. 1보다 크면 영업현금흐름이 자본지출보다 많은 흑자 흐름."""
    try:
        plt = _setup_matplotlib()
        fig, ax = plt.subplots(figsize=(11, 6.5))
        any_drawn = False
        for i, (t, fin) in enumerate(financials_by_ticker.items()):
            r = fin.ocf_capex_ratio()
            if not r:
                continue
            xs = [p.fy for p in r]
            ys = [p.val for p in r]
            ax.plot(
                xs, ys, marker="s", linewidth=2,
                color=_ticker_color(i), label=t, markersize=7,
            )
            for x, y in zip(xs, ys):
                ax.annotate(
                    f"{y:.1f}x", (x, y),
                    xytext=(0, 8), textcoords="offset points",
                    ha="center", fontsize=8, color=_ticker_color(i),
                )
            any_drawn = True

        if not any_drawn:
            import matplotlib.pyplot as plt2
            plt2.close(fig)
            return None

        ax.axhline(y=1.0, color="red", linewidth=1.2, linestyle="--", alpha=0.6, label="OCF = CapEx (break-even)")
        ax.set_xlabel("Fiscal Year")
        ax.set_ylabel("OCF / CapEx Ratio (×)")
        ax.set_title("OCF / CapEx Ratio — Cash Generation vs Reinvestment", fontsize=13, fontweight="bold", pad=12)
        ax.grid(True, alpha=0.25, linestyle="--")
        ax.legend(loc="best", fontsize=9, ncol=min(len(financials_by_ticker) + 1, 5))
        fig.tight_layout()
        return fig
    except Exception:
        log.exception("chart_ocf_capex_ratio 실패")
        return None


def chart_capex_intensity(financials_by_ticker: dict[str, Any]) -> Optional[Any]:
    """CapEx Intensity = CapEx / Revenue (%) line."""
    try:
        plt = _setup_matplotlib()
        fig, ax = plt.subplots(figsize=(11, 6.5))
        any_drawn = False
        for i, (t, fin) in enumerate(financials_by_ticker.items()):
            rev_by_fy = {p.fy: p.val for p in fin.revenue}
            pts = []
            for cp in fin.capex:
                rv = rev_by_fy.get(cp.fy)
                if rv and rv > 0:
                    pts.append((cp.fy, cp.val / rv * 100.0))
            pts.sort()
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            ax.plot(
                xs, ys, marker="o", linewidth=2,
                color=_ticker_color(i), label=t, markersize=7,
            )
            for x, y in zip(xs, ys):
                ax.annotate(
                    f"{y:.1f}%", (x, y),
                    xytext=(0, 8), textcoords="offset points",
                    ha="center", fontsize=8, color=_ticker_color(i),
                )
            any_drawn = True

        if not any_drawn:
            import matplotlib.pyplot as plt2
            plt2.close(fig)
            return None

        ax.set_xlabel("Fiscal Year")
        ax.set_ylabel("CapEx / Revenue (%)")
        ax.set_title("CapEx Intensity — Reinvestment Rate", fontsize=13, fontweight="bold", pad=12)
        ax.grid(True, alpha=0.25, linestyle="--")
        ax.legend(loc="best", fontsize=9, ncol=min(len(financials_by_ticker), 4))
        fig.tight_layout()
        return fig
    except Exception:
        log.exception("chart_capex_intensity 실패")
        return None


def chart_revenue(financials_by_ticker: dict[str, Any]) -> Optional[Any]:
    """Revenue 6년치 grouped bar (참고용)."""
    try:
        plt = _setup_matplotlib()
        from matplotlib.ticker import FuncFormatter
        import numpy as np

        years_set: set[int] = set()
        for fin in financials_by_ticker.values():
            for p in fin.revenue:
                years_set.add(p.fy)
        if not years_set:
            return None
        years = sorted(years_set)[-6:]
        tickers = list(financials_by_ticker.keys())

        fig, ax = plt.subplots(figsize=(11, 6.5))
        n = len(tickers)
        bar_w = 0.8 / max(n, 1)
        xs = np.arange(len(years))

        for i, t in enumerate(tickers):
            by_fy = {p.fy: p.val for p in financials_by_ticker[t].revenue}
            vals = [by_fy.get(fy, 0) for fy in years]
            ax.bar(
                xs + i * bar_w - (bar_w * (n - 1) / 2),
                vals,
                bar_w * 0.95,
                label=t,
                color=_ticker_color(i),
                edgecolor="black",
                linewidth=0.4,
            )

        ax.set_xticks(xs)
        ax.set_xticklabels([f"FY{fy}" for fy in years])
        ax.set_ylabel("Revenue (USD)")
        ax.set_title("Revenue — 6-Year Comparison", fontsize=13, fontweight="bold", pad=12)
        ax.yaxis.set_major_formatter(FuncFormatter(_format_billions))
        ax.grid(True, axis="y", alpha=0.25, linestyle="--")
        ax.legend(loc="best", fontsize=9, ncol=min(n, 4))
        fig.tight_layout()
        return fig
    except Exception:
        log.exception("chart_revenue 실패")
        return None
