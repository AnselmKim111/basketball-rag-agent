"""단일 종목 차트 — 1년치 일봉 캔들 + 이동평균 + 거래량 (채널 게시용).

report 차트 테마(chart_theme) 재사용. PNG bytes 반환.
rows: db.load_ohlcv 형식. 한국 OHLCV는 원 단위 정수(미장의 cent×100과 다름) → /100 안 함.
"""
from __future__ import annotations

import logging
from io import BytesIO

log = logging.getLogger(__name__)


def render_candle_volume(ticker: str, rows: list[dict], title: str | None = None,
                         days: int = 252) -> bytes | None:
    """1년 일봉 캔들+MA+거래량 PNG bytes. 데이터 부족/실패 시 None."""
    if not rows or len(rows) < 20:
        return None
    try:
        from src.report.charts import chart_theme as theme
        theme.setup()
        import matplotlib.pyplot as plt
        import pandas as pd

        sub = rows[-days:]
        idx = pd.to_datetime([r["date"] for r in sub])
        df = pd.DataFrame({
            "Open": [r["open"] for r in sub],
            "High": [r["high"] for r in sub],
            "Low": [r["low"] for r in sub],
            "Close": [r["close"] for r in sub],
            "Volume": [r["volume"] for r in sub],
        }, index=idx)

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(11, 7), gridspec_kw={"height_ratios": [3, 1]}, sharex=True)

        theme.candlestick(ax1, df)
        closes = df["Close"]
        for w, col in zip((20, 50, 200), theme.COLOR_MA[1:]):
            if len(closes) >= w:
                ax1.plot(range(len(df)), closes.rolling(w).mean().values,
                         color=col, linewidth=1.0, label=f"{w}MA")
        hi = float(df["High"].max())
        ax1.axhline(hi, color=theme.COLOR_UP, linestyle="--", linewidth=0.7, alpha=0.5)
        ax1.legend(loc="upper left", fontsize=7, ncol=4)
        ax1.set_ylabel("가격(원)")
        ax1.set_title(title or ticker, fontsize=12)
        ax1.grid(True, alpha=0.2)
        ax1.ticklabel_format(axis="y", style="plain")

        colors = [theme.COLOR_UP if c >= o else theme.COLOR_DOWN
                  for o, c in zip(df["Open"], df["Close"])]
        ax2.bar(range(len(df)), df["Volume"].values, color=colors, width=0.8, alpha=0.8)
        ax2.set_ylabel("거래량")
        ax2.grid(True, alpha=0.2)
        theme.date_xticks(ax2, df.index)

        fig.tight_layout()
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception:
        log.exception("[screener.chart] %s 렌더 실패", ticker)
        return None
