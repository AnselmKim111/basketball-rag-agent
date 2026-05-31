"""§6 개별 종목 하이라이트 — watchlist 5-stream 통합 차트.

새 시그니처: highlight_grid(dfs, out_dir, *, watchlist_us, watchlist_kr, ...).
- dfs (기존 호환): {label: DataFrame} — 추가 캔들 차트 데이터 (없으면 watchlist만으로 OHLCV 자동 fetch)
- watchlist_us / watchlist_kr: report/data/watchlist.build_watchlist 결과 list[dict]

각 셀: 캔들 + 20MA + 카테고리 배지(📈 어닝, 💎 IPO, 🚀 섹터리더, 🔄 반전, 🔁 F/U) + caption.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)

_CATEGORY_BADGE = {
    "earnings_actual": "📈 어닝",
    "estimate_revision": "🔭 기대",
    "ipo": "💎 IPO",
    "sector_leader": "🚀 리더",
    "trend_reversal": "🔄 반전",
    "followup": "🔁 F/U",
}


def _primary_badge(item: dict) -> str:
    cats = item.get("categories") or []
    # 우선순위: ipo > earnings_actual > trend_reversal > sector_leader > estimate_revision > followup
    for k in ("ipo", "earnings_actual", "trend_reversal", "sector_leader",
              "estimate_revision", "followup"):
        if k in cats:
            return _CATEGORY_BADGE[k]
    return ""


def _caption(item: dict) -> str:
    """카탈리스트 caption 한 줄."""
    ea = item.get("earnings_actual") or {}
    if ea.get("eps_surprise_pct") is not None:
        eps_s = ea.get("eps_surprise_pct")
        rev_s = ea.get("rev_surprise_pct")
        asof = ea.get("asof") or ""
        bits = [f"EPS {eps_s:+.1f}%"]
        if rev_s is not None:
            bits.append(f"Rev {rev_s:+.1f}%")
        if asof:
            bits.append(asof)
        return " · ".join(bits)
    ipo = item.get("ipo") or {}
    if ipo.get("status"):
        bits = [f"IPO {ipo.get('status')}"]
        if ipo.get("date"):
            bits.append(str(ipo["date"]))
        if ipo.get("price"):
            bits.append(f"${ipo.get('price')}")
        return " · ".join(bits)
    rev = item.get("estimate_revision") or {}
    if rev.get("buy_delta"):
        return f"분석가 buy {rev.get('buy_count_old')}→{rev.get('buy_count_new')}"
    sigs = item.get("screener_signals") or []
    if sigs:
        return " · ".join(sigs[:2])
    return ""


def _resolve_df(item: dict, extra_dfs: dict):
    """OHLCV df — extra_dfs(기존 label match) → screener_adapter → fetch_prices.fetch_ohlcv 순."""
    label = item.get("label") or ""
    ticker = item.get("ticker") or ""
    # label match (e.g. 기존 highlights_df에 "NVIDIA(NVDA)" 같은 key)
    for k, v in (extra_dfs or {}).items():
        if v is None:
            continue
        if k == label or ticker in k:
            return v
    # adapter (KR/US)
    try:
        from src.report.data.screener_adapter import load_ohlcv_df
        df = load_ohlcv_df(ticker, item.get("market") or "US", days=260)
        if df is not None and len(df) >= 30:
            return df
    except Exception:
        pass
    # fetch_prices 폴백
    try:
        from src.report.data.fetch_prices import fetch_ohlcv
        df = fetch_ohlcv(ticker, days=260)
        if df is not None and len(df) >= 30:
            return df
    except Exception:
        pass
    return None


def _render_grid(items: list[dict], dfs_map: dict, out_dir: Path, filename: str,
                 title: str, days: int = 120, date_iso: str | None = None) -> str | None:
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    drawable = []
    for it in items:
        df = _resolve_df(it, dfs_map)
        if df is None or len(df) < 6:
            continue
        drawable.append((it, df))
    if not drawable:
        return None
    cols = 2
    rows = (len(drawable) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(13, 3.6 * rows))
    axes = axes.flatten() if rows * cols > 1 else [axes]
    for i, (it, df) in enumerate(drawable):
        ax = axes[i]
        n = min(days, len(df))
        close = df["Close"]
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else last
        chg = (last / prev - 1) * 100 if prev else 0
        color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
        theme.candlestick(ax, df, n=n)
        if len(close) >= 20:
            ma20 = close.rolling(20).mean().iloc[-n:].values
            ax.plot(np.arange(n), ma20, color=theme.COLOR_MA[1], linewidth=0.8, alpha=0.85)
        badge = _primary_badge(it)
        ticker = it.get("ticker") or ""
        label = it.get("label") or ticker
        title_text = f"{badge} {label} ({chg:+.2f}%)" if badge else f"{label} ({chg:+.2f}%)"
        ax.set_title(title_text, fontsize=9, color=color)
        cap = _caption(it)
        if cap:
            ax.text(0.02, 0.04, cap, transform=ax.transAxes, fontsize=7, color="#666",
                    verticalalignment="bottom", horizontalalignment="left")
        ax.tick_params(labelsize=6)
        theme.date_xticks(ax, df.index, n=n, count=4)
    for j in range(len(drawable), len(axes)):
        axes[j].set_axis_off()
    fig.suptitle(title, fontsize=13)
    theme.stamp(axes[len(drawable) - 1], date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def highlight_grid(dfs: dict[str, object] | None, out_dir: Path,
                   filename: str = "30_highlights.png", days: int = 120,
                   date_iso: str | None = None,
                   watchlist_us: list[dict] | None = None,
                   watchlist_kr: list[dict] | None = None):
    """미국·한국 분리 2장 (US: 30_highlights_us.png · KR: 31_highlights_kr.png).

    호출자가 list 형태로 받으면 두 차트 모두 PDF에 삽입됨.
    watchlist가 비면 기존 dfs로 단일 폴백 차트 (이전 동작).
    """
    us_items = watchlist_us or []
    kr_items = watchlist_kr or []
    paths: list[str] = []
    if us_items:
        p = _render_grid(us_items, dfs or {}, out_dir, "30_highlights_us.png",
                         "🇺🇸 미국 — 개별 종목 하이라이트 (watchlist)",
                         days=days, date_iso=date_iso)
        if p:
            paths.append(p)
    if kr_items:
        p = _render_grid(kr_items, dfs or {}, out_dir, "31_highlights_kr.png",
                         "🇰🇷 한국 — 개별 종목 하이라이트 (watchlist)",
                         days=days, date_iso=date_iso)
        if p:
            paths.append(p)
    if not paths and dfs:
        # 폴백: 기존 동작
        items = [(k, v) for k, v in (dfs or {}).items() if v is not None and len(v) > 5]
        if items:
            theme.setup()
            import matplotlib.pyplot as plt
            import numpy as np
            cols = 2
            rows = (len(items) + cols - 1) // cols
            fig, axes = plt.subplots(rows, cols, figsize=(13, 3.4 * rows))
            axes = axes.flatten() if rows * cols > 1 else [axes]
            for i, (label, df) in enumerate(items):
                ax = axes[i]
                n = min(days, len(df))
                close = df["Close"]
                last = float(close.iloc[-1])
                prev = float(close.iloc[-2]) if len(close) > 1 else last
                chg = (last / prev - 1) * 100 if prev else 0
                color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
                theme.candlestick(ax, df, n=n)
                if len(close) >= 20:
                    ma20 = close.rolling(20).mean().iloc[-n:].values
                    ax.plot(np.arange(n), ma20, color=theme.COLOR_MA[1], linewidth=0.8, alpha=0.85)
                ax.set_title(f"{label}  ({chg:+.2f}%)", fontsize=9, color=color)
                ax.tick_params(labelsize=6)
                theme.date_xticks(ax, df.index, n=n, count=4)
            for j in range(len(items), len(axes)):
                axes[j].set_axis_off()
            fig.suptitle("개별 종목 하이라이트 (일봉, 최근 120일)", fontsize=13)
            theme.stamp(axes[len(items) - 1], date_iso)
            fig.tight_layout()
            fb = theme.save_fig(fig, out_dir, filename)
            return fb
    return paths if paths else None
