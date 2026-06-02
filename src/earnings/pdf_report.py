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
    """consensus delta result list → {revenue: "...", eps: "...", surprise: "..."}."""
    out = {"revenue": "—", "eps": "—"}
    for r in deltas or []:
        msg = getattr(r, "message", str(r))
        low = msg.lower()
        if "revenue" in low and ("delta" in low or "vs" in low or "consensus" in low):
            out["revenue"] = msg
        elif ("eps" in low) and ("delta" in low or "vs" in low):
            out["eps"] = msg
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
def _draw_cover(pdf, tickers: list[str], period: str, custom_question: str) -> None:
    """표지 — 1면 (Decision Page가 메인이지만 표지도 유지)."""
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
) -> Path | None:
    """전체 PDF 빌드 (Phase 8 PM Decision Document)."""
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

        with PdfPages(str(output_path)) as pdf:
            # p.1 — Decision Page (verdict + KPI grid + bull/bear)
            _draw_decision_page(
                pdf,
                tickers=tickers,
                fiscal_period=fiscal_period,
                synthesis_text=industry_summary_kr or "",
                transcripts=transcripts,
                consensus_by_ticker=consensus_by_ticker,
                consensus_delta_by_ticker=consensus_delta_by_ticker,
                market_reaction_by_ticker=market_reaction_by_ticker,
            )

            # p.2 — Market Reaction
            _draw_market_reaction_page(
                pdf,
                tickers=tickers,
                market_reaction_by_ticker=market_reaction_by_ticker,
                consensus_delta_by_ticker=consensus_delta_by_ticker,
                synthesis_text=industry_summary_kr or "",
            )

            # p.3 — 표지 (간단)
            _draw_cover(pdf, tickers, fiscal_period, custom_question)

            # p.4-N — Evidence (synthesis §0~§10)
            if industry_summary_kr:
                _draw_long_text_pages(
                    pdf,
                    "Evidence — 산업 분위기 & 인사이트 (Opus)",
                    industry_summary_kr,
                    footer_prefix="Evidence",
                )

            # Verify cross-check
            if verify_lines:
                _draw_long_text_pages(
                    pdf,
                    "숫자 교차검증 — 어닝콜 ↔ SEC EDGAR",
                    "\n".join(verify_lines),
                    footer_prefix="Verify",
                )

            # Meta synthesis (§9 Perspective Consensus & Conflicts)
            if meta_text:
                _draw_long_text_pages(
                    pdf,
                    "Perspective Consensus & Conflicts (Bull/Bear/Neutral Meta)",
                    meta_text,
                    footer_prefix="Meta",
                )

            # Charts with interpretation — 차트당 1쪽 + 캡션 페이지
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
                    # synthesis §9.x 본문에서 해당 차트 해석 추출
                    sect_id = title.split(" ")[0]  # "9.1", "9.2", ...
                    interp = _extract_section(industry_summary_kr or "", sect_id)
                    if not interp:
                        # 폴백: §9 전체에서 첫 문단
                        s9 = _extract_section(industry_summary_kr or "", "9")
                        if s9 and sect_id in s9:
                            idx = s9.find(sect_id)
                            # 다음 9.x 까지
                            rest = s9[idx:]
                            nxt = re.search(r"\n\*\*9\.\d", rest[1:])
                            interp = rest[: nxt.start() + 1 if nxt else len(rest)][:1200]
                    _draw_chart_interpretation_page(
                        pdf, fig, title=title, interpretation=interp,
                        footer=f"{title} · PDF p.charts",
                    )

            # Counter-thesis (있을 때)
            if counter_summary:
                _draw_long_text_pages(
                    pdf,
                    "Counter-thesis (자동 반박 — Opus)",
                    counter_summary,
                    footer_prefix="Counter",
                )

            # 종목별 어닝콜 핵심 — Top Q&A 5 + significance
            for ticker in tickers:
                tr = transcripts.get(ticker)
                if not tr:
                    continue
                from src.earnings.transcripts import format_transcript_text
                body = format_transcript_text(tr)
                _draw_long_text_pages(
                    pdf,
                    f"{ticker} — {tr.get('company_name', '')} · {tr.get('fiscal_period', '')}",
                    body,
                    footer_prefix=ticker,
                )

            # 커스텀 분석 (있는 경우)
            if custom_question and custom_answer_kr:
                _draw_long_text_pages(
                    pdf,
                    f"커스텀 분석 — {custom_question[:60]}",
                    custom_answer_kr,
                    footer_prefix="Custom Analysis",
                )

            # 부록 — 재무 raw 표
            if financials_by_ticker:
                _draw_financial_table(pdf, financials_by_ticker)

        log.info("PDF 생성 완료: %s (%d bytes)", output_path, output_path.stat().st_size)
        return output_path
    except Exception:
        log.exception("PDF 빌드 실패")
        return None
