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
                         date_iso: str | None = None,
                         gauge_score: int | None = None,
                         gauge_label: str | None = None,
                         fx_score: int | None = None,
                         fx_label: str | None = None) -> str | None:
    """Sankey식 자금흐름도 (TV 팔레트). 흐름 굵기 = 강도.

    sources: [(label, strength)] 자금 이탈처 (적색)
    destinations: [(label, strength)] 자금 유입처 (녹색)
    gauge_score/label: 글로벌 Risk-On/Off 점수.
    fx_score/label: §4 환전 압력 점수 (한국).
    """
    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
    if not sources or not destinations:
        return None

    def _norm(items):
        vals = [abs(s) for _, s in items] or [1]
        m = max(vals) or 1
        return [(lbl, abs(s) / m, s) for lbl, s in items]

    src = _norm(sources)
    dst = _norm(destinations)

    n = max(len(src), len(dst))
    fig, ax = plt.subplots(figsize=(14, max(7, 0.95 * n + 2)))
    ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")

    # TV 팔레트
    C_OUT = "#f7525f"  # 적: 자금 이탈
    C_IN = "#22ab94"   # 녹: 자금 유입

    hub = (5.0, 5.0)
    # 중앙 허브 — Risk + FX 게이지 통합
    hub_lines = []
    if gauge_score is not None:
        hub_lines.append(f"Risk {gauge_score}/100")
        hub_lines.append(gauge_label or "")
    if fx_score is not None:
        hub_lines.append(f"FX압력 {fx_score}/100")
        hub_lines.append(fx_label or "")
    if not hub_lines:
        hub_lines = ["자금", "재편"]
    hub_text = "\n".join([l for l in hub_lines if l])
    hub_fs = 10 if len(hub_lines) >= 3 else 12
    ax.add_patch(FancyBboxPatch((4.05, 4.0), 1.9, 2.0, boxstyle="round,pad=0.08",
                 facecolor="#2a2e39", edgecolor="#888", linewidth=1.5))
    ax.text(hub[0], hub[1], hub_text, ha="center", va="center", color="white",
            fontsize=hub_fs, fontweight="bold")

    def _ys(n):
        if n == 1:
            return [5.0]
        return list(__import__("numpy").linspace(8.7, 1.3, n))

    sy = _ys(len(src))
    for (lbl, w, raw), y in zip(src, sy):
        ax.add_patch(FancyBboxPatch((0.2, y - 0.40), 3.5, 0.8, boxstyle="round,pad=0.06",
                     facecolor=C_OUT, alpha=0.22, edgecolor=C_OUT, linewidth=1.2))
        ax.text(1.95, y, lbl, ha="center", va="center", fontsize=10.5,
                fontweight="bold", color="#222")
        # Sankey식 굵은 흐름 (강도 비례 굵기)
        ax.add_patch(FancyArrowPatch((3.7, y), (4.05, hub[1]), arrowstyle="-|>",
                     mutation_scale=16, linewidth=1.5 + 8 * w, color=C_OUT, alpha=0.78))
    ax.text(1.95, 9.55, "자금 이탈", ha="center", fontsize=13,
            color=C_OUT, fontweight="bold")

    dy = _ys(len(dst))
    for (lbl, w, raw), y in zip(dst, dy):
        ax.add_patch(FancyBboxPatch((6.3, y - 0.40), 3.5, 0.8, boxstyle="round,pad=0.06",
                     facecolor=C_IN, alpha=0.22, edgecolor=C_IN, linewidth=1.2))
        ax.text(8.05, y, lbl, ha="center", va="center", fontsize=10.5,
                fontweight="bold", color="#222")
        ax.add_patch(FancyArrowPatch((5.95, hub[1]), (6.3, y), arrowstyle="-|>",
                     mutation_scale=16, linewidth=1.5 + 8 * w, color=C_IN, alpha=0.82))
    ax.text(8.05, 9.55, "자금 유입", ha="center", fontsize=13,
            color=C_IN, fontweight="bold")

    ax.set_title("종합 자금흐름 — 어디서 빠져 어디로 가는가  (굵기 = 5D 강도)", fontsize=14)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def theme_flow_timeline_chart(theme_rows: list[dict], theme_dfs: dict, out_dir: Path,
                              filename: str = "35_flow_timeline.png",
                              days: int = 20, top_n: int = 6,
                              date_iso: str | None = None) -> str | None:
    """좌: 유입(5D 상위 N) 누적 라인 · 우: 이탈(5D 하위 N) 누적 라인.

    "어디서 언제부터 빠져 어디로 갔는지" 시간축에서 추적.
    """
    from src.report.analysis.theme_momentum import theme_flow_timeline
    theme.setup()
    import matplotlib.pyplot as plt

    rows = [r for r in theme_rows if r.get("r5d") is not None]
    if len(rows) < top_n:
        return None
    rows_s = sorted(rows, key=lambda r: r["r5d"])
    bottoms = rows_s[:top_n]
    tops = list(reversed(rows_s[-top_n:]))

    series = theme_flow_timeline(theme_dfs, days=days)
    if not series:
        return None

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(15, 6.4))
    palette_up = ["#22ab94", "#0d9e74", "#3dc7ad", "#2e7d63", "#16b585", "#5fc7a3"]
    palette_dn = ["#f7525f", "#c93747", "#e0524e", "#a82a35", "#d04550", "#b73c47"]

    panels = [
        (axL, tops, palette_up, f"자금 유입 (5D 상위 {top_n})"),
        (axR, bottoms, palette_dn, f"자금 이탈 (5D 하위 {top_n})"),
    ]
    for ax, group, palette, title_txt in panels:
        any_drawn = False
        for i, r in enumerate(group):
            s = series.get(r["label"])
            if s is None or len(s) < 2:
                continue
            color = palette[i % len(palette)]
            ax.plot(s.index, s.values, color=color, linewidth=2.0,
                    label=f"{r['label']} (5D {r['r5d']:+.1f}%)")
            ax.scatter([s.index[-1]], [float(s.iloc[-1])], color=color, s=42, zorder=5)
            any_drawn = True
        if not any_drawn:
            continue
        ax.axhline(0, color="#999", linewidth=0.7, linestyle=":")
        ax.set_title(title_txt, fontsize=13)
        ax.legend(fontsize=8.5, loc="upper left")
        ax.tick_params(labelsize=8)
        ax.set_ylabel("누적 수익률 (%)", fontsize=9)
        ax.grid(True, alpha=0.22)
        theme.stamp(ax, date_iso)

    fig.suptitle(f"테마 자금 흐름 시계열 — 최근 {days}영업일 누적", fontsize=14)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


# 미국 테마(키워드) → 한국 수혜주 매핑 (레퍼런스 + 일반 대응)
US_KR_LINKAGE = {
    "반도체": ["삼성전자", "SK하이닉스", "한미반도체"],
    "우주": ["한화에어로스페이스", "한국항공우주", "컨텍", "인텔리안테크"],
    "연료전지": ["두산퓨얼셀", "에스퓨얼셀", "범한퓨얼셀"],
    "수소": ["두산퓨얼셀", "에스퓨얼셀"],
    "방산": ["LIG넥스원", "한화에어로스페이스", "현대로템"],
    "사이버": ["안랩", "윈스", "샌즈랩"],
    "양자": ["우리로", "엑스게이트", "코위버"],
    "로봇": ["레인보우로보틱스", "두산로보틱스", "에스피지"],
    "AI": ["레인보우로보틱스", "두산로보틱스"],
    "태양광": ["OCI홀딩스", "한화솔루션", "HD현대에너지솔루션"],
    "신재생": ["한화솔루션", "씨에스윈드"],
    "원전": ["두산에너빌리티", "한전기술", "우진"],
    "우라늄": ["두산에너빌리티", "한전기술"],
    "리튬": ["에코프로비엠", "포스코퓨처엠", "LG에너지솔루션"],
    "2차전지": ["에코프로비엠", "삼성SDI", "포스코퓨처엠"],
    "바이오": ["알테오젠", "HLB", "리가켐바이오", "디앤디파마텍", "펩트론"],
}


def _rate_tier(r5d: float) -> str:
    """5D 등락 → ★ 등급. ≥15%=★★★ · ≥8%=★★ · ≥5%=★ · 그 외=빈문자."""
    if r5d is None:
        return ""
    if r5d >= 15:
        return "★★★"
    if r5d >= 8:
        return "★★"
    if r5d >= 5:
        return "★"
    return ""


_KR_5D_CACHE: dict[str, float] = {}
_KR_5D_CACHE_TS: float = 0.0
_KR_5D_TTL = 1800  # 30분


def _fetch_kr_5d(tickers: list[str]) -> dict[str, float]:
    """ticker → 5D 등락%. screener_adapter 우선 → fetch_prices.fetch_ohlcv 폴백."""
    global _KR_5D_CACHE, _KR_5D_CACHE_TS
    import time
    now = time.time()
    if (now - _KR_5D_CACHE_TS) >= _KR_5D_TTL:
        _KR_5D_CACHE = {}
        _KR_5D_CACHE_TS = now
    out: dict[str, float] = {}
    miss: list[str] = []
    for t in tickers:
        if t in _KR_5D_CACHE:
            out[t] = _KR_5D_CACHE[t]
        else:
            miss.append(t)
    if not miss:
        return out
    try:
        from src.report.data import screener_adapter
        from src.report.data.fetch_prices import fetch_ohlcv
        for tk in miss:
            df = screener_adapter.load_ohlcv_df(tk, "KR", days=15)
            if df is None or len(df) < 6:
                try:
                    df = fetch_ohlcv(tk, days=15)
                except Exception:
                    df = None
            if df is None or len(df) < 6:
                continue
            try:
                close = df["Close"]
                last = float(close.iloc[-1]); ref = float(close.iloc[-6])
                if ref:
                    chg = (last / ref - 1) * 100
                    out[tk] = chg
                    _KR_5D_CACHE[tk] = chg
            except Exception:
                continue
    except Exception:
        pass
    return out


def us_korea_linkage(theme_rows: list[dict], out_dir: Path, filename: str = "33_us_kr_linkage.png",
                     top_n: int = 9, date_iso: str | None = None) -> str | None:
    """미국 테마 강도(5D) → 한국 수혜주 연결 다이어그램.
    좌: 미국 테마 + ★ 등급 / 우: 한국 수혜주 + 5D 등락 / 화살표: 강도 두께·색상.
    """
    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
    from matplotlib import colors

    def _match(label: str):
        for kw, names in US_KR_LINKAGE.items():
            if kw in label:
                return kw, names
        return None, None

    mapped = []
    for r in theme_rows:
        kw, names = _match(r["label"])
        if kw and r.get("r5d") is not None:
            mapped.append((r["label"], r["r5d"], names))
    if not mapped:
        return None
    mapped = sorted(mapped, key=lambda m: -abs(m[1]))[:top_n]
    mapped = sorted(mapped, key=lambda m: -m[1])

    # 한국 종목 5D 등락 일괄 fetch (ticker 변환 후)
    from src.report.data import kr_theme_linkage as kr_link
    all_tickers: list[str] = []
    for _, _, names in mapped:
        for nm in names[:3]:
            tkr = kr_link.KR_NAME_TO_TICKER.get(nm)
            if tkr and tkr not in all_tickers:
                all_tickers.append(tkr)
    kr_5d = _fetch_kr_5d(all_tickers)

    norm = colors.Normalize(vmin=-6, vmax=6)
    cmap = colors.LinearSegmentedColormap.from_list("krrb", ["#1f77b4", "#cfd8dc", "#d62728"])
    mx = max(abs(m[1]) for m in mapped) or 1.0

    fig, ax = plt.subplots(figsize=(14, max(5.5, 1.0 * len(mapped) + 1.5)))
    ax.set_xlim(0, 10); ax.set_ylim(0, len(mapped)); ax.axis("off")
    for i, (label, r5d, names) in enumerate(mapped):
        y = len(mapped) - i - 0.5
        col = cmap(norm(max(-6, min(6, r5d))))
        tier = _rate_tier(r5d)
        # 미국 테마 박스 (좌) + 별 등급
        ax.add_patch(FancyBboxPatch((0.2, y - 0.40), 3.4, 0.80, boxstyle="round,pad=0.05",
                     facecolor=col, edgecolor="#666", linewidth=0.6))
        head = f"{label}  {tier}".strip()
        ax.text(1.9, y + 0.13, head, ha="center", va="center", fontsize=9,
                fontweight="bold", color="white" if abs(r5d) > 3 else "#111")
        ax.text(1.9, y - 0.16, f"5D {r5d:+.1f}%", ha="center", va="center", fontsize=8,
                color="white" if abs(r5d) > 3 else "#222")
        # 화살표
        w = 0.8 + 4 * (abs(r5d) / mx)
        ax.add_patch(FancyArrowPatch((3.7, y), (5.7, y), arrowstyle="-|>", mutation_scale=14,
                     linewidth=w, color=col, alpha=0.7))
        # 한국 수혜주 박스 (우) — 종목명 + 5D 등락
        ax.add_patch(FancyBboxPatch((5.8, y - 0.40), 4.0, 0.80, boxstyle="round,pad=0.05",
                     facecolor="#fafafa", edgecolor=col, linewidth=1.2))
        parts = []
        for nm in names[:3]:
            tkr = kr_link.KR_NAME_TO_TICKER.get(nm)
            chg = kr_5d.get(tkr) if tkr else None
            if chg is not None:
                color_marker = "+" if chg >= 0 else ""
                parts.append(f"{nm} {color_marker}{chg:.1f}%")
            else:
                parts.append(f"{nm} (N/A)")
        ax.text(7.8, y, "\n".join(parts), ha="center", va="center", fontsize=8, color="#111")
    ax.text(1.9, len(mapped) + 0.05, "미국 테마 (5D 강도 · ★ 등급)", ha="center",
            fontsize=10, fontweight="bold")
    ax.text(7.8, len(mapped) + 0.05, "한국 수혜주 (5D 등락)", ha="center",
            fontsize=10, fontweight="bold")
    ax.set_title("미국 → 한국 자금흐름 연결 — ★★★ ≥15%·★★ ≥8%·★ ≥5%", fontsize=13)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def earnings_calendar_grid(upcoming: list[dict], out_dir: Path,
                           filename: str = "35_earnings_calendar.png",
                           date_iso: str | None = None,
                           hot_tickers: set[str] | None = None) -> str | None:
    """다가올 어닝 발표 캘린더 — 시총 가로bar + 발표일/EPS est/revision 색상.

    upcoming: [{symbol, date, eps_est, revenue_est, market_cap?, revision?, source}]
    bar 색상: revision.buy_delta ≥+2 = 녹색 / ≤-2 = 빨강 / 무변화·없음 = 회색.
    hot_tickers: hot 테마 매칭 종목 set — outline 두께/색상 강조 (thesis-relevant).
    """
    hot_tickers = hot_tickers or set()
    if not upcoming:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    from datetime import date as _date

    # 시총 큰 순, 최대 12개
    items = [u for u in upcoming if u.get("symbol")]
    items.sort(key=lambda u: -(u.get("market_cap") or 0))
    items = items[:12]
    if not items:
        return None

    today = None
    if date_iso:
        try:
            today = _date.fromisoformat(date_iso)
        except Exception:
            today = None
    today = today or _date.today()

    def _color(rev: dict | None) -> str:
        if not rev:
            return "#9e9e9e"
        bd = rev.get("buy_delta") or 0
        if bd >= 2:
            return "#2e7d32"
        if bd <= -2:
            return "#c62828"
        return "#9e9e9e"

    def _cap_b(v) -> float:
        try:
            return float(v) / 1e9
        except Exception:
            return 0.0

    caps = [_cap_b(u.get("market_cap") or u.get("revenue_est") or 0) for u in items]
    syms = [u.get("symbol") or "" for u in items]
    colors = [_color(u.get("revision")) for u in items]

    fig, ax = plt.subplots(figsize=(13, max(4.5, 0.55 * len(items) + 1.8)))
    y = np.arange(len(items))
    # hot 테마 매칭 종목은 outline 두꺼운 노란색 강조
    edge_colors = ["#fbc02d" if syms[i] in hot_tickers else "#333" for i in range(len(syms))]
    edge_widths = [2.2 if syms[i] in hot_tickers else 0.4 for i in range(len(syms))]
    ax.barh(y, caps, color=colors, edgecolor=edge_colors, linewidth=edge_widths, alpha=0.85)
    ax.invert_yaxis()
    ax.set_xlabel("시총 ($B)  ·  바 색상: 분석가 buy 변화 (녹=+2이상 · 적=-2이하 · 회=무변화)",
                  fontsize=9)
    ax.set_yticks(y)
    ax.set_yticklabels(syms, fontsize=10, fontweight="bold")
    ax.grid(axis="x", linestyle=":", alpha=0.4)

    # 우측 annotation — 발표일 + EPS est + revenue est + revision 요약
    mx = max(caps) if caps else 1.0
    for i, u in enumerate(items):
        date_s = u.get("date") or ""
        d_n = ""
        try:
            d = _date.fromisoformat(date_s)
            delta = (d - today).days
            d_n = f"D{delta:+d}"
        except Exception:
            pass
        eps = u.get("eps_est")
        rev = u.get("revenue_est") or 0
        rev_b = rev / 1e9 if rev else 0
        eps_s = f"EPS est ${eps:.2f}" if eps is not None else ""
        rev_s = f"Rev est ${rev_b:.1f}B" if rev_b else ""
        rv = u.get("revision") or {}
        rv_s = ""
        if rv:
            buy_new = rv.get("buy_count_new")
            buy_old = rv.get("buy_count_old")
            if buy_new is not None and buy_old is not None:
                rv_s = f"buy {buy_old}→{buy_new}"
        annot_parts = [p for p in (date_s + (f" ({d_n})" if d_n else ""), eps_s, rev_s, rv_s) if p]
        ax.text(caps[i] + mx * 0.02, i, " · ".join(annot_parts),
                ha="left", va="center", fontsize=8, color="#222")

    ax.set_title("[다가올 어닝] 발표 예정 (시총 큰 순) — 분석가 buy 변화 색상",
                 fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def risk_gauge_visual(gauge: dict, prev_gauge_score: float | None,
                      out_dir: Path, filename: str = "00_risk_gauge.png",
                      date_iso: str | None = None) -> str | None:
    """위험선호 반원 게이지 — 0~100 + 어제 대비 delta + 라벨.

    gauge: {score, label, signals?}
    prev_gauge_score: 어제 점수 (없으면 baseline 표시).
    """
    if not gauge or gauge.get("score") is None:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Wedge, FancyBboxPatch

    score = float(gauge.get("score") or 50)
    label = gauge.get("label") or ""
    score = max(0.0, min(100.0, score))

    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.set_xlim(-1.3, 1.3); ax.set_ylim(-0.4, 1.25); ax.axis("off")

    # 배경 5단 구간 (Risk-Off → Risk-On)
    bands = [
        (0, 30, "#c62828", "Risk-Off"),
        (30, 45, "#ef6c00", "약 Risk-Off"),
        (45, 55, "#9e9e9e", "중립"),
        (55, 75, "#43a047", "약 Risk-On"),
        (75, 100, "#1b5e20", "Risk-On"),
    ]
    for lo, hi, col, _ in bands:
        # 0=180°(좌), 100=0°(우)
        ang_lo = 180 - (lo / 100.0) * 180
        ang_hi = 180 - (hi / 100.0) * 180
        ax.add_patch(Wedge((0, 0), 1.0, ang_hi, ang_lo, width=0.28,
                           facecolor=col, edgecolor="white", linewidth=1.5))

    # 눈금 0/25/50/75/100
    for tick in (0, 25, 50, 75, 100):
        ang = np.radians(180 - (tick / 100.0) * 180)
        rx, ry = np.cos(ang), np.sin(ang)
        ax.plot([rx * 0.72, rx * 0.68], [ry * 0.72, ry * 0.68], color="#222", linewidth=1.2)
        ax.text(rx * 0.62, ry * 0.62, str(tick), ha="center", va="center",
                fontsize=9, color="#444")

    # 바늘
    ang = np.radians(180 - (score / 100.0) * 180)
    nx, ny = np.cos(ang) * 0.92, np.sin(ang) * 0.92
    ax.plot([0, nx], [0, ny], color="#111", linewidth=3, solid_capstyle="round")
    ax.add_patch(plt.Circle((0, 0), 0.05, facecolor="#111", edgecolor="white", linewidth=1.5))

    # 점수 텍스트 (중앙 아래)
    ax.text(0, -0.18, f"{int(score)}/100", ha="center", va="center",
            fontsize=26, fontweight="bold", color="#111")
    ax.text(0, -0.32, label, ha="center", va="center", fontsize=14, color="#333")

    # 어제 대비 delta
    if isinstance(prev_gauge_score, (int, float)):
        diff = score - float(prev_gauge_score)
        arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "·")
        col = "#2e7d32" if diff > 0 else ("#c62828" if diff < 0 else "#666")
        ax.text(0, 1.10, f"전일 {int(prev_gauge_score)}/100  {arrow}{abs(diff):.0f}점",
                ha="center", va="center", fontsize=11, color=col, fontweight="bold")
    else:
        ax.text(0, 1.10, "전일 기준선 (snapshot 없음)",
                ha="center", va="center", fontsize=10, color="#666")

    ax.set_title("[위험선호 게이지] 0=Risk-Off · 50=중립 · 100=Risk-On",
                 fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def followup_tracking_table(prev_highlights: list[dict], out_dir: Path,
                            filename: str = "01_followup_table.png",
                            date_iso: str | None = None) -> str | None:
    """어제 watchlist 종목별 어제 5D → 오늘 5D 추적 표.

    prev_highlights: snapshot.highlights_meta (어제 종목 list).
    각 ticker 오늘 5D 재 fetch (cache 활용) → 표.
    """
    if not prev_highlights:
        return None
    theme.setup()
    import matplotlib.pyplot as plt

    # ticker별 오늘 5D fetch
    from src.report.data import screener_adapter
    from src.report.data.fetch_prices import fetch_ohlcv

    def _today_5d(tkr: str, market: str) -> float | None:
        try:
            df = screener_adapter.load_ohlcv_df(tkr, market, days=15)
            if df is None or len(df) < 6:
                try:
                    df = fetch_ohlcv(tkr, days=15)
                except Exception:
                    df = None
            if df is None or len(df) < 6:
                return None
            close = df["Close"]
            last = float(close.iloc[-1]); ref = float(close.iloc[-6])
            return (last / ref - 1) * 100 if ref else None
        except Exception:
            return None

    rows = []
    for it in prev_highlights[:14]:  # 최대 14종목
        tkr = it.get("ticker")
        if not tkr:
            continue
        mkt = it.get("market") or "US"
        prev_5d = it.get("chg_5d")
        today_5d = _today_5d(tkr, mkt)
        if today_5d is None:
            today_5d_str = "N/A"
            delta = None
            cls = "—"
        else:
            today_5d_str = f"{today_5d:+.1f}%"
            if prev_5d is not None:
                delta = today_5d - float(prev_5d)
                if delta >= 1:
                    cls = "강화"
                elif delta <= -1:
                    cls = "약화"
                else:
                    cls = "유지"
            else:
                delta = None
                cls = "신규"
        rows.append({
            "label": (it.get("label") or tkr).split("(")[0].strip(),
            "ticker": tkr,
            "market": mkt,
            "sector": (it.get("sector") or "")[:14],
            "prev_5d": f"{float(prev_5d):+.1f}%" if isinstance(prev_5d, (int, float)) else "—",
            "today_5d": today_5d_str,
            "delta": f"{delta:+.1f}" if delta is not None else "—",
            "cls": cls,
        })
    if not rows:
        return None

    fig, ax = plt.subplots(figsize=(13, max(2.5, 0.45 * len(rows) + 1.5)))
    ax.axis("off")
    headers = ["종목", "티커", "시장", "섹터", "어제 5D", "오늘 5D", "Δ", "분류"]
    cell_text = [[r["label"], r["ticker"], r["market"], r["sector"],
                  r["prev_5d"], r["today_5d"], r["delta"], r["cls"]] for r in rows]
    table = ax.table(cellText=cell_text, colLabels=headers, loc="center",
                     cellLoc="center", colLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.5)
    # 분류 색상
    cls_col = {"강화": "#c8e6c9", "약화": "#ffcdd2", "유지": "#f5f5f5",
               "신규": "#e3f2fd", "—": "#fafafa"}
    for i, r in enumerate(rows, start=1):
        for j in range(len(headers)):
            cell = table[(i, j)]
            if j == 7:  # 분류 셀
                cell.set_facecolor(cls_col.get(r["cls"], "#fafafa"))
            elif j == 6 and r["cls"] in ("강화", "약화"):
                cell.set_facecolor("#e8f5e9" if r["cls"] == "강화" else "#ffebee")
    # 헤더 진한 색
    for j in range(len(headers)):
        cell = table[(0, j)]
        cell.set_facecolor("#37474f")
        cell.set_text_props(color="white", fontweight="bold")
    ax.set_title("[어제 watchlist 추적] 어제 5D → 오늘 5D · 신호 강화·약화·유지",
                 fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def fx_pressure_gauge(usdkrw_df, ewy_df, foreign_kospi_20d: float | None,
                      out_dir: Path, filename: str = "10_fx_pressure.png",
                      prev_pressure: float | None = None,
                      date_iso: str | None = None) -> tuple[str | None, dict]:
    """환전 압력 게이지 — USD/KRW 5D + EWY 5D + 외국인 KOSPI 20D 통합 점수.

    반환: (path, gauge_dict). gauge_dict = {score, label, components}.

    압력 점수 (0~100, 기본 50):
      + USD/KRW 5D ×20 (양수 = 압력 ↑)
      - EWY 5D ×20 (양수 = 압력 ↓)
      - 외국인 KOSPI 20D / 1e11 ×4 (음수 = 압력 ↑, 1조 매도 → 약 +40)
    """
    if usdkrw_df is None or len(usdkrw_df) < 7:
        return None, {}
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Wedge

    # 5D 변화 계산
    def _pct_5d(df) -> float | None:
        if df is None or len(df) < 7:
            return None
        try:
            return (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-6]) - 1) * 100
        except Exception:
            return None

    krw_5d = _pct_5d(usdkrw_df) or 0.0
    ewy_5d = _pct_5d(ewy_df)
    # 외국인 KOSPI 20D 누적 (억원 단위로 보임)
    fk = float(foreign_kospi_20d) if isinstance(foreign_kospi_20d, (int, float)) else 0.0

    # 점수 (cap 0-100)
    score = 50.0
    score += krw_5d * 20
    if ewy_5d is not None:
        score -= ewy_5d * 20
    # 외국인 매도 (억원, 음수 = 매도): -10000억 = -1조 → +40점
    score -= fk / 250  # -10000(억원) / 250 = -40 → -(-40) = +40
    score = max(0.0, min(100.0, score))

    if score >= 75:
        label = "매우 강함"
    elif score >= 60:
        label = "강함"
    elif score >= 45:
        label = "중간"
    elif score >= 30:
        label = "약함"
    else:
        label = "낮음"

    # 게이지(좌) + 컴포넌트 기여도 bar(우) 통합 figure
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(15, 5.5),
                                   gridspec_kw={"width_ratios": [3, 2]})
    ax.set_xlim(-1.3, 1.3); ax.set_ylim(-0.4, 1.25); ax.axis("off")

    # 5단 색상 (반전 — 압력 ↑ = 빨강)
    bands = [
        (0, 30, "#43a047"),    # 낮음 — 녹
        (30, 45, "#9e9e9e"),   # 약함 — 회
        (45, 60, "#ef6c00"),   # 중간 — 주황
        (60, 75, "#d84315"),   # 강함 — 빨강
        (75, 100, "#b71c1c"),  # 매우 강함 — 진빨강
    ]
    for lo, hi, col in bands:
        ang_lo = 180 - (lo / 100.0) * 180
        ang_hi = 180 - (hi / 100.0) * 180
        ax.add_patch(Wedge((0, 0), 1.0, ang_hi, ang_lo, width=0.28,
                           facecolor=col, edgecolor="white", linewidth=1.5))

    for tick in (0, 25, 50, 75, 100):
        ang = np.radians(180 - (tick / 100.0) * 180)
        rx, ry = np.cos(ang), np.sin(ang)
        ax.plot([rx * 0.72, rx * 0.68], [ry * 0.72, ry * 0.68], color="#222", linewidth=1.2)
        ax.text(rx * 0.62, ry * 0.62, str(tick), ha="center", va="center",
                fontsize=9, color="#444")

    ang = np.radians(180 - (score / 100.0) * 180)
    nx, ny = np.cos(ang) * 0.92, np.sin(ang) * 0.92
    ax.plot([0, nx], [0, ny], color="#111", linewidth=3, solid_capstyle="round")
    ax.add_patch(plt.Circle((0, 0), 0.05, facecolor="#111", edgecolor="white", linewidth=1.5))

    ax.text(0, -0.14, f"{int(score)}/100", ha="center", va="center",
            fontsize=24, fontweight="bold", color="#111")
    ax.text(0, -0.28, label, ha="center", va="center", fontsize=14, color="#333")

    # 전일 대비 delta
    if isinstance(prev_pressure, (int, float)):
        diff = score - float(prev_pressure)
        arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "·")
        col = "#c62828" if diff > 0 else ("#2e7d32" if diff < 0 else "#666")
        ax.text(0, 1.10, f"전일 {int(prev_pressure)}/100  {arrow}{abs(diff):.0f}점",
                ha="center", va="center", fontsize=11, color=col, fontweight="bold")
    else:
        ax.text(0, 1.10, "전일 기준선", ha="center", va="center", fontsize=10, color="#666")

    ax.set_title("[환전 압력 게이지]", fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)

    # 컴포넌트 기여도 bar (우측)
    contributions = []
    contributions.append(("USD/KRW 5D", krw_5d * 20))
    if ewy_5d is not None:
        contributions.append(("EWY 5D", -ewy_5d * 20))
    contributions.append(("외국인 KOSPI 20D", -fk / 250))
    labels_c = [c[0] for c in contributions]
    vals_c = [c[1] for c in contributions]
    colors_c = ["#c62828" if v > 0 else ("#2e7d32" if v < 0 else "#9e9e9e") for v in vals_c]
    y_pos = np.arange(len(labels_c))
    ax2.barh(y_pos, vals_c, color=colors_c, alpha=0.85, edgecolor="#333", linewidth=0.5)
    ax2.set_yticks(y_pos)
    ax2.set_yticklabels(labels_c, fontsize=10, fontweight="bold")
    ax2.invert_yaxis()
    ax2.axvline(0, color="#333", linewidth=0.6)
    ax2.set_xlabel("점수 기여 (+압력 ↑ · -압력 ↓)", fontsize=9)
    ax2.set_title("컴포넌트 기여도", fontsize=11)
    ax2.grid(axis="x", linestyle=":", alpha=0.4)
    for i, v in enumerate(vals_c):
        if v != 0:
            ax2.text(v + (0.5 if v >= 0 else -0.5), i, f"{v:+.1f}",
                     ha="left" if v >= 0 else "right", va="center", fontsize=9)
    # raw 값 우측 annotation
    raw_vals = [f"USD/KRW {krw_5d:+.2f}%",
                f"EWY {ewy_5d:+.2f}%" if ewy_5d is not None else "EWY —",
                f"{fk:+,.0f}억"]
    raw_max = max([abs(v) for v in vals_c] + [1])
    for i, raw_s in enumerate(raw_vals):
        ax2.text(raw_max * 1.05, i, raw_s, ha="left", va="center",
                 fontsize=8, color="#555")

    fig.tight_layout()
    out_path = theme.save_fig(fig, out_dir, filename)
    gauge_dict = {
        "score": int(score),
        "label": label,
        "components": {
            "usdkrw_5d": round(krw_5d, 2),
            "ewy_5d": round(ewy_5d, 2) if ewy_5d is not None else None,
            "foreign_kospi_20d": round(fk),
        },
    }
    return out_path, gauge_dict


def risk_component_breakdown(rows: list[dict], out_dir: Path,
                             filename: str = "00b_risk_components.png",
                             date_iso: str | None = None) -> str | None:
    """6대 자산군별 1D·5D 평균 — Risk gauge 점수의 기여 구조."""
    if not rows:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np

    # group별 평균
    groups: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        g = r.get("group") or "기타"
        d1 = r.get("d1"); d5 = r.get("d5")
        if d1 is None and d5 is None:
            continue
        groups.setdefault(g, []).append((d1, d5))
    if not groups:
        return None

    order = ["주식", "채권", "원자재", "통화", "암호"]
    items = []
    for g in order:
        if g in groups:
            vals = groups[g]
            d1_avg = float(np.mean([v[0] for v in vals if v[0] is not None] or [0]))
            d5_avg = float(np.mean([v[1] for v in vals if v[1] is not None] or [0]))
            items.append((g, d1_avg, d5_avg))
    # order 외 자산군
    for g in groups:
        if g not in order:
            vals = groups[g]
            d1_avg = float(np.mean([v[0] for v in vals if v[0] is not None] or [0]))
            d5_avg = float(np.mean([v[1] for v in vals if v[1] is not None] or [0]))
            items.append((g, d1_avg, d5_avg))

    fig, ax = plt.subplots(figsize=(13, max(4, 0.6 * len(items) + 1.8)))
    y = np.arange(len(items))
    h = 0.38
    d1_vals = [it[1] for it in items]
    d5_vals = [it[2] for it in items]

    def _col(v):
        if v >= 0.5:
            return "#1b5e20"
        if v >= 0:
            return "#43a047"
        if v >= -0.5:
            return "#ef6c00"
        return "#c62828"

    c1 = [_col(v) for v in d1_vals]
    c5 = [_col(v) for v in d5_vals]
    ax.barh(y + h / 2, d1_vals, height=h, color=c1, edgecolor="#333", linewidth=0.4, label="1D")
    ax.barh(y - h / 2, d5_vals, height=h, color=c5, edgecolor="#333", linewidth=0.4, alpha=0.6, label="5D")
    ax.set_yticks(y)
    ax.set_yticklabels([it[0] for it in items], fontsize=11, fontweight="bold")
    ax.invert_yaxis()
    ax.axvline(0, color="#333", linewidth=0.6)
    ax.set_xlabel("평균 등락률 (%)", fontsize=9)
    ax.grid(axis="x", linestyle=":", alpha=0.4)
    ax.legend(fontsize=9, loc="lower right")
    mx = max([abs(v) for v in d1_vals + d5_vals] + [0.5])
    for i, (g, v1, v5) in enumerate(items):
        ax.text(v1 + (mx * 0.04 if v1 >= 0 else -mx * 0.04), i + h / 2,
                f"{v1:+.2f}%", va="center", ha="left" if v1 >= 0 else "right",
                fontsize=8, color="#222")
        ax.text(v5 + (mx * 0.04 if v5 >= 0 else -mx * 0.04), i - h / 2,
                f"{v5:+.2f}%", va="center", ha="left" if v5 >= 0 else "right",
                fontsize=8, color="#555")
    ax.set_title("[6대 자산군 1D·5D 평균] Risk 게이지 기여 구조",
                 fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def watchlist_thesis_quadrant(watchlist: dict, out_dir: Path,
                              filename: str = "30b_thesis_quadrant.png",
                              date_iso: str | None = None) -> str | None:
    """watchlist 14종목 → 4 quadrant 분류 dashboard.

    분류 (auto rule):
    - 강화: chg_5d ≥ +5% (녹색)
    - 약화: chg_5d ≤ -5% (빨강)
    - 디커플링: kr_linkage 카테고리 + theme_linkage.us_theme_r5d와 종목 5D 차이 ≥5%
    - 유지/노이즈: 그 외 (회색)
    """
    if not watchlist:
        return None
    items = list(watchlist.get("us") or []) + list(watchlist.get("kr") or [])
    if not items:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    groups: dict[str, list[str]] = {"강화": [], "약화": [], "디커플링": [], "유지": []}
    for it in items:
        chg = it.get("chg_5d")
        label = (it.get("label") or it.get("ticker") or "").split("(")[0].strip()
        market = it.get("market") or "US"
        tag = f"{label}({market})"
        if not isinstance(chg, (int, float)):
            groups["유지"].append(tag)
            continue
        tl = it.get("theme_linkage") or {}
        us_r5d = tl.get("us_theme_r5d")
        if isinstance(us_r5d, (int, float)) and "kr_linkage" in (it.get("categories") or []):
            diff = us_r5d - chg
            if abs(diff) >= 5:
                groups["디커플링"].append(f"{tag}  Δ{diff:+.0f}%")
                continue
        if chg >= 5:
            groups["강화"].append(f"{tag}  {chg:+.1f}%")
        elif chg <= -5:
            groups["약화"].append(f"{tag}  {chg:+.1f}%")
        else:
            groups["유지"].append(f"{tag}  {chg:+.1f}%")

    quad_specs = [
        ("강화", "#1b5e20", "#c8e6c9", (0.05, 0.55, 0.45, 0.40)),
        ("약화", "#c62828", "#ffcdd2", (0.55, 0.55, 0.40, 0.40)),
        ("디커플링", "#ef6c00", "#ffe0b2", (0.05, 0.10, 0.45, 0.40)),
        ("유지", "#616161", "#eeeeee", (0.55, 0.10, 0.40, 0.40)),
    ]

    fig, ax = plt.subplots(figsize=(13, 7.5))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    for name, dark, light, (x, y, w, h) in quad_specs:
        lst = groups[name]
        # 박스
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.01",
                     facecolor=light, edgecolor=dark, linewidth=1.5))
        # 헤더
        ax.text(x + 0.02, y + h - 0.04, f"{name}  ({len(lst)})",
                ha="left", va="center", fontsize=14, fontweight="bold", color=dark)
        # 종목 list
        max_items = 6
        for i, item in enumerate(lst[:max_items]):
            ax.text(x + 0.03, y + h - 0.10 - i * 0.05, f"• {item}",
                    ha="left", va="center", fontsize=10, color="#222")
        if len(lst) > max_items:
            ax.text(x + 0.03, y + h - 0.10 - max_items * 0.05,
                    f"  ... 외 {len(lst) - max_items}",
                    ha="left", va="center", fontsize=9, color="#666", style="italic")
        if not lst:
            ax.text(x + w / 2, y + h / 2, "—", ha="center", va="center",
                    fontsize=18, color="#aaa")

    ax.set_title("[watchlist thesis quadrant] 14종목 자동 분류 — 강화·약화·디커플링·유지",
                 fontsize=13, fontweight="bold")
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)


def gauge_history_chart(history: list[dict], today_risk: dict, today_fx: dict,
                        out_dir: Path, filename: str = "00c_gauge_history.png",
                        date_iso: str | None = None) -> str | None:
    """Risk gauge + FX 압력 게이지 N일 시계열.

    history: state.load_history 결과 (오래된 → 최신).
    today_risk/fx: 오늘 게이지 dict.
    """
    if not history and not today_risk and not today_fx:
        return None
    theme.setup()
    import matplotlib.pyplot as plt
    from datetime import date as _date

    dates = []
    risk_vals = []
    fx_vals = []
    for snap in history:
        d_str = snap.get("date")
        if not d_str:
            continue
        try:
            d = _date.fromisoformat(d_str)
        except Exception:
            continue
        dates.append(d)
        risk_vals.append(snap.get("gauge_score"))
        fx_vals.append(snap.get("fx_pressure_score"))

    # 오늘 append
    today_d = None
    if date_iso:
        try:
            today_d = _date.fromisoformat(date_iso)
        except Exception:
            today_d = None
    if today_d is None:
        today_d = _date.today()
    dates.append(today_d)
    risk_vals.append((today_risk or {}).get("score"))
    fx_vals.append((today_fx or {}).get("score"))

    if not dates or len([v for v in risk_vals if v is not None]) < 1:
        return None

    fig, ax = plt.subplots(figsize=(13, 5.0))
    valid_risk = [(d, v) for d, v in zip(dates, risk_vals) if v is not None]
    valid_fx = [(d, v) for d, v in zip(dates, fx_vals) if v is not None]

    if valid_risk:
        rd, rv = zip(*valid_risk)
        ax.plot(rd, rv, color="#2e7d32", linewidth=2.2, marker="o", markersize=6,
                label="Risk-On 게이지", zorder=4)
        # 마지막 점 강조
        ax.scatter([rd[-1]], [rv[-1]], color="#1b5e20", s=120, zorder=5,
                   edgecolor="white", linewidth=1.5)
    if valid_fx:
        fd, fv = zip(*valid_fx)
        ax.plot(fd, fv, color="#c62828", linewidth=2.2, marker="s", markersize=6,
                label="환전 압력 게이지", zorder=4)
        ax.scatter([fd[-1]], [fv[-1]], color="#b71c1c", s=120, zorder=5,
                   edgecolor="white", linewidth=1.5, marker="s")

    # 임계 라인
    ax.axhline(50, color="#888", linewidth=0.6, linestyle=":", alpha=0.6)
    ax.axhspan(0, 30, color="#43a047", alpha=0.06)
    ax.axhspan(70, 100, color="#c62828", alpha=0.06)
    ax.set_ylim(0, 100)
    ax.set_ylabel("점수 (0-100)", fontsize=10)
    ax.set_title(f"[게이지 시계열] Risk·FX 압력 — 최근 {len(dates)}일",
                 fontsize=13, fontweight="bold")
    ax.legend(fontsize=10, loc="upper left")
    ax.grid(True, alpha=0.25, linestyle=":")
    ax.tick_params(labelsize=8)
    fig.autofmt_xdate(rotation=30)
    theme.stamp(ax, date_iso)
    fig.tight_layout()
    return theme.save_fig(fig, out_dir, filename)
