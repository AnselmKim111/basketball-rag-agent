"""매크로/변동성 라인 차트 — 금리·유가·VIX 등."""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)


def macro_line(df, label: str, out_dir: Path, filename: str) -> str | None:
    """단일 매크로 지표 라인 (최근 180일) + 최근값 주석."""
    theme.setup()
    import matplotlib.pyplot as plt
    if df is None or len(df) < 5:
        return None
    fig, ax = plt.subplots(figsize=(12, 4.5))
    s = df["Close"].iloc[-180:]
    last = float(s.iloc[-1]); prev = float(s.iloc[-2]) if len(s) > 1 else last
    chg = (last / prev - 1) * 100 if prev else 0
    color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
    ax.plot(s.index, s.values, color=color, linewidth=1.5)
    ax.axhline(float(s.iloc[-1]), color="#bbbbbb", linewidth=0.6, linestyle=":")
    ax.set_title(f"{label}  {last:,.2f}  ({chg:+.2f}%)", fontsize=12, color=color)
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def _pick(macro: dict[str, object], *needles: str):
    """라벨에 needle 중 하나가 포함된 첫 시계열(Close, 최근 180일) 반환. 없으면 None."""
    for k, v in macro.items():
        if any(nd in k for nd in needles):
            try:
                s = v["Close"].iloc[-180:]
                if len(s) >= 5:
                    return k, s
            except Exception:
                continue
    return None, None


def _pick_df(macro: dict[str, object], *needles: str):
    """라벨에 needle 포함된 첫 OHLC DataFrame 반환 (최근 180일). 없으면 (None, None)."""
    for k, v in macro.items():
        if any(nd in k for nd in needles):
            try:
                if v is not None and len(v) >= 5:
                    return k, v.iloc[-180:]
            except Exception:
                continue
    return None, None


def _chg(s) -> float:
    last = float(s.iloc[-1]); prev = float(s.iloc[-2]) if len(s) > 1 else last
    return (last / prev - 1) * 100 if prev else 0.0


def _finish(ax, fig, out_dir, filename, date_iso):
    ax.tick_params(labelsize=9)
    ax.legend(fontsize=10, loc="upper left", ncol=3)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def rates_curve(macro: dict[str, object], out_dir: Path,
                filename: str = "20_macro_rates.png", date_iso: str | None = None) -> str | None:
    """미국 금리 곡선 — 3개월/10년/30년 한 차트 + 장단기 스프레드 주석 (침체 신호)."""
    theme.setup()
    import matplotlib.pyplot as plt
    series = []
    for needles, col in ((("3개월", "13주", "2년"), theme.COLOR_MA[1]),
                         (("10년",), theme.COLOR_UP),
                         (("30년",), theme.COLOR_DOWN)):
        k, s = _pick(macro, *needles)
        if s is not None:
            series.append((k, s, col))
    if not series:
        return None
    fig, ax = plt.subplots(figsize=(13, 5.2))
    for k, s, col in series:
        ax.plot(s.index, s.values, color=col, linewidth=1.8, label=f"{k} {float(s.iloc[-1]):.2f}%")
        ax.scatter([s.index[-1]], [float(s.iloc[-1])], color=col, s=24, zorder=5)
    # 장단기 스프레드 (10Y − 단기): 음수면 역전(침체 시그널)
    short = next((s for k, s, _ in series if any(x in k for x in ("3개월", "13주", "2년"))), None)
    ten = next((s for k, s, _ in series if "10년" in k), None)
    note = ""
    if short is not None and ten is not None:
        spread = float(ten.iloc[-1]) - float(short.iloc[-1])
        note = f"  |  10Y−단기 {spread:+.2f}%p {'(역전·침체경계)' if spread < 0 else '(정상)'}"
    ax.set_title(f"미국 국채 금리 곡선 (최근 180일){note}", fontsize=13)
    ax.set_ylabel("수익률 (%)", fontsize=10)
    return _finish(ax, fig, out_dir, filename, date_iso)


def oil_chart(macro: dict[str, object], out_dir: Path,
              filename: str = "21_macro_oil.png", date_iso: str | None = None) -> str | None:
    """유가 — WTI + Brent 한 차트 ($/배럴)."""
    theme.setup()
    import matplotlib.pyplot as plt
    series = []
    for needles, col in ((("WTI",), theme.COLOR_UP), (("Brent",), theme.COLOR_DOWN)):
        k, s = _pick(macro, *needles)
        if s is not None:
            series.append((k, s, col))
    if not series:
        return None
    fig, ax = plt.subplots(figsize=(13, 4.8))
    for k, s, col in series:
        ax.plot(s.index, s.values, color=col, linewidth=1.8,
                label=f"{k} ${float(s.iloc[-1]):,.1f} ({_chg(s):+.1f}%)")
        ax.scatter([s.index[-1]], [float(s.iloc[-1])], color=col, s=24, zorder=5)
    ax.set_title("국제 유가 — WTI · Brent (최근 180일)", fontsize=13)
    ax.set_ylabel("$/배럴", fontsize=10)
    return _finish(ax, fig, out_dir, filename, date_iso)


def vol_chart(macro: dict[str, object], out_dir: Path,
              filename: str = "22_macro_vol.png", date_iso: str | None = None) -> str | None:
    """변동성 — VIX(주식) + OVX(유가) 한 차트. 공포/안도 게이지."""
    theme.setup()
    import matplotlib.pyplot as plt
    series = []
    for needles, col in ((("VIX",), theme.COLOR_DOWN), (("OVX", "유가 변동성"), theme.COLOR_MA[1])):
        k, s = _pick(macro, *needles)
        if s is not None:
            series.append((k, s, col))
    if not series:
        return None
    fig, ax = plt.subplots(figsize=(13, 4.8))
    for k, s, col in series:
        ax.plot(s.index, s.values, color=col, linewidth=1.8,
                label=f"{k} {float(s.iloc[-1]):.1f} ({_chg(s):+.1f}%)")
        ax.scatter([s.index[-1]], [float(s.iloc[-1])], color=col, s=24, zorder=5)
    vk, vs = _pick(macro, "VIX")
    if vs is not None:
        lvl = float(vs.iloc[-1])
        zone = "안도(<15)" if lvl < 15 else ("경계(15~25)" if lvl < 25 else "공포(>25)")
        ax.axhline(20, color="#bbbbbb", linewidth=0.7, linestyle=":")
        ax.set_title(f"변동성 게이지 — VIX {lvl:.1f} {zone}", fontsize=13)
    else:
        ax.set_title("변동성 게이지 (최근 180일)", fontsize=13)
    return _finish(ax, fig, out_dir, filename, date_iso)


def single_macro(macro: dict[str, object], out_dir: Path, filename: str,
                 *needles: str, date_iso: str | None = None) -> str | None:
    """단일 매크로 지표 큰 일봉 캔들 차트 (달러 인덱스·비트코인 등). OHLC 없으면 라인 폴백."""
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    k, df = _pick_df(macro, *needles)
    if df is None:
        return None
    close = df["Close"]
    chg = _chg(close)
    color = theme.COLOR_UP if chg >= 0 else theme.COLOR_DOWN
    fig, ax = plt.subplots(figsize=(13, 4.6))
    n = len(df)
    has_ohlc = all(c in df.columns for c in ("Open", "High", "Low"))
    if has_ohlc:
        theme.candlestick(ax, df, n=n)
        if n >= 20:
            ax.plot(np.arange(n), close.rolling(20).mean().iloc[-n:].values,
                    color=theme.COLOR_MA[1], linewidth=1.0, alpha=0.85, label="20MA")
        theme.date_xticks(ax, df.index, n=n)
    else:
        ax.plot(close.index, close.values, color=color, linewidth=1.8)
        ax.fill_between(close.index, close.values, float(close.min()), color=color, alpha=0.06)
    ax.set_title(f"{k}  {float(close.iloc[-1]):,.2f}  ({chg:+.2f}%)", fontsize=13, color=color)
    ax.tick_params(labelsize=9)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
