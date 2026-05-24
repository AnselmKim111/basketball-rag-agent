"""미국 4대 지수 그리드 + 개별 ETF/테마 캔들 차트."""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def us_indices_grid(dfs: dict[str, object], out_dir: Path, filename: str = "01_us_indices.png",
                    date_iso: str | None = None) -> str | None:
    """미국 4대 지수 2x2. 각 박스 등락률·종가 + 20MA. dfs={label: DataFrame}."""
    theme.setup()
    import matplotlib.pyplot as plt
    items = [(k, dfs[k]) for k in ("DOW", "NASDAQ", "S&P500", "Russell2000") if k in dfs]
    if not items:
        return None
    fig, axes = plt.subplots(2, 2, figsize=(13, 7.3))
    axes = axes.flatten()
    for i, (label, df) in enumerate(items[:4]):
        ax = axes[i]
        close = df["Close"].iloc[-120:]
        last = float(close.iloc[-1]); prev = float(close.iloc[-2]) if len(close) > 1 else last
        chg = (last / prev - 1) * 100 if prev else 0
        color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
        ax.plot(close.index, close.values, color=color, linewidth=1.6)
        if len(df) >= 20:
            ma20 = df["Close"].iloc[-120:].rolling(20).mean()
            ax.plot(ma20.index, ma20.values, color=theme.COLOR_MA[1], linewidth=0.9, alpha=0.8, label="20MA")
        ax.set_title(f"{label}  {last:,.0f}  ({chg:+.2f}%)", fontsize=12, color=color)
        ax.tick_params(labelsize=8)
    for j in range(len(items), 4):
        axes[j].set_axis_off()
    fig.suptitle("미국 4대 지수 (최근 120일)", fontsize=13)
    theme.stamp(axes[min(len(items), 4) - 1], date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def indices_normalized(dfs: dict[str, object], out_dir: Path, filename: str = "01b_indices_norm.png",
                       title: str = "미국 4대 지수 (1년, 100 리베이스)", days: int = 252,
                       date_iso: str | None = None) -> str | None:
    """여러 지수를 100 기준 normalized 비교 라인."""
    from src.report.data.fetch_prices import normalize_100
    theme.setup()
    import matplotlib.pyplot as plt
    if not dfs:
        return None
    fig, ax = plt.subplots(figsize=(12, 6))
    palette = ["#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]
    drawn = 0
    for i, (label, df) in enumerate(dfs.items()):
        if df is None or len(df) < 5:
            continue
        s = normalize_100(df.iloc[-days:])
        if s is None:
            continue
        last = float(s.iloc[-1])
        ax.plot(s.index, s.values, linewidth=1.5, color=palette[i % len(palette)],
                label=f"{label} ({last - 100:+.1f}%)")
        drawn += 1
    if not drawn:
        plt.close(fig); return None
    ax.axhline(100, color="#999", linewidth=0.6, linestyle=":")
    ax.set_title(title, fontsize=13)
    ax.legend(fontsize=8, loc="upper left")
    ax.tick_params(labelsize=8)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def theme_chart(df, label: str, out_dir: Path, filename: str, date_iso: str | None = None,
                days: int = 252) -> str | None:
    """테마/ETF 캔들 차트 — 캔들 + 20/50/200MA + 52주고점선 + 거래량 서브패널 + 자동 주석.

    자동 주석: 신고가 / 정배열 / 52주 위치 / 일변화 배지 + 신고가 돌파 화살표.
    """
    theme.setup()
    import matplotlib.pyplot as plt
    if df is None or len(df) < 20:
        return None
    from src.report.analysis.technical_signals import analyze
    a = analyze(df)
    import numpy as np

    n = min(days, len(df))
    has_vol = "Volume" in df and float(df["Volume"].iloc[-n:].fillna(0).abs().sum()) > 0
    if has_vol:
        fig, (ax, axv) = plt.subplots(
            2, 1, figsize=(12, 6.2), sharex=True,
            gridspec_kw={"height_ratios": [4, 1], "hspace": 0.05})
    else:
        fig, ax = plt.subplots(figsize=(12, 5.5)); axv = None

    theme.candlestick(ax, df, n=n)

    close = df["Close"]
    x = np.arange(n)
    for w, c, name in ((20, theme.COLOR_MA[1], "20"), (50, theme.COLOR_MA[2], "50"), (200, theme.COLOR_MA[3], "200")):
        if len(close) >= w:
            ma = close.rolling(w).mean().iloc[-n:].values
            ax.plot(x, ma, color=c, linewidth=1.0, alpha=0.85, label=f"{name}MA")
    hi = a.get("high_52w")
    if hi:
        ax.axhline(hi, color=theme.COLOR_UP, linestyle="--", linewidth=0.7, alpha=0.5)
    # 신고가 돌파 화살표 (마지막 봉)
    if a.get("is_new_high"):
        last_px = float(close.iloc[-1])
        ax.annotate("돌파", xy=(n - 1, last_px), xytext=(n - 1 - max(n // 12, 3), last_px * 1.04),
                    fontsize=8, color=theme.COLOR_UP, ha="center",
                    arrowprops=dict(arrowstyle="->", color=theme.COLOR_UP, lw=1.2))

    # 거래량 서브패널 (상승=빨강/하락=파랑) + 20일 평균선
    if axv is not None:
        sub = df.iloc[-n:]
        vol = sub["Volume"].fillna(0).values
        o = sub["Open"].values if "Open" in sub else sub["Close"].values
        cvals = sub["Close"].values
        vcolors = [theme.COLOR_UP if cvals[i] >= o[i] else theme.COLOR_DOWN for i in range(len(sub))]
        axv.bar(x, vol, width=0.7, color=vcolors, alpha=0.6)
        if len(df) >= 20:
            vma = df["Volume"].rolling(20).mean().iloc[-n:].values
            axv.plot(x, vma, color="#555555", linewidth=0.8, alpha=0.8)
        axv.set_ylabel("거래량", fontsize=7)
        axv.tick_params(labelsize=6)
        axv.grid(True, alpha=0.15)
        theme.date_xticks(axv, df.index, n=n)
    else:
        theme.date_xticks(ax, df.index, n=n)

    # 주석 배지
    tags = []
    if a.get("is_new_high"):
        tags.append("신고가")
    if a.get("above_ma20") and a.get("above_ma50") and a.get("above_ma200"):
        tags.append("정배열")
    elif a.get("above_ma200") is False:
        tags.append("200선 하회")
    pct52 = a.get("pct_of_52w_high", 0)
    badge = "  ·  ".join(tags) if tags else f"52주 {pct52:.0f}%"
    last = a.get("close", float(close.iloc[-1])); chg = a.get("chg_pct", 0.0)
    color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
    ax.set_title(f"{label}   {last:,.2f}  ({chg:+.2f}%)   [{badge}]", fontsize=12, color=color)
    ax.legend(fontsize=7, loc="upper left", ncol=3)
    ax.tick_params(labelsize=7)
    theme.stamp(axv if axv is not None else ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


# 하위호환: 기존 라인형 etf_chart 호출부 → 캔들형으로 위임
def etf_chart(df, label: str, out_dir: Path, filename: str, date_iso: str | None = None) -> str | None:
    return theme_chart(df, label, out_dir, filename, date_iso=date_iso)
