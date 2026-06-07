"""어닝콜 PDF 보고서 빌더 (matplotlib PdfPages) — Phase 8 PM Decision Document.

페이지 구성 (12-15쪽 풀버전):
  p.1   Decision Page — verdict + 2×4 KPI 그리드 + bull/bear 박스 (PM이 1초 안에 판단)
  p.2   Market Reaction — 주가 reaction 표 + sell-side target revision + 표면 vs 구조적 thesis
  p.3-5 Evidence — synthesis §0~§10 (sub-header·표·bullet 위주)
  p.6   Verify cross-check (있을 때)
  p.7-12 Charts with interpretation — 차트당 1쪽 (차트 + §3.5 thesis 인용)
  p.13  Counter-thesis (있을 때)
  p.14  Top Q&A 5개 + significance
  p.15  Appendix — 재무 raw 표 (1종목이면 단순, 다종목이면 비교)

격리: 모든 import는 build_pdf() 본문 안에서. 실패 시 None 반환.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 이모지 — Noto Sans CJK에 글리프 없어 tofu 박스 — 렌더 전 제거
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "]"
)


def _strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", s or "").strip()


# ------------------------------------------------------------------
# Phase 10 — 네이버 톤 스타일 상수 + 페이지 레이아웃
# ------------------------------------------------------------------
STYLE = {
    "navy":     "#0b3d91",
    "accent":   "#c0392b",
    "ink":      "#1a1a1a",
    "body":     "#222222",
    "muted":    "#555555",
    "panel_bg": "#f4f6fa",
    "zebra":    "#f5f7fa",
    "green":    "#1b5e20",
    "red":      "#b71c1c",
}

# A4 페이지 좌표 (inches). matplotlib axis 좌표는 fig 내부 비율(0~1).
PAGE_W, PAGE_H = 8.27, 11.69
# 본문 axis (좌표 기준). add_axes([left, bottom, width, height])
CONTENT_AXES = [0.06, 0.04, 0.88, 0.92]
# 본문 진입 시 y 시작 / 본문 하한 (BOTTOM 미만 시 새 페이지)
Y_TOP = 0.93
Y_BOTTOM = 0.03
# Markdown 본문 렌더 기본값
BODY_SIZE = 9.0
LINE_H = 0.0175

# Markdown 헤딩 H1/H2/H3 시각 스타일 — _render_markdown_block 분기 단순화용
HEADING_STYLES = {
    1: {
        "size": 13.0, "weight": "bold", "text_color": "white",
        "y_consume": 0.048, "min_y": 0.10,
        "box_h": 0.034, "box_offset": 0.036, "text_y_offset": 0.008,
        "box_color": "navy",
    },
    2: {
        "size": 11.5, "weight": "bold", "text_color": "navy",
        "y_consume": 0.030, "min_y": 0.07,
        "bar_w": 0.006, "bar_h": 0.022, "bar_offset": 0.024,
        "text_x": 0.015, "bar_color": "navy",
    },
    3: {
        "size": 10.0, "weight": "bold", "text_color": "ink",
        "y_consume": 0.022, "min_y": 0.05,
    },
}


def _wrap_text(text: str, width: int = 95) -> list[str]:
    """단어 단위 wrap (영문/한글 혼합 OK). 이모지 제거."""
    import textwrap
    out: list[str] = []
    for line in (text or "").splitlines():
        line = _EMOJI_RE.sub("", line)
        if not line.strip():
            out.append("")
            continue
        han_ratio = sum(1 for c in line if ord(c) > 127) / max(len(line), 1)
        eff_width = int(width * (0.55 if han_ratio > 0.4 else 1.0))
        wrapped = textwrap.wrap(line, width=eff_width) or [""]
        out.extend(wrapped)
    return out


def _setup_korean_font():
    from matplotlib import font_manager
    import matplotlib.pyplot as plt
    for fp in font_manager.findSystemFonts(fontpaths=None):
        low = fp.lower()
        if any(k in low for k in ("notosanscjk", "notosanskr", "nanumgothic")):
            try:
                font_manager.fontManager.addfont(fp)
                plt.rcParams["font.family"] = font_manager.FontProperties(fname=fp).get_name()
                return True
            except Exception:
                continue
    return False


# ------------------------------------------------------------------
# Synthesis 텍스트 파서 — §0 verdict 추출
# ------------------------------------------------------------------
_VERDICT_LINE_RE = re.compile(
    r"\*\*\s*([A-Z][A-Z0-9.\-]{0,9})\s*\*\*\s*:\s*"
    r"(bull|bear|pair|long|short|pass|buy|sell|hold)[^\n]*?"
    r"(★+(?:\s*☆*)?|\*+)?[^\n]*",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"^##\s+(\d+(?:\.\d+)?)\.?\s+(.*)$", re.MULTILINE)


def _extract_verdict_lines(synthesis_text: str, tickers: list[str]) -> dict[str, str]:
    """§0 또는 본문 첫 1500자에서 종목별 verdict 한 줄 추출.

    합성 출력에서 `**TICKER**: bull/bear/pair · ★N · 근거` 패턴 매칭.
    실패 시 빈 dict.
    """
    out: dict[str, str] = {}
    if not synthesis_text:
        return out
    # 0번 섹션만 절단
    head = synthesis_text
    sections = list(_SECTION_RE.finditer(synthesis_text))
    if sections:
        # 0번 끝 위치 = 1번 시작 직전 (못 찾으면 처음 2500자)
        for s in sections:
            if s.group(1).startswith("1"):
                head = synthesis_text[: s.start()]
                break
        else:
            head = synthesis_text[:2500]
    else:
        head = synthesis_text[:2500]
    for m in _VERDICT_LINE_RE.finditer(head):
        t = m.group(1).upper()
        if t in tickers:
            line = m.group(0).strip()
            # 너무 길면 자르기
            if len(line) > 280:
                line = line[:280] + "…"
            out.setdefault(t, line)
    return out


def _extract_section(synthesis_text: str, section_id: str) -> str:
    """§N 본문만 잘라내기. 다음 ## 시작 직전까지. 못 찾으면 빈 문자열."""
    if not synthesis_text:
        return ""
    pattern = re.compile(
        rf"^##\s+{re.escape(section_id)}\.?[ \t]+.*?$",
        re.MULTILINE,
    )
    m = pattern.search(synthesis_text)
    if not m:
        return ""
    start = m.end()
    # 다음 ## 까지
    rest = synthesis_text[start:]
    nxt = re.search(r"^##\s+\d", rest, flags=re.MULTILINE)
    end = nxt.start() if nxt else len(rest)
    return rest[:end].strip()


# ------------------------------------------------------------------
# 기본 페이지 빌더
# ------------------------------------------------------------------
def _draw_text_page(pdf, title: str, body: str, *, footer: str = "") -> None:
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0.07, 0.05, 0.86, 0.90])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.text(
        0.0, 0.97, _strip_emoji(title),
        fontsize=16, fontweight="bold", va="top", ha="left", color="#1a1a1a",
    )
    ax.axhline(y=0.945, xmin=0.0, xmax=1.0, color="#888", linewidth=0.6)
    lines = _wrap_text(body, width=95)
    y = 0.92
    line_h = 0.018
    max_y_low = 0.04
    for ln in lines:
        if y < max_y_low:
            break
        ax.text(0.0, y, ln, fontsize=9.5, va="top", ha="left", color="#222")
        y -= line_h
    if footer:
        ax.text(0.5, 0.0, footer, fontsize=7.5, va="bottom", ha="center", color="#888")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _draw_long_text_pages(pdf, title: str, body: str, *, footer_prefix: str = "") -> None:
    """긴 텍스트를 여러 페이지로 분할."""
    lines = _wrap_text(body, width=95)
    LINES_PER_PAGE = 48
    page_num = 0
    for start in range(0, len(lines), LINES_PER_PAGE):
        chunk = lines[start:start + LINES_PER_PAGE]
        page_num += 1
        suffix = " (cont.)" if start > 0 else ""
        _draw_text_page(
            pdf,
            title + suffix,
            "\n".join(chunk),
            footer=f"{footer_prefix} p.{page_num}" if footer_prefix else f"p.{page_num}",
        )


# ------------------------------------------------------------------
# p.1 — Decision Page (Phase 8 Track N)
# ------------------------------------------------------------------
def _fmt_pct(v: Any, sign: bool = True) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:+.1f}%" if sign else f"{f:.1f}%"


def _fmt_usd(v: Any) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 1e9:
        return f"${f/1e9:.2f}B"
    if abs(f) >= 1e6:
        return f"${f/1e6:.1f}M"
    return f"${f:.2f}"


def _pick_consensus_delta(deltas: list[Any]) -> dict[str, str]:
    """ConsensusDelta list → {revenue: msg, eps: msg}. dataclass.metric 필드 기반 직접 분류.

    이전엔 메시지 텍스트를 정규식 매칭했으나 verify.py 메시지 포맷 변경 시 깨지기 쉬워서
    ConsensusDelta.metric 필드("revenue" | "eps")를 직접 사용한다.
    """
    out = {"revenue": "—", "eps": "—"}
    for r in deltas or []:
        metric = getattr(r, "metric", None)
        if metric in out:
            out[metric] = getattr(r, "message", str(r))
    return out


def _draw_decision_page(
    pdf,
    *,
    tickers: list[str],
    fiscal_period: str,
    synthesis_text: str,
    transcripts: dict[str, dict],
    consensus_by_ticker: dict[str, Any],
    consensus_delta_by_ticker: dict[str, list],
    market_reaction_by_ticker: dict[str, Any],
) -> None:
    """1페이지 — PM verdict + KPI grid + bull/bear. 30초 안에 의사결정."""
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # 상단 배경 띠
    ax.add_patch(plt.Rectangle((0, 0.92), 1, 0.08, facecolor="#0b3d91", zorder=0))
    ax.text(
        0.5, 0.96, "PM Decision Brief",
        fontsize=22, fontweight="bold", va="center", ha="center", color="white",
    )
    ax.text(
        0.5, 0.935, f"{', '.join(tickers[:6])} · {fiscal_period}",
        fontsize=11, va="center", ha="center", color="#cfe1ff",
    )

    # § Verdict 추출
    verdicts = _extract_verdict_lines(synthesis_text, [t.upper() for t in tickers])

    y = 0.89
    ax.text(
        0.04, y, "VERDICT",
        fontsize=11, fontweight="bold", va="top", ha="left", color="#0b3d91",
    )
    y -= 0.028
    for t in tickers:
        line = verdicts.get(t.upper(), "")
        if not line:
            line = f"**{t}**: (synthesis §0에서 verdict 미추출 — §0 본문 확인)"
        wrapped = _wrap_text(line, width=98)
        for w in wrapped[:3]:
            ax.text(0.04, y, _strip_emoji(w), fontsize=10, va="top", ha="left", color="#1a1a1a")
            y -= 0.022
        y -= 0.005

    # KPI grid — 종목별 2×4
    y -= 0.01
    ax.text(
        0.04, y, "KPI SNAPSHOT (PM이 1초에 봐야 할 것)",
        fontsize=11, fontweight="bold", va="top", ha="left", color="#0b3d91",
    )
    y -= 0.025

    # 표 데이터 구성
    for t in tickers[:4]:  # 표지엔 최대 4개 종목
        tr = transcripts.get(t, {}) or {}
        snap = consensus_by_ticker.get(t)
        mr = market_reaction_by_ticker.get(t)
        deltas = _pick_consensus_delta(consensus_delta_by_ticker.get(t) or [])
        hn = tr.get("headline_numbers") or {}
        rev_actual = hn.get("revenue_actual") or "—"
        eps_actual = hn.get("eps_actual") or "—"
        rev_yoy = hn.get("revenue_yoy") or "—"
        guidance = (tr.get("guidance") or {}).get("next_quarter_revenue") or "—"
        if isinstance(guidance, str) and len(guidance) > 50:
            guidance = guidance[:50] + "…"
        # Market reaction one-liner
        if mr is not None:
            ret1 = getattr(mr, "ret_1d", None)
            ret5 = getattr(mr, "ret_5d", None)
            ret30 = getattr(mr, "ret_30d", None)
            alpha5 = getattr(mr, "alpha_5d", None)
            rev_count = len(getattr(mr, "target_revisions", []) or [])
            mr_summary = (
                f"1d {_fmt_pct(ret1)} · 5d {_fmt_pct(ret5)} · 30d {_fmt_pct(ret30)} · "
                f"α5d {_fmt_pct(alpha5)} · revisions {rev_count}"
            )
        else:
            mr_summary = "—"
        # Target premium
        target_premium = "—"
        if snap is not None:
            tm = getattr(snap, "target_mean", None)
            cp = getattr(snap, "current_price", None)
            if tm and cp and cp > 0:
                target_premium = f"target ${tm:.0f} / spot ${cp:.0f} = {(tm/cp - 1)*100:+.1f}%"

        # 종목 header
        ax.text(0.04, y, _strip_emoji(t), fontsize=12, fontweight="bold", va="top", ha="left", color="#0b3d91")
        y -= 0.022
        rows = [
            ("Rev / YoY", f"{rev_actual} / {rev_yoy}"),
            ("Rev 컨센 delta", deltas["revenue"][:80]),
            ("EPS / 컨센 delta", f"{eps_actual} · {deltas['eps'][:60]}"),
            ("Next-Q 가이드", str(guidance)),
            ("주가 reaction", mr_summary),
            ("Target", target_premium),
        ]
        for label, val in rows:
            ax.text(0.06, y, label, fontsize=8.5, va="top", ha="left", color="#555")
            ax.text(0.30, y, _strip_emoji(str(val)), fontsize=8.5, va="top", ha="left", color="#1a1a1a")
            y -= 0.018
        y -= 0.008
        if y < 0.18:
            break

    # 하단 bull/bear 박스
    box_y = 0.05
    box_h = 0.10
    ax.add_patch(plt.Rectangle((0.04, box_y), 0.44, box_h, facecolor="#e6f4ea", edgecolor="#2e7d32", linewidth=0.8))
    ax.text(0.06, box_y + box_h - 0.012, "BULL THESIS (§4·§5A)", fontsize=9, fontweight="bold", color="#1b5e20", va="top")
    ax.add_patch(plt.Rectangle((0.52, box_y), 0.44, box_h, facecolor="#fdeaea", edgecolor="#c62828", linewidth=0.8))
    ax.text(0.54, box_y + box_h - 0.012, "BEAR / COUNTER (§5B·§6)", fontsize=9, fontweight="bold", color="#b71c1c", va="top")

    # bull/bear 내용 — synthesis §4 시장 서프라이즈 첫 두 줄 + §6 리스크 첫 두 줄
    surprises = _extract_section(synthesis_text, "4")
    risks = _extract_section(synthesis_text, "6")
    def _first_bullets(text: str, n: int = 3) -> list[str]:
        if not text:
            return []
        bullets = re.findall(r"^[\-\*•]\s+(.+)$", text, flags=re.MULTILINE)
        if bullets:
            return bullets[:n]
        return [s.strip() for s in text.split("\n") if s.strip()][:n]

    bull_pts = _first_bullets(surprises, 3)
    bear_pts = _first_bullets(risks, 3)
    for i, b in enumerate(bull_pts):
        wrapped = _wrap_text(_strip_emoji(b), width=68)
        if wrapped:
            ax.text(0.06, box_y + box_h - 0.025 - i * 0.022, "• " + wrapped[0][:60], fontsize=7.8, color="#1b5e20", va="top")
    for i, b in enumerate(bear_pts):
        wrapped = _wrap_text(_strip_emoji(b), width=68)
        if wrapped:
            ax.text(0.54, box_y + box_h - 0.025 - i * 0.022, "• " + wrapped[0][:60], fontsize=7.8, color="#b71c1c", va="top")

    # footer
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    ax.text(0.5, 0.02, f"생성: {now} · 데이터: SEC EDGAR · Yahoo · Alpha Vantage", fontsize=7.5, ha="center", color="#888")
    pdf.savefig(fig)
    plt.close(fig)


# ------------------------------------------------------------------
# p.2 — Market Reaction Page
# ------------------------------------------------------------------
def _draw_market_reaction_page(
    pdf,
    *,
    tickers: list[str],
    market_reaction_by_ticker: dict[str, Any],
    consensus_delta_by_ticker: dict[str, list],
    synthesis_text: str,
) -> None:
    """p.2 — 시장 반응 표 + sell-side target revision + §1 표면 vs 구조적 thesis."""
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0.05, 0.04, 0.90, 0.92])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.text(
        0.0, 0.98, "Market Reaction — Why did the market react X%?",
        fontsize=16, fontweight="bold", va="top", ha="left", color="#1a1a1a",
    )
    ax.axhline(y=0.955, xmin=0.0, xmax=1.0, color="#888", linewidth=0.6)
    y = 0.93
    # 가격 reaction 표
    ax.text(0.0, y, "1) Price Reaction (T-1 close → T+30d) + alpha vs ^GSPC", fontsize=10.5, fontweight="bold", color="#0b3d91", va="top")
    y -= 0.025
    headers = ["Ticker", "T-1", "T", "T+1d", "T+5d", "T+30d", "ret 1d", "ret 5d", "ret 30d", "α 5d", "implied"]
    cols_x = [0.0, 0.08, 0.16, 0.24, 0.32, 0.40, 0.50, 0.58, 0.66, 0.74, 0.84]
    for h, x in zip(headers, cols_x):
        ax.text(x, y, h, fontsize=8.0, fontweight="bold", color="#555", va="top")
    y -= 0.018
    for t in tickers:
        mr = market_reaction_by_ticker.get(t)
        if mr is None:
            vals = [t, "—", "—", "—", "—", "—", "—", "—", "—", "—", "—"]
        else:
            vals = [
                t,
                _fmt_usd(getattr(mr, "price_t_minus_1", None)),
                _fmt_usd(getattr(mr, "close_t", None)),
                _fmt_usd(getattr(mr, "close_t_plus_1d", None)),
                _fmt_usd(getattr(mr, "close_t_plus_5d", None)),
                _fmt_usd(getattr(mr, "close_t_plus_30d", None)),
                _fmt_pct(getattr(mr, "ret_1d", None)),
                _fmt_pct(getattr(mr, "ret_5d", None)),
                _fmt_pct(getattr(mr, "ret_30d", None)),
                _fmt_pct(getattr(mr, "alpha_5d", None)),
                _fmt_pct(getattr(mr, "implied_move_pct", None), sign=False),
            ]
        for v, x in zip(vals, cols_x):
            ax.text(x, y, _strip_emoji(str(v)), fontsize=8.0, va="top", color="#1a1a1a")
        y -= 0.018
    y -= 0.01

    # Sell-side target revisions
    ax.text(0.0, y, "2) Sell-side Target Revisions (since call)", fontsize=10.5, fontweight="bold", color="#0b3d91", va="top")
    y -= 0.025
    any_rev = False
    for t in tickers:
        mr = market_reaction_by_ticker.get(t)
        if mr is None:
            continue
        revs = getattr(mr, "target_revisions", []) or []
        if not revs:
            ax.text(0.0, y, f"  · {t}: (no revisions reported)", fontsize=8.5, color="#888", va="top")
            y -= 0.018
            continue
        ax.text(0.0, y, f"  · {t} ({len(revs)} revisions):", fontsize=8.8, fontweight="bold", color="#1a1a1a", va="top")
        y -= 0.018
        for r in revs[:6]:
            line = (
                f"      {r.get('date','?')}  {r.get('firm','?')}: "
                f"{r.get('from_grade','?')} → {r.get('to_grade','?')}  ({r.get('action','?')})"
            )
            ax.text(0.0, y, _strip_emoji(line)[:130], fontsize=8.0, color="#1a1a1a", va="top")
            y -= 0.017
        any_rev = True
    if not any_rev and tickers:
        pass

    y -= 0.01
    # §1 본문 인용 (표면 vs 구조적 thesis 분해)
    ax.text(0.0, y, "3) Surface surprise vs Structural thesis (synthesis §1 본문)", fontsize=10.5, fontweight="bold", color="#0b3d91", va="top")
    y -= 0.025
    s1_body = _extract_section(synthesis_text, "1") or "(§1 본문 미확보)"
    lines = _wrap_text(s1_body, width=98)
    for ln in lines:
        if y < 0.05:
            break
        ax.text(0.0, y, _strip_emoji(ln), fontsize=8.5, va="top", color="#222")
        y -= 0.0165

    ax.text(0.5, 0.005, "p.2 Market Reaction", fontsize=7, color="#888", ha="center")
    pdf.savefig(fig)
    plt.close(fig)


# ------------------------------------------------------------------
# 차트 + interpretation (Phase 8 Track N)
# ------------------------------------------------------------------
def _draw_chart_interpretation_page(
    pdf,
    fig_chart,
    *,
    title: str,
    interpretation: str,
    footer: str = "",
) -> None:
    """한 페이지에 차트(상단 60%) + 해석(하단 35%) 배치."""
    import matplotlib.pyplot as plt
    if fig_chart is None:
        return
    try:
        # 새 figure 만들어서 chart를 이미지로 임시 저장 → 새 fig에 embed는 복잡 →
        # 차트는 그대로 fig로 PDF에 저장 (matplotlib PdfPages는 fig 단위)
        # 캡션은 별도 텍스트 페이지 추가
        fig_chart.set_size_inches(8.27, 6.0)
        pdf.savefig(fig_chart, bbox_inches="tight")
    finally:
        plt.close(fig_chart)

    # 캡션 페이지
    fig = plt.figure(figsize=(8.27, 4.0))
    ax = fig.add_axes([0.07, 0.10, 0.86, 0.85])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.text(
        0.0, 0.95, f"▣ Chart Interpretation — {title}",
        fontsize=13, fontweight="bold", va="top", ha="left", color="#0b3d91",
    )
    ax.axhline(y=0.91, xmin=0, xmax=1, color="#888", linewidth=0.5)
    body = interpretation or "(synthesis §9.X에 차트 해석이 없음 — synthesis가 §9 차트 evidence 섹션을 출력해야 PDF에 표시됨)"
    y = 0.88
    for ln in _wrap_text(body, width=110):
        if y < 0.05:
            break
        ax.text(0.0, y, _strip_emoji(ln), fontsize=9.5, va="top", color="#222")
        y -= 0.045
    if footer:
        ax.text(0.5, 0.0, footer, fontsize=7.5, ha="center", color="#888")
    pdf.savefig(fig)
    plt.close(fig)


# ------------------------------------------------------------------
# 표지 (compact)
# ------------------------------------------------------------------
def _draw_cover_v1(pdf, tickers: list[str], period: str, custom_question: str) -> None:
    """V1 표지 — 1면. EARNINGS_PDF_V2=0 폴백 경로에서만 호출."""
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.add_patch(plt.Rectangle((0, 0.78), 1, 0.22, facecolor="#0b3d91", zorder=0))
    ax.text(
        0.5, 0.93, "US Earnings Call Brief",
        fontsize=28, fontweight="bold", va="center", ha="center", color="white",
    )
    ax.text(
        0.5, 0.86, "PM Decision Document (Phase 8)",
        fontsize=14, va="center", ha="center", color="#cfe1ff",
    )
    ax.text(
        0.5, 0.70, f"분기: {period}",
        fontsize=14, fontweight="bold", ha="center", va="center", color="#1a1a1a",
    )
    ax.text(
        0.5, 0.65, "분석 대상 기업",
        fontsize=11, ha="center", va="center", color="#555",
    )
    grid_y = 0.58
    for i in range(0, len(tickers), 4):
        row = "    ".join(tickers[i:i + 4])
        ax.text(0.5, grid_y, row, fontsize=18, fontweight="bold", ha="center", va="center", color="#0b3d91")
        grid_y -= 0.06
    if custom_question:
        wrapped = _wrap_text("추가 분석 요청: " + custom_question, width=70)
        y = 0.30
        for ln in wrapped[:6]:
            ax.text(0.5, y, ln, fontsize=10, ha="center", va="center", color="#444", style="italic")
            y -= 0.022
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    ax.text(0.5, 0.06, f"생성: {now}", fontsize=9, ha="center", va="center", color="#777")
    ax.text(
        0.5, 0.03,
        "데이터: SEC EDGAR (XBRL US-GAAP) · 어닝콜 전문 · Yahoo Finance · Alpha Vantage",
        fontsize=8, ha="center", va="center", color="#999",
    )
    pdf.savefig(fig)
    plt.close(fig)


# ------------------------------------------------------------------
# Appendix — 빈 행 제거 + 1종목 단순화
# ------------------------------------------------------------------
def _draw_financial_table(pdf, financials_by_ticker: dict[str, Any]) -> None:
    """부록: 6년치 재무 raw 표 (CapEx / OCF / FCF / Revenue). 1종목이면 빈 구분 행 제거."""
    import matplotlib.pyplot as plt
    from src.earnings.sec_edgar import fmt_usd

    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0.04, 0.04, 0.92, 0.92])
    ax.set_axis_off()
    ax.text(
        0.0, 0.99, "Appendix — Annual Financial Data",
        fontsize=15, fontweight="bold", va="top", ha="left", color="#1a1a1a",
        transform=ax.transAxes,
    )
    ax.text(
        0.0, 0.965, "단위: USD (millions/billions). 소스: SEC EDGAR Company Facts (10-K FY).",
        fontsize=8, va="top", ha="left", color="#777", transform=ax.transAxes,
    )
    rows: list[list[str]] = []
    n_tickers = len(financials_by_ticker)
    ticker_keys = list(financials_by_ticker.keys())
    for idx, (ticker, fin) in enumerate(financials_by_ticker.items()):
        cap_by = {p.fy: p.val for p in fin.capex}
        ocf_by = {p.fy: p.val for p in fin.ocf}
        rev_by = {p.fy: p.val for p in fin.revenue}
        fcf_by = {p.fy: p.val for p in fin.fcf()}
        all_fys = sorted(
            set(list(cap_by.keys()) + list(ocf_by.keys()) + list(rev_by.keys())), reverse=True,
        )[:6]
        for fy in all_fys:
            rows.append([
                ticker,
                f"FY{fy}",
                fmt_usd(cap_by[fy]) if fy in cap_by else "—",
                fmt_usd(ocf_by[fy]) if fy in ocf_by else "—",
                fmt_usd(fcf_by[fy]) if fy in fcf_by else "—",
                fmt_usd(rev_by[fy]) if fy in rev_by else "—",
            ])
        # 구분 행은 종목 2개 이상이고 마지막 종목이 아닐 때만
        if n_tickers > 1 and idx < n_tickers - 1:
            rows.append(["", "", "", "", "", ""])
    if not rows:
        ax.text(0.5, 0.5, "재무 데이터 없음", fontsize=11, ha="center", va="center", color="#888", transform=ax.transAxes)
        pdf.savefig(fig)
        plt.close(fig)
        return
    col_labels = ["Ticker", "FY", "CapEx", "OCF", "FCF", "Revenue"]
    tbl = ax.table(
        cellText=rows,
        colLabels=col_labels,
        loc="upper center",
        cellLoc="center",
        colColours=["#0b3d91"] * len(col_labels),
        bbox=[0.0, 0.06, 1.0, 0.88],
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.5)
    for j in range(len(col_labels)):
        cell = tbl[0, j]
        cell.set_text_props(color="white", fontweight="bold")
    for i in range(1, len(rows) + 1):
        for j in range(len(col_labels)):
            if i % 2 == 0:
                tbl[i, j].set_facecolor("#f5f7fa")
    pdf.savefig(fig)
    plt.close(fig)


# ==================================================================
# Phase 10 — Markdown 토큰 파서 + 시각 강조 렌더러
# ==================================================================
_MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_MD_TABLE_SEP_RE = re.compile(r"^:?-+:?$")
_MD_BLOCKQUOTE_RE = re.compile(r"^>\s*(.*)$")
_MD_BULLET_RE = re.compile(r"^(\s*)[\-\*•·]\s+(.+)$")
_MD_INLINE_SEG_RE = re.compile(r"(\*\*[^*\n]+\*\*|\[[A-Z0-9.\-_]+\]\[[a-z_0-9\-]+\]|`[^`\n]+`|_[^_\n]+_)")
_MD_CITATION_RE = re.compile(r"\[[A-Z0-9.\-_]+\]\[[a-z_0-9\-]+\]")


def _md_tokenize(text: str) -> list[dict]:
    """라인 단위 markdown → 토큰 리스트.

    토큰 타입: heading(level 1-3) / bullet(indent) / blockquote / table / para / blank.
    표는 연속 table_row를 묶어서 단일 table 토큰으로.
    """
    tokens: list[dict] = []
    pending_rows: list[list[str]] = []
    pending_align: list[str] = []

    def _flush_table():
        nonlocal pending_rows, pending_align
        if pending_rows:
            tokens.append({
                "type": "table",
                "rows": pending_rows,
                "align": pending_align or ["left"] * len(pending_rows[0]),
            })
            pending_rows = []
            pending_align = []

    for raw in (text or "").splitlines():
        line = _strip_emoji(raw.rstrip())
        # 표 행 (| ... |)
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(_MD_TABLE_SEP_RE.match(c.replace(" ", "")) for c in cells if c):
                pending_align = ["left"] * len(cells)
                continue
            pending_rows.append(cells)
            continue
        else:
            _flush_table()

        # 헤딩
        m = _MD_HEADING_RE.match(line)
        if m:
            tokens.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).strip()})
            continue

        # blockquote
        m = _MD_BLOCKQUOTE_RE.match(line)
        if m:
            tokens.append({"type": "blockquote", "text": m.group(1)})
            continue

        # bullet
        m = _MD_BULLET_RE.match(line)
        if m:
            indent = min(len(m.group(1)) // 2, 3)
            tokens.append({"type": "bullet", "indent": indent, "text": m.group(2).strip()})
            continue

        # blank
        if not stripped:
            tokens.append({"type": "blank"})
            continue

        # para
        tokens.append({"type": "para", "text": line})

    _flush_table()
    return tokens


def _clean_md_inline(text: str) -> str:
    """표 셀 안 markdown 인라인 마크업 제거 (matplotlib table은 rich text 미지원)."""
    s = text or ""
    s = _MD_CITATION_RE.sub("", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"_([^_]+)_", r"\1", s)
    return s.strip()


def _strip_md_inline_keep_text(text: str) -> str:
    """굵게/인용/citation 마크업만 제거, 텍스트 그대로 유지 (graceful fallback)."""
    s = text or ""
    s = _MD_CITATION_RE.sub("", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"_([^_]+)_", r"\1", s)
    return s


def _wrap_text_kr(text: str, width: int = 95) -> list[str]:
    """한글 비율 고려 wrap (citation·강조 마크업은 사전 제거 후)."""
    return _wrap_text(_strip_md_inline_keep_text(text), width=width)


def _draw_md_table(ax, rows: list[list[str]], y_top: float, *, max_width: float = 1.0) -> float:
    """matplotlib table로 markdown 표 렌더링. 반환: 표 끝 y 좌표."""
    import matplotlib.pyplot as plt
    if not rows:
        return y_top
    cleaned = [[_clean_md_inline(c)[:60] for c in row] for row in rows]
    n_cols = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (n_cols - len(r)) for r in cleaned]

    header, *body = cleaned
    has_header = bool(body)

    row_h = 0.022
    table_h = row_h * len(cleaned) + 0.008
    y_bottom = y_top - table_h
    if y_bottom < 0.03:
        return y_top  # 페이지에 못 들어가면 패스

    # 셀 한글 비율 → fontsize 동적
    avg_text_len = sum(len(c) for row in cleaned for c in row) / max(sum(len(row) for row in cleaned), 1)
    fontsize = 8.0 if avg_text_len < 18 else 7.0

    tbl = ax.table(
        cellText=body if has_header else cleaned,
        colLabels=header if has_header else None,
        loc="upper left",
        cellLoc="left",
        colColours=[STYLE["navy"]] * n_cols if has_header else None,
        bbox=[0.0, y_bottom, max_width, table_h],
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(fontsize)
    if has_header:
        for j in range(n_cols):
            cell = tbl[0, j]
            cell.set_text_props(color="white", fontweight="bold")
    for i in range(1, len(cleaned) if has_header else len(cleaned) + 1):
        for j in range(n_cols):
            try:
                if i % 2 == 0:
                    tbl[i, j].set_facecolor(STYLE["zebra"])
            except KeyError:
                pass
    return y_bottom - 0.012


def _render_inline_styled(ax, x: float, y: float, text: str, *, base_size: float = 10.0, base_color: str = None) -> None:
    """한 줄 안의 **굵게** / `code` / _기울임_ / [X][src] 분할 렌더.

    측정 정확도 한계 → 부분 렌더링 실패 시 fallback으로 plain text 한 번에.
    가드: 미매칭 백틱/asterisk/citation은 raw 노출 안 되게 사전 정리.
    """
    if base_color is None:
        base_color = STYLE["body"]
    text = _strip_emoji(text)
    if not text:
        return
    # 미매칭 raw 마크업 사전 제거 (segment regex 미캐치 케이스)
    # 짝수개 백틱만 유지, 홀수개면 모두 제거
    if text.count("`") % 2 != 0:
        text = text.replace("`", "")
    if text.count("**") % 2 != 0:
        text = text.replace("**", "")
    parts = _MD_INLINE_SEG_RE.split(text)
    cur_x = x
    for part in parts:
        if not part:
            continue
        weight = "normal"
        color = base_color
        size = base_size
        family = None
        style = "normal"
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            content = part[2:-2]
            weight = "bold"
            color = STYLE["ink"]
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            content = part[1:-1]
            family = "monospace"
            color = STYLE["muted"]
            size = base_size - 0.5
        elif part.startswith("_") and part.endswith("_") and len(part) > 2:
            content = part[1:-1]
            style = "italic"
            color = STYLE["muted"]
        elif _MD_CITATION_RE.fullmatch(part):
            # citation: 시각 노이즈 → 완전 제거 (PM은 본문만 읽음)
            continue
        else:
            content = part
        kwargs = dict(fontsize=size, color=color, va="top", ha="left", fontweight=weight, fontstyle=style)
        if family:
            kwargs["family"] = family
        try:
            ax.text(cur_x, y, content, **kwargs)
        except Exception:
            ax.text(cur_x, y, content, fontsize=size, color=color, va="top", ha="left")
        han = sum(1 for c in content if ord(c) > 127)
        ascii_n = len(content) - han
        cur_x += (ascii_n * 0.0058 + han * 0.011) * (size / 10.0)


def _draw_heading(ax, plt, y: float, level: int, text: str) -> float:
    """HEADING_STYLES[level]에 따라 헤딩 시각 요소 그림. 반환: 다음 y 좌표."""
    s = HEADING_STYLES[level]
    txt = _strip_emoji(text)
    if level == 1:
        ax.add_patch(plt.Rectangle(
            (0, y - s["box_offset"]), 1, s["box_h"],
            facecolor=STYLE[s["box_color"]],
        ))
        ax.text(
            0.01, y - s["text_y_offset"], txt,
            fontsize=s["size"], fontweight=s["weight"],
            color=s["text_color"], va="center",
        )
    elif level == 2:
        ax.add_patch(plt.Rectangle(
            (0.0, y - s["bar_offset"]), s["bar_w"], s["bar_h"],
            facecolor=STYLE[s["bar_color"]],
        ))
        ax.text(
            s["text_x"], y, txt,
            fontsize=s["size"], fontweight=s["weight"],
            color=STYLE[s["text_color"]], va="top",
        )
    else:  # level 3
        ax.text(
            0.0, y, txt,
            fontsize=s["size"], fontweight=s["weight"],
            color=STYLE[s["text_color"]], va="top",
        )
    return y - s["y_consume"]


def _new_md_page(plt, title: str, *, title_box: bool = True):
    """Markdown 본문 페이지 1장 생성. title_box=True면 상단 파란 띠 + 흰 타이틀."""
    fig = plt.figure(figsize=(PAGE_W, PAGE_H))
    ax = fig.add_axes(CONTENT_AXES)
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    if title and title_box:
        ax.add_patch(plt.Rectangle((0, 0.96), 1, 0.04, facecolor=STYLE["navy"], zorder=0))
        ax.text(0.01, 0.98, _strip_emoji(title), fontsize=14, fontweight="bold",
                va="center", ha="left", color="white")
    return fig, ax


def _render_markdown_block(pdf, title: str, md_text: str, *, footer: str = "") -> None:
    """Markdown 본문 → 시각 강조 렌더링. 페이지 자동 분할."""
    import matplotlib.pyplot as plt
    if not md_text:
        return
    tokens = _md_tokenize(md_text)
    if not tokens:
        return

    page_n = 0

    def start_page():
        nonlocal page_n
        page_n += 1
        suffix = f" (cont. {page_n})" if page_n > 1 else ""
        return _new_md_page(plt, title + suffix)

    fig, ax = start_page()
    y = Y_TOP

    def new_page():
        nonlocal fig, ax, y
        if footer:
            ax.text(0.5, 0.005, footer, fontsize=7, color="#888", ha="center")
        pdf.savefig(fig)
        plt.close(fig)
        fig, ax = start_page()
        y = Y_TOP

    for tok in tokens:
        t = tok["type"]

        # widow guard: heading이 페이지 끝 직전이면 강제 새 페이지
        if t == "heading" and y < 0.14:
            new_page()

        if t == "heading":
            level = max(1, min(3, tok["level"]))
            if y < HEADING_STYLES[level]["min_y"]:
                new_page()
            y = _draw_heading(ax, plt, y, level, tok["text"])

        elif t == "blank":
            y -= 0.006

        elif t == "bullet":
            indent = tok["indent"]
            wrapped = _wrap_text_kr(tok["text"], width=int(100 - indent * 6))
            for j, line in enumerate(wrapped):
                if y < Y_BOTTOM:
                    new_page()
                prefix = "  " * indent + ("• " if j == 0 else "  ")
                ax.text(0.0, y, prefix, fontsize=BODY_SIZE, color=STYLE["body"], va="top")
                _render_inline_styled(ax, 0.018 + indent * 0.016, y, line,
                                       base_size=BODY_SIZE, base_color=STYLE["body"])
                y -= LINE_H

        elif t == "blockquote":
            wrapped = _wrap_text_kr(tok["text"], width=92)
            block_h = LINE_H * len(wrapped) + 0.006
            if y - block_h < Y_BOTTOM:
                new_page()
            ax.add_patch(plt.Rectangle((0.0, y - block_h), 1.0, block_h, facecolor=STYLE["panel_bg"], zorder=0))
            ax.add_patch(plt.Rectangle((0.0, y - block_h), 0.006, block_h, facecolor=STYLE["muted"]))
            cy = y - 0.010
            for line in wrapped:
                ax.text(0.020, cy, line, fontsize=BODY_SIZE - 0.5, color=STYLE["muted"],
                        fontstyle="italic", va="top")
                cy -= LINE_H
            y -= block_h + 0.005

        elif t == "table":
            rows = tok["rows"]
            if not rows:
                continue
            est_h = 0.022 * len(rows) + 0.015
            if y - est_h < Y_BOTTOM:
                new_page()
            y = _draw_md_table(ax, rows, y, max_width=1.0)

        elif t == "para":
            wrapped = _wrap_text_kr(tok["text"], width=100)
            for line in wrapped:
                if y < Y_BOTTOM:
                    new_page()
                _render_inline_styled(ax, 0.0, y, line, base_size=BODY_SIZE, base_color=STYLE["body"])
                y -= LINE_H

    if footer:
        ax.text(0.5, 0.005, footer, fontsize=7, color="#888", ha="center")
    pdf.savefig(fig)
    plt.close(fig)


# ==================================================================
# Phase 10-B — Editor's Pick 파서 + 표지 V2
# ==================================================================
def _parse_editors_pick(text: str) -> dict[str, Any]:
    """Phase 9 Editor's Pick markdown 본문에서 표지 TL;DR 박스용 4개 필드 추출.

    Editor's Pick은 Opus가 정확히 4개 ## 섹션으로 출력 (`prompts/earnings_editors_pick.txt`):
      ## Verdict — ticker별 한 줄 (`**TICKER**: bull/bear · ★N · 2문장`)
      ## 시장 기대 vs 진짜 중요했던 것 — TOP 3 항목
      ## 주가 ±X% 반응의 진짜 동력
      ## 다음 분기 검증 트리거 — 3개 트리거

    Returns:
        dict with keys:
          - verdict_lines: list[str] (Verdict 섹션의 ticker별 한 줄 라인 max 4)
          - real_signal: str (시장 기대 vs 진짜 섹션 첫 항목 첫 문장 max 200자)
          - trigger: str (검증 트리거 첫 항목 max 200자)
          - raw: str (위 파싱 모두 실패 시 첫 800자 — graceful fallback)
    """
    out: dict[str, Any] = {"verdict_lines": [], "real_signal": "", "trigger": "", "raw": ""}
    if not text:
        return out
    cleaned = _strip_emoji(text)

    # § Verdict 섹션
    m = re.search(r"##\s*Verdict[^\n]*\n(.+?)(?=\n##\s|\Z)", cleaned, re.DOTALL | re.IGNORECASE)
    if m:
        body = m.group(1).strip()
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            stripped = _strip_md_inline_keep_text(line)
            stripped = re.sub(r"^[\-\*•·]\s+", "", stripped).strip()
            if stripped:
                out["verdict_lines"].append(stripped[:220])
            if len(out["verdict_lines"]) >= 4:
                break

    # § 시장 기대 vs 진짜 중요했던 것 — 첫 항목 첫 문장
    m = re.search(r"##\s*시장\s*기대[^\n]*\n(.+?)(?=\n##\s|\Z)", cleaned, re.DOTALL)
    if m:
        body = m.group(1).strip()
        first_item = re.search(r"(?:\*\*|^)1[.\)]\s*(.+?)(?:\n\n|\n\*\*2|\Z)", body, re.DOTALL)
        if first_item:
            txt = _strip_md_inline_keep_text(first_item.group(1)).strip()
            sentence = re.split(r"(?<=[.다。])\s+", txt, maxsplit=1)[0]
            out["real_signal"] = sentence[:200]

    # § 검증 트리거 — 첫 항목
    m = re.search(r"##\s*(?:다음\s*분기\s*)?검증\s*트리거[^\n]*\n(.+?)(?=\n##\s|\Z)", cleaned, re.DOTALL)
    if m:
        body = m.group(1).strip()
        first = re.search(r"(?:^|\n)\s*1[.\)]\s*(.+?)(?:\n\s*2[.\)]|\Z)", body, re.DOTALL)
        if first:
            txt = _strip_md_inline_keep_text(first.group(1)).strip()
            out["trigger"] = txt.splitlines()[0][:200]

    if not (out["verdict_lines"] or out["real_signal"] or out["trigger"]):
        out["raw"] = _strip_md_inline_keep_text(cleaned)[:800]
    return out


def _draw_cover(
    pdf,
    *,
    tickers: list[str],
    fiscal_period: str,
    editors_pick_kr: str,
    synthesis_text: str,
    transcripts: dict[str, dict],
    consensus_by_ticker: dict[str, Any],
    consensus_delta_by_ticker: dict[str, list],
    market_reaction_by_ticker: dict[str, Any],
    custom_question: str = "",
) -> None:
    """V2 통합 표지 — 단일 페이지에 PM 30초 시각.

    레이아웃 (위→아래):
      0.92-1.00  파란 띠 + "US Earnings Brief" + 종목·분기
      0.66-0.89  TL;DR 박스 (회색 패널 + 좌측 navy 바) — verdict / 진짜 중요한 것 / 트리거
      0.20-0.62  KPI snapshot (종목별 Rev/EPS/컨센delta/주가 reaction)
      0.05-0.17  Bull thesis (좌, 녹색) + Bear/Counter (우, 빨강) 박스
      0.00-0.04  footer (생성 시각 + 출처)

    데이터 소스:
      - editors_pick_kr: Phase 9 `_step_editors_pick` Opus 본문. `_parse_editors_pick`으로
        verdict_lines / real_signal / trigger 추출.
      - synthesis_text: Phase 8 Opus 비교 합성. §4(시장 서프라이즈) → bull, §6(리스크) → bear.
      - transcripts[t].headline_numbers: KPI 표 데이터.
      - market_reaction_by_ticker[t]: 주가 reaction one-liner.

    Editor's Pick 파싱 실패 시 graceful fallback (raw 첫 800자 박스 출력).
    """
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # 상단 파란 띠
    ax.add_patch(plt.Rectangle((0, 0.92), 1, 0.08, facecolor=STYLE["navy"], zorder=0))
    ax.text(0.5, 0.965, "US Earnings Brief", fontsize=22, fontweight="bold",
            va="center", ha="center", color="white")
    ax.text(0.5, 0.935, f"{', '.join(tickers[:6])} · {fiscal_period}",
            fontsize=11, va="center", ha="center", color="#cfe1ff")

    # TL;DR 박스 (회색 배경 + 좌측 파란 바)
    pick = _parse_editors_pick(editors_pick_kr)
    box_top = 0.89
    box_bottom = 0.66
    box_h = box_top - box_bottom
    ax.add_patch(plt.Rectangle((0.04, box_bottom), 0.92, box_h, facecolor=STYLE["panel_bg"], zorder=0))
    ax.add_patch(plt.Rectangle((0.04, box_bottom), 0.010, box_h, facecolor=STYLE["navy"]))
    ax.text(0.060, box_top - 0.020, "TL;DR — PM 30초",
            fontsize=11, fontweight="bold", color=STYLE["navy"], va="top")

    y = box_top - 0.050
    if pick["verdict_lines"]:
        for line in pick["verdict_lines"][:3]:
            wrapped = _wrap_text_kr(line, width=78)
            for j, w in enumerate(wrapped[:2]):
                ax.text(0.060, y, ("▸ " if j == 0 else "   ") + w,
                        fontsize=9.5, fontweight="bold" if j == 0 else "normal",
                        color=STYLE["ink"], va="top")
                y -= 0.020
            y -= 0.005
    elif pick.get("raw"):
        for ln in _wrap_text_kr(pick["raw"], width=78)[:8]:
            ax.text(0.060, y, ln, fontsize=9, color=STYLE["body"], va="top")
            y -= 0.020

    if pick["real_signal"]:
        y -= 0.005
        ax.text(0.060, y, "▶ 진짜 중요했던 것", fontsize=9, fontweight="bold",
                color=STYLE["accent"], va="top")
        y -= 0.020
        for ln in _wrap_text_kr(pick["real_signal"], width=78)[:2]:
            ax.text(0.060, y, ln, fontsize=9, color=STYLE["body"], va="top")
            y -= 0.020

    if pick["trigger"]:
        y -= 0.005
        ax.text(0.060, y, "▶ 검증 트리거", fontsize=9, fontweight="bold",
                color=STYLE["green"], va="top")
        y -= 0.020
        for ln in _wrap_text_kr(pick["trigger"], width=78)[:2]:
            ax.text(0.060, y, ln, fontsize=9, color=STYLE["body"], va="top")
            y -= 0.020

    # KPI 압축 그리드 (Decision 페이지 핵심만 통합)
    ax.text(0.04, 0.62, "KPI SNAPSHOT", fontsize=11, fontweight="bold", color=STYLE["navy"], va="top")
    y = 0.60
    for t in tickers[:3]:
        tr = transcripts.get(t, {}) or {}
        mr = market_reaction_by_ticker.get(t)
        deltas = _pick_consensus_delta(consensus_delta_by_ticker.get(t) or [])
        hn = tr.get("headline_numbers") or {}
        rev_actual = hn.get("revenue_actual") or "—"
        eps_actual = hn.get("eps_actual") or "—"
        rev_yoy = hn.get("revenue_yoy") or "—"
        ret1 = _fmt_pct(getattr(mr, "ret_1d", None)) if mr else "—"
        ret5 = _fmt_pct(getattr(mr, "ret_5d", None)) if mr else "—"
        ax.text(0.04, y, _strip_emoji(t), fontsize=11, fontweight="bold", color=STYLE["navy"], va="top")
        y -= 0.020
        rows = [
            ("Rev / YoY", f"{rev_actual} · {rev_yoy}"),
            ("EPS", str(eps_actual)),
            ("Rev 컨센 delta", deltas["revenue"][:72]),
            ("주가 1d / 5d", f"{ret1} / {ret5}"),
        ]
        for label, val in rows:
            ax.text(0.06, y, label, fontsize=8.5, color=STYLE["muted"], va="top")
            ax.text(0.30, y, _strip_emoji(_clean_md_inline(str(val))),
                    fontsize=8.5, color=STYLE["ink"], va="top")
            y -= 0.018
        y -= 0.008
        if y < 0.20:
            break

    # 하단 bull/bear 박스 (Decision 페이지에서 흡수)
    surprises = _extract_section(synthesis_text, "4")
    risks = _extract_section(synthesis_text, "6")
    box_y = 0.05
    box_h2 = 0.12
    ax.add_patch(plt.Rectangle((0.04, box_y), 0.44, box_h2, facecolor="#e6f4ea",
                                 edgecolor="#2e7d32", linewidth=0.8))
    ax.text(0.06, box_y + box_h2 - 0.012, "BULL THESIS",
            fontsize=9, fontweight="bold", color=STYLE["green"], va="top")
    ax.add_patch(plt.Rectangle((0.52, box_y), 0.44, box_h2, facecolor="#fdeaea",
                                 edgecolor="#c62828", linewidth=0.8))
    ax.text(0.54, box_y + box_h2 - 0.012, "BEAR / COUNTER",
            fontsize=9, fontweight="bold", color=STYLE["red"], va="top")

    def _first_bullets(text: str, n: int = 3) -> list[str]:
        if not text:
            return []
        bullets = re.findall(r"^[\-\*•]\s+(.+)$", text, flags=re.MULTILINE)
        if bullets:
            return bullets[:n]
        return [s.strip() for s in text.split("\n") if s.strip()][:n]

    for i, b in enumerate(_first_bullets(surprises, 3)):
        cleaned = _strip_md_inline_keep_text(b)
        wrapped = _wrap_text_kr(cleaned, width=58)
        if wrapped:
            ax.text(0.06, box_y + box_h2 - 0.026 - i * 0.024, "• " + wrapped[0][:55],
                    fontsize=7.8, color=STYLE["green"], va="top")
    for i, b in enumerate(_first_bullets(risks, 3)):
        cleaned = _strip_md_inline_keep_text(b)
        wrapped = _wrap_text_kr(cleaned, width=58)
        if wrapped:
            ax.text(0.54, box_y + box_h2 - 0.026 - i * 0.024, "• " + wrapped[0][:55],
                    fontsize=7.8, color=STYLE["red"], va="top")

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    ax.text(0.5, 0.02, f"생성: {now} · SEC EDGAR · Yahoo · Alpha Vantage",
            fontsize=7.5, ha="center", color="#888")
    if custom_question:
        wrapped = _wrap_text_kr("추가 분석: " + custom_question, width=80)
        if wrapped:
            ax.text(0.5, 0.005, wrapped[0][:90], fontsize=7, ha="center",
                    color=STYLE["muted"], style="italic")
    pdf.savefig(fig)
    plt.close(fig)


# ==================================================================
# Phase 10-C — Chart Grid 1페이지 (6개 차트 3×2)
# ==================================================================
def _draw_chart_grid_page(pdf, financials_by_ticker: dict[str, Any], synthesis_text: str) -> None:
    """6개 6년 재무 차트를 1 페이지 3×2 grid에 배치 (V1 대비 12p → 1p 압축).

    종목 수 ≤ 5: grid 모드 (각 sub-chart figsize 자동, sub-title은 synthesis §9.x 첫 문장 25자).
    종목 수 > 5: 차트 가독성 망 → 폴백 (차트당 1p figsize 절반, 캡션 없음).

    Args:
        financials_by_ticker: {ticker: CompanyFinancials}. capex/ocf/fcf/revenue 6년치.
        synthesis_text: Opus 비교 합성 본문. §9.1~§9.6의 첫 문장이 sub-title로 추출됨.
    """
    import matplotlib.pyplot as plt
    from src.earnings import charts as charts_mod

    n_tickers = len(financials_by_ticker)
    if n_tickers > 5:
        # 종목 6+개면 grid 가독성 망함 → 차트당 1p 폴백 (figsize 절반)
        specs = [
            (charts_mod.chart_capex_absolute,  "9.1 CapEx"),
            (charts_mod.chart_capex_yoy,       "9.2 CapEx YoY"),
            (charts_mod.chart_fcf,             "9.3 FCF"),
            (charts_mod.chart_ocf_capex_ratio, "9.4 OCF/CapEx"),
            (charts_mod.chart_capex_intensity, "9.5 CapEx Intensity"),
            (charts_mod.chart_revenue,         "9.6 Revenue"),
        ]
        for builder, title in specs:
            fig = builder(financials_by_ticker)
            if fig is None:
                continue
            fig.set_size_inches(8.27, 5.0)
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)
        return

    fig = plt.figure(figsize=(8.27, 11.69))
    fig.suptitle("Charts — 6-Year Financial Comparison", fontsize=13, fontweight="bold", y=0.985)
    gs = fig.add_gridspec(3, 2, hspace=0.75, wspace=0.32, top=0.94, bottom=0.04, left=0.07, right=0.97)
    specs = [
        (charts_mod.chart_capex_absolute,  "9.1 CapEx",          0, 0),
        (charts_mod.chart_capex_yoy,       "9.2 CapEx YoY",      0, 1),
        (charts_mod.chart_fcf,             "9.3 FCF",            1, 0),
        (charts_mod.chart_ocf_capex_ratio, "9.4 OCF/CapEx",      1, 1),
        (charts_mod.chart_capex_intensity, "9.5 CapEx Intensity",2, 0),
        (charts_mod.chart_revenue,         "9.6 Revenue",        2, 1),
    ]
    for builder, title, r, c in specs:
        ax = fig.add_subplot(gs[r, c])
        try:
            builder(financials_by_ticker, ax=ax)
        except Exception:
            log.exception("grid 차트 실패 (%s)", title)
            ax.set_visible(False)
            continue
        sect_id = title.split(" ")[0]
        insight = _extract_section(synthesis_text or "", sect_id)
        if not insight:
            s9 = _extract_section(synthesis_text or "", "9")
            insight = s9[:200] if s9 else ""
        insight_clean = _strip_md_inline_keep_text(insight).strip()
        first_sent = re.split(r"(?<=[.다。])\s+", insight_clean, maxsplit=1)[0] if insight_clean else ""
        # sub-title은 25자 cap + 한 줄 — 다른 chart 영역 침범 방지
        subtitle = first_sent[:25]
        ax.set_title(title, fontsize=9, loc="left", color=STYLE["ink"], pad=8, fontweight="bold")
        if subtitle:
            # sub-text는 axis 위쪽에 한 줄, fontsize 작게
            ax.text(0.0, 1.02, subtitle, transform=ax.transAxes,
                     fontsize=6.5, color=STYLE["muted"], va="bottom", ha="left")
        ax.tick_params(labelsize=6.5)
        leg = ax.get_legend()
        if leg is not None:
            for txt in leg.get_texts():
                txt.set_fontsize(6)
    pdf.savefig(fig)
    plt.close(fig)


# ==================================================================
# Phase 10-D — 종목별 KPI 페이지 + 한글 Q&A 페이지
# ==================================================================
def _draw_ticker_kpi_page(
    pdf,
    *,
    ticker: str,
    tr: dict,
    mr: Any,
    consensus: Any,
    consensus_delta: list,
) -> None:
    """종목별 KPI 페이지 1장 (V1 7p → 1p 압축).

    레이아웃:
      0.95-1.00  파란 배너 + ticker / company / fiscal_period
      0.78-0.92  Key Numbers (Revenue/EPS/컨센delta/Margin/Net income/FCF/Buyback·Dividend
                  /Next-Q guide/FY guide/Capex commentary 12행)
      0.62-0.76  Market Reaction one-liner + target revisions top 3
      0.46-0.60  Segments top 4 (name · revenue · YoY · note)
      0.30-0.44  Management Tone overall (3줄 max)
      0.10-0.28  Surprises & Risks 불릿 (5개 max)

    Args:
        tr: extract JSON (headline_numbers / guidance / segments / management_tone /
            surprises_and_risks 등 필드 사용).
        mr: MarketReaction | None (Yahoo chart fetch 결과).
        consensus: ConsensusSnapshot | None (현재 미사용 — 미래 확장용).
        consensus_delta: list[ConsensusDelta] (revenue/eps 분류해 표에 표시).
    """
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # 상단 배너
    ax.add_patch(plt.Rectangle((0, 0.95), 1, 0.05, facecolor=STYLE["navy"], zorder=0))
    company = tr.get("company_name", "") or ""
    period = tr.get("fiscal_period", "") or ""
    ax.text(0.04, 0.975, f"{ticker} — {company}", fontsize=14, fontweight="bold",
            va="center", color="white")
    ax.text(0.96, 0.975, period, fontsize=10, va="center", ha="right", color="#cfe1ff")

    hn = tr.get("headline_numbers") or {}
    guidance = tr.get("guidance") or {}
    deltas = _pick_consensus_delta(consensus_delta or [])

    # KPI 표 (2열)
    y = 0.92
    ax.text(0.04, y, "Key Numbers", fontsize=11, fontweight="bold", color=STYLE["navy"], va="top")
    y -= 0.022
    kpi_rows = [
        ("Revenue (actual)", str(hn.get("revenue_actual") or "—")),
        ("Revenue YoY", str(hn.get("revenue_yoy") or "—")),
        ("Revenue 컨센 delta", deltas["revenue"][:80]),
        ("EPS (actual)", str(hn.get("eps_actual") or "—")),
        ("EPS 컨센 delta", deltas["eps"][:80]),
        ("Operating margin", str(hn.get("operating_margin") or "—")),
        ("Net income", str(hn.get("net_income") or "—")),
        ("FCF (분기)", str(hn.get("fcf_quarter") or "—")),
        ("Buyback / Dividend", str(hn.get("buyback_dividend") or "—")),
        ("Next-Q 가이드", str(guidance.get("next_quarter_revenue") or "—")[:80]),
        ("FY 가이드", str(guidance.get("fy_revenue") or "—")[:80]),
        ("Capex commentary", str(guidance.get("capex_commentary") or "—")[:80]),
    ]
    for label, val in kpi_rows:
        ax.text(0.05, y, label, fontsize=8.8, color=STYLE["muted"], va="top")
        ax.text(0.32, y, _strip_emoji(_clean_md_inline(val)), fontsize=8.8, color=STYLE["ink"], va="top")
        y -= 0.018
    y -= 0.005

    # 시장 반응
    ax.text(0.04, y, "Market Reaction", fontsize=11, fontweight="bold", color=STYLE["navy"], va="top")
    y -= 0.022
    if mr is not None:
        ax.text(0.05, y, f"T+1d {_fmt_pct(getattr(mr, 'ret_1d', None))}  ·  "
                f"T+5d {_fmt_pct(getattr(mr, 'ret_5d', None))}  ·  "
                f"T+30d {_fmt_pct(getattr(mr, 'ret_30d', None))}  ·  "
                f"α5d {_fmt_pct(getattr(mr, 'alpha_5d', None))}",
                fontsize=9, color=STYLE["ink"], va="top")
        y -= 0.020
        revs = getattr(mr, "target_revisions", []) or []
        if revs:
            ax.text(0.05, y, f"Target revisions ({len(revs)} firms):",
                    fontsize=8.5, color=STYLE["muted"], va="top")
            y -= 0.018
            for r in revs[:3]:
                ln = f"  {r.get('date','?')} {r.get('firm','?')[:24]}: {r.get('from_grade','?')} → {r.get('to_grade','?')} ({r.get('action','?')})"
                ax.text(0.05, y, _strip_emoji(ln)[:100], fontsize=8.0, color=STYLE["body"], va="top")
                y -= 0.016
    else:
        ax.text(0.05, y, "—", fontsize=9, color=STYLE["muted"], va="top")
        y -= 0.020
    y -= 0.005

    # 세그먼트 (top 4)
    segments = tr.get("segments") or []
    if segments:
        ax.text(0.04, y, "Segments", fontsize=11, fontweight="bold", color=STYLE["navy"], va="top")
        y -= 0.022
        for seg in segments[:4]:
            name = seg.get("name") if isinstance(seg, dict) else str(seg)
            rev = seg.get("revenue") if isinstance(seg, dict) else None
            yoy = seg.get("yoy") if isinstance(seg, dict) else None
            note = seg.get("note") or seg.get("callout") if isinstance(seg, dict) else None
            line = f"{name or '?'}: {rev or '—'} · YoY {yoy or '—'}"
            if note:
                line += f" — {_strip_md_inline_keep_text(str(note))[:60]}"
            ax.text(0.05, y, _strip_emoji(line)[:110], fontsize=8.8, color=STYLE["ink"], va="top")
            y -= 0.018
        y -= 0.005

    # 경영진 톤
    tone = tr.get("management_tone") or {}
    overall = tone.get("overall") if isinstance(tone, dict) else None
    if overall:
        ax.text(0.04, y, "Management Tone", fontsize=11, fontweight="bold", color=STYLE["navy"], va="top")
        y -= 0.022
        for ln in _wrap_text_kr(_strip_md_inline_keep_text(str(overall)), width=92)[:3]:
            ax.text(0.05, y, ln, fontsize=9, color=STYLE["body"], va="top")
            y -= 0.020
        y -= 0.005

    # 서프라이즈 & 리스크
    surprises = tr.get("surprises_and_risks") or []
    if surprises:
        ax.text(0.04, y, "Surprises & Risks", fontsize=11, fontweight="bold", color=STYLE["navy"], va="top")
        y -= 0.022
        for item in surprises[:5]:
            txt = item if isinstance(item, str) else (item.get("text") or item.get("desc") or str(item))
            cleaned = _strip_md_inline_keep_text(str(txt))
            wrapped = _wrap_text_kr(cleaned, width=90)
            if not wrapped:
                continue
            ax.text(0.05, y, "• " + wrapped[0][:90], fontsize=9, color=STYLE["body"], va="top")
            y -= 0.020
            for w in wrapped[1:2]:
                ax.text(0.075, y, w[:90], fontsize=9, color=STYLE["body"], va="top")
                y -= 0.020
            if y < 0.10:
                break

    pdf.savefig(fig)
    plt.close(fig)


def _draw_ticker_qna_page(pdf, ticker: str, company: str, period: str, korean_md: str) -> None:
    """종목별 한글 번역 본문 페이지 (1-2p, V1 영문 verbatim 7p 대비 압축).

    Phase 9 `_step_translate_transcript` (Sonnet 1회/종목) 결과를 `_render_markdown_block`에
    위임 — markdown 헤딩/굵게/blockquote/표가 시각 강조로 렌더됨. 영문 verbatim 인용은
    한글 본문 내 blockquote 박스에 자동 매핑.

    Args:
        korean_md: Sonnet 한글 번역 markdown 본문 (3500-5500자 권장). 빈 문자열이면 호출 skip
            (caller 책임).
    """
    title = f"{ticker} — {company} · {period} · 한글 번역"
    _render_markdown_block(pdf, title, korean_md, footer=ticker)


# ==================================================================
# Phase 10-G — PDF lint (마크다운 잔존 검사)
# ==================================================================
def _lint_pdf(path: Path) -> dict:
    """raw markdown 잔존 검사. pdfminer 없으면 skip."""
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        text = extract_text(str(path))
    except Exception:
        return {"ok": True, "reason": "pdfminer unavailable"}
    md_hits = len(re.findall(r"^##\s|\*\*\w|\[[A-Z]+\]\[[a-z_]+\]", text, re.MULTILINE))
    pages = text.count("\f") + 1 if text else 0
    return {"ok": md_hits < 5, "md_hits": md_hits, "pages": pages}


# ------------------------------------------------------------------
# build_pdf — 전체 페이지 오케스트레이션 (Phase 8 신규 순서)
# ------------------------------------------------------------------
def build_pdf(
    output_path: Path,
    *,
    tickers: list[str],
    fiscal_period: str,
    transcripts: dict[str, dict],
    financials_by_ticker: dict[str, Any],
    industry_summary_kr: str,
    custom_question: str = "",
    custom_answer_kr: str = "",
    verify_lines: list[str] | None = None,
    # Phase 8 신규 인자 (모두 backward-compat — 미전달 시 빈 dict/str)
    consensus_by_ticker: dict[str, Any] | None = None,
    consensus_delta_by_ticker: dict[str, list] | None = None,
    market_reaction_by_ticker: dict[str, Any] | None = None,
    counter_summary: str = "",
    meta_text: str = "",
    # Phase 10 신규 인자 (V2 — Phase 9 결과 재활용, 모두 backward-compat)
    korean_translations: dict[str, str] | None = None,
    editors_pick_kr: str = "",
) -> Path | None:
    """전체 PDF 빌드. EARNINGS_PDF_V2=1 (기본): Phase 10 가독성 surgery 경로 / =0: 기존 V1."""
    try:
        from matplotlib.backends.backend_pdf import PdfPages
        from src.earnings import charts

        plt = charts.setup_matplotlib_safe()
        _setup_korean_font()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        consensus_by_ticker = consensus_by_ticker or {}
        consensus_delta_by_ticker = consensus_delta_by_ticker or {}
        market_reaction_by_ticker = market_reaction_by_ticker or {}
        korean_translations = korean_translations or {}

        use_v2 = os.getenv("EARNINGS_PDF_V2", "1") == "1"
        log.debug("[pdf] entry v2=%s pick=%d ko=%d",
                  use_v2, len(editors_pick_kr or ""), len(korean_translations or {}))

        with PdfPages(str(output_path)) as pdf:
            if use_v2:
                # ====================================================
                # Phase 10 V2 — 네이버 톤 · 11p 압축
                # ====================================================
                # p.1 — Cover + TL;DR + KPI + bull/bear (통합)
                _draw_cover(
                    pdf,
                    tickers=tickers,
                    fiscal_period=fiscal_period,
                    editors_pick_kr=editors_pick_kr,
                    synthesis_text=industry_summary_kr or "",
                    transcripts=transcripts,
                    consensus_by_ticker=consensus_by_ticker,
                    consensus_delta_by_ticker=consensus_delta_by_ticker,
                    market_reaction_by_ticker=market_reaction_by_ticker,
                    custom_question=custom_question,
                )

                # p.2 — Market Reaction
                _draw_market_reaction_page(
                    pdf,
                    tickers=tickers,
                    market_reaction_by_ticker=market_reaction_by_ticker,
                    consensus_delta_by_ticker=consensus_delta_by_ticker,
                    synthesis_text=industry_summary_kr or "",
                )

                # p.3 — Editor's Pick 한글 본문 (Phase 9 Opus 재활용)
                if editors_pick_kr:
                    _render_markdown_block(
                        pdf,
                        "Editor's Pick — 시장 기대 vs 진짜 중요했던 것",
                        editors_pick_kr,
                        footer="Editor's Pick",
                    )

                # p.4+ — Evidence (Opus 비교 합성 markdown 시각 강조 렌더)
                if industry_summary_kr:
                    _render_markdown_block(
                        pdf,
                        "Evidence — 산업 분위기 & 인사이트",
                        industry_summary_kr,
                        footer="Evidence",
                    )

                # p.N — Chart Grid (12p → 1p)
                if financials_by_ticker:
                    _draw_chart_grid_page(pdf, financials_by_ticker, industry_summary_kr or "")

                # Verify cross-check (있을 때)
                if verify_lines:
                    _render_markdown_block(
                        pdf,
                        "숫자 교차검증 — 어닝콜 ↔ SEC EDGAR",
                        "\n".join("- " + ln for ln in verify_lines),
                        footer="Verify",
                    )

                # Counter + Meta (있으면 합쳐서 1-2페이지)
                combined = []
                if counter_summary:
                    combined.append("# Counter-thesis (자동 반박)\n\n" + counter_summary)
                if meta_text:
                    combined.append("# Perspective Consensus & Conflicts (Meta)\n\n" + meta_text)
                if combined:
                    _render_markdown_block(
                        pdf,
                        "Counter-thesis & Multi-Perspective Meta",
                        "\n\n".join(combined),
                        footer="Counter + Meta",
                    )

                # 종목별 KPI + 한글 Q&A (1-2p / 종목)
                for ticker in tickers:
                    tr = transcripts.get(ticker)
                    if not tr:
                        continue
                    _draw_ticker_kpi_page(
                        pdf,
                        ticker=ticker,
                        tr=tr,
                        mr=market_reaction_by_ticker.get(ticker),
                        consensus=consensus_by_ticker.get(ticker),
                        consensus_delta=consensus_delta_by_ticker.get(ticker) or [],
                    )
                    ko = korean_translations.get(ticker)
                    if ko:
                        _draw_ticker_qna_page(
                            pdf,
                            ticker=ticker,
                            company=tr.get("company_name", "") or "",
                            period=tr.get("fiscal_period", "") or "",
                            korean_md=ko,
                        )
                    else:
                        # Fallback V1: format_transcript_text 한글 요약
                        try:
                            from src.earnings.transcripts import format_transcript_text
                            body = format_transcript_text(tr)
                            _render_markdown_block(
                                pdf,
                                f"{ticker} — {tr.get('company_name', '')} · {tr.get('fiscal_period', '')}",
                                body,
                                footer=ticker,
                            )
                        except Exception:
                            log.exception("V1 폴백 format_transcript_text 실패 (%s)", ticker)

                # 커스텀 분석 (있을 때)
                if custom_question and custom_answer_kr:
                    _render_markdown_block(
                        pdf,
                        f"커스텀 분석 — {custom_question[:60]}",
                        custom_answer_kr,
                        footer="Custom Analysis",
                    )

                # Appendix — 재무 raw 표
                if financials_by_ticker:
                    _draw_financial_table(pdf, financials_by_ticker)

            else:
                # ====================================================
                # V1 (legacy, EARNINGS_PDF_V2=0) — 23p 기존 흐름 보존
                # ====================================================
                _draw_decision_page(
                    pdf, tickers=tickers, fiscal_period=fiscal_period,
                    synthesis_text=industry_summary_kr or "", transcripts=transcripts,
                    consensus_by_ticker=consensus_by_ticker,
                    consensus_delta_by_ticker=consensus_delta_by_ticker,
                    market_reaction_by_ticker=market_reaction_by_ticker,
                )
                _draw_market_reaction_page(
                    pdf, tickers=tickers,
                    market_reaction_by_ticker=market_reaction_by_ticker,
                    consensus_delta_by_ticker=consensus_delta_by_ticker,
                    synthesis_text=industry_summary_kr or "",
                )
                _draw_cover_v1(pdf, tickers, fiscal_period, custom_question)
                if industry_summary_kr:
                    _draw_long_text_pages(pdf, "Evidence — 산업 분위기 & 인사이트 (Opus)",
                                          industry_summary_kr, footer_prefix="Evidence")
                if verify_lines:
                    _draw_long_text_pages(pdf, "숫자 교차검증 — 어닝콜 ↔ SEC EDGAR",
                                          "\n".join(verify_lines), footer_prefix="Verify")
                if meta_text:
                    _draw_long_text_pages(pdf, "Perspective Consensus & Conflicts (Bull/Bear/Neutral Meta)",
                                          meta_text, footer_prefix="Meta")
                if financials_by_ticker:
                    chart_specs = [
                        (charts.chart_capex_absolute, "9.1 CapEx (Absolute) — 6-Year"),
                        (charts.chart_capex_yoy, "9.2 CapEx YoY Growth"),
                        (charts.chart_fcf, "9.3 Free Cash Flow — 6-Year"),
                        (charts.chart_ocf_capex_ratio, "9.4 OCF / CapEx Ratio"),
                        (charts.chart_capex_intensity, "9.5 CapEx Intensity"),
                        (charts.chart_revenue, "9.6 Revenue — 6-Year"),
                    ]
                    for builder, title in chart_specs:
                        fig = builder(financials_by_ticker)
                        if fig is None:
                            continue
                        sect_id = title.split(" ")[0]
                        interp = _extract_section(industry_summary_kr or "", sect_id)
                        _draw_chart_interpretation_page(
                            pdf, fig, title=title, interpretation=interp,
                            footer=f"{title} · PDF p.charts",
                        )
                if counter_summary:
                    _draw_long_text_pages(pdf, "Counter-thesis (자동 반박 — Opus)",
                                          counter_summary, footer_prefix="Counter")
                for ticker in tickers:
                    tr = transcripts.get(ticker)
                    if not tr:
                        continue
                    from src.earnings.transcripts import format_transcript_text
                    _draw_long_text_pages(
                        pdf,
                        f"{ticker} — {tr.get('company_name', '')} · {tr.get('fiscal_period', '')}",
                        format_transcript_text(tr), footer_prefix=ticker,
                    )
                if custom_question and custom_answer_kr:
                    _draw_long_text_pages(pdf, f"커스텀 분석 — {custom_question[:60]}",
                                          custom_answer_kr, footer_prefix="Custom Analysis")
                if financials_by_ticker:
                    _draw_financial_table(pdf, financials_by_ticker)

        # Phase 10-G lint (v2일 때만)
        lint_info = ""
        if use_v2:
            try:
                lint = _lint_pdf(output_path)
                lint_info = f" lint={lint}"
                if not lint.get("ok"):
                    log.warning("PDF lint 경고: %s", lint)
            except Exception:
                log.exception("PDF lint 실패")

        log.info("PDF 생성 완료: %s (%d bytes) v2=%s%s",
                 output_path, output_path.stat().st_size, use_v2, lint_info)
        return output_path
    except Exception:
        log.exception("PDF 빌드 실패")
        return None
