"""Minimal mode 전용 차트 — 신고가 종목 grid + 섹터 상대강도 표.

기존 차트와 분리 (legacy _build_report 미영향). 모바일 우선 figsize.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from . import chart_theme as theme

log = logging.getLogger(__name__)


def new_highs_grid(out_dir: Path, date_iso: str,
                   us_limit: int = 12, kr_limit: int = 8) -> str | None:
    """US/KR screener.db에서 today 신고가(high_52w) 종목 가격 차트 grid.

    각 종목은 최근 ~180일 가격 라인 + 52w 신고가 마커. 모바일 가독성 위해
    종목당 큰 mini chart (4열 × N행).
    """
    from src.report.data import screener_adapter

    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec

    # 1. 신고가 종목 수집 (US 먼저, 부족하면 KR로 채움)
    items: list[tuple[str, str, dict]] = []  # (market, name, payload)
    try:
        us_sig = screener_adapter.load_signals("US")
        for entry in (us_sig.get("high_52w", []) or [])[:us_limit]:
            items.append(("US", entry["name"], entry))
    except Exception:
        log.exception("[minimal.new_highs_grid] US load 실패")
    try:
        kr_sig = screener_adapter.load_signals("KR")
        for entry in (kr_sig.get("high_52w", []) or [])[:kr_limit]:
            items.append(("KR", entry["name"], entry))
    except Exception:
        log.exception("[minimal.new_highs_grid] KR load 실패")

    if not items:
        log.warning("[minimal.new_highs_grid] 신고가 종목 0 — skip")
        return None

    # 2. 각 종목 OHLCV 로드
    enriched: list[tuple[str, str, dict, object]] = []
    for mkt, name, payload in items:
        try:
            df = screener_adapter.load_ohlcv_df(payload["ticker"], mkt, days=180)
        except Exception:
            df = None
        if df is None or len(df) < 30:
            continue
        enriched.append((mkt, name, payload, df))
        if len(enriched) >= 16:  # 모바일 보기에 16종 cap
            break

    if not enriched:
        log.warning("[minimal.new_highs_grid] OHLCV 로드 0건 — skip")
        return None

    # 3. grid 그리기 (4열, 행은 종목 수 / 4)
    cols = 4
    rows = (len(enriched) + cols - 1) // cols
    fig = plt.figure(figsize=(theme.SQUARE_FIGSIZE[0], 3.2 * rows))
    fig.suptitle(f"🚀 신고가 종목 — {date_iso} (US {sum(1 for x in enriched if x[0]=='US')} · KR {sum(1 for x in enriched if x[0]=='KR')})",
                 fontsize=16, fontweight="bold", y=0.995)

    gs = GridSpec(rows, cols, figure=fig, hspace=0.55, wspace=0.30, top=0.93, bottom=0.04)

    for i, (mkt, name, payload, df) in enumerate(enriched):
        r, c = divmod(i, cols)
        ax = fig.add_subplot(gs[r, c])
        # 가격 라인 (close)
        try:
            closes = df["close"].astype(float).values
        except Exception:
            closes = df.iloc[:, -1].astype(float).values
        ax.plot(range(len(closes)), closes, color="#2563eb", linewidth=1.8)
        # 신고가 마커
        last = float(closes[-1])
        prev_hi = float(payload.get("prev_high") or max(closes[:-1] or [last]))
        ax.axhline(prev_hi, color="#ef4444", linestyle="--", linewidth=1.0, alpha=0.6)
        # 라벨
        flag = "🇺🇸" if mkt == "US" else "🇰🇷"
        pct = float(payload.get("pct") or 0)
        ax.set_title(f"{flag} {name} ({payload['ticker']})\n{last:,.2f} (+{pct:.1f}% vs 52w hi)",
                     fontsize=11, pad=4)
        ax.grid(True, alpha=0.25)
        ax.tick_params(labelsize=8)

    out = "07_new_highs.png"
    theme.save_fig(fig, out_dir, out)
    log.info("[minimal.new_highs_grid] %d종 차트 저장", len(enriched))
    return out


def sector_relative_strength_table(theme_rows: list[dict], out_dir: Path,
                                    date_iso: str) -> str | None:
    """섹터별 1D/5D/1M 상대강도 표 (SPY 대비). 가격 차트 대신 정량 표.

    theme_rows: [{label, r1d, r5d, r1m, ...}] (theme_momentum.compute 출력)
    """
    if not theme_rows:
        log.warning("[minimal.sector_rs_table] theme_rows 비어있음 — skip")
        return None

    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    # SPY baseline 추출
    spy = next((r for r in theme_rows if (r.get("label") or "").upper() in ("SPY", "S&P 500")), None)
    spy_1d = float(spy.get("r1d") or 0) if spy else 0
    spy_5d = float(spy.get("r5d") or 0) if spy else 0
    spy_1m = float(spy.get("r1m") or 0) if spy else 0

    # SPY 제외 + 5D 내림차순
    rows = [r for r in theme_rows if r is not spy and r.get("label")]
    rows.sort(key=lambda r: -(r.get("r5d") or 0))

    # 표 데이터: [라벨, 1D, 5D, 1M, RS(5D vs SPY)]
    headers = ["섹터/테마", "1D %", "5D %", "1M %", "5D vs SPY"]
    data = []
    for r in rows[:20]:  # 최대 20행
        label = r.get("label", "")[:20]
        r1d = float(r.get("r1d") or 0)
        r5d = float(r.get("r5d") or 0)
        r1m = float(r.get("r1m") or 0)
        rs = r5d - spy_5d
        data.append([label, f"{r1d:+.2f}", f"{r5d:+.2f}", f"{r1m:+.2f}", f"{rs:+.2f}"])

    fig, ax = plt.subplots(figsize=theme.PORTRAIT_FIGSIZE)
    ax.axis("off")
    ax.set_title(f"📊 섹터·테마 상대강도 — {date_iso}  (SPY 5D = {spy_5d:+.2f}%)",
                 fontsize=15, fontweight="bold", pad=12)

    tbl = ax.table(cellText=data, colLabels=headers, loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(11)
    tbl.scale(1.0, 1.7)  # 모바일 — 행 높이 확대

    # 색상 — 양수=빨강(한국식)·음수=청색  · RS 컬럼은 진하게
    cmap_pos = LinearSegmentedColormap.from_list("pos", ["#fff", "#dc2626"])
    cmap_neg = LinearSegmentedColormap.from_list("neg", ["#fff", "#2563eb"])
    for i, row in enumerate(data, start=1):  # row 0 = header
        for j, val_str in enumerate(row):
            if j == 0:
                continue
            try:
                v = float(val_str.replace("+", "").replace("%", ""))
            except ValueError:
                continue
            cell = tbl[(i, j)]
            if v > 0:
                cell.set_facecolor(cmap_pos(min(1.0, abs(v) / 8)))
            elif v < 0:
                cell.set_facecolor(cmap_neg(min(1.0, abs(v) / 8)))
            if j == 4:  # RS 컬럼 강조
                cell.set_text_props(weight="bold")

    # header 스타일
    for j in range(len(headers)):
        cell = tbl[(0, j)]
        cell.set_facecolor("#1e293b")
        cell.set_text_props(color="white", weight="bold")

    out = "03_sector_rs_table.png"
    theme.save_fig(fig, out_dir, out)
    log.info("[minimal.sector_rs_table] %d행 표 저장", len(data))
    return out
