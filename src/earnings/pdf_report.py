"""어닝콜 PDF 보고서 빌더 (matplotlib PdfPages).

페이지 구성:
  1. 표지 — 제목, 분석 대상 기업 리스트, 분기, 생성 일자
  2. Executive Summary — 산업 분위기 1 page (한국어)
  3. 차트 모음 — CapEx (절대값/YoY), FCF, OCF/CapEx 비율, CapEx intensity, Revenue
  4. 기업별 어닝콜 핵심 — ticker마다 1-2 page
  5. (옵션) 커스텀 분석 — 사용자 질문에 대한 답
  6. 부록 — 재무 데이터 raw 표

격리: 모든 import는 build_pdf() 본문 안에서. 실패 시 None 반환.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


def _wrap_text(text: str, width: int = 95) -> list[str]:
    """단순 단어 단위 wrap (영문/한글 혼합 OK)."""
    import textwrap
    out: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            out.append("")
            continue
        # 한글 위주면 width 줄임
        han_ratio = sum(1 for c in line if ord(c) > 127) / max(len(line), 1)
        eff_width = int(width * (0.55 if han_ratio > 0.4 else 1.0))
        wrapped = textwrap.wrap(line, width=eff_width) or [""]
        out.extend(wrapped)
    return out


def _setup_korean_font():
    """한국어 폰트 등록 (Dockerfile의 fonts-noto-cjk). 미발견이어도 영문은 그려짐."""
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


def _draw_text_page(pdf, title: str, body: str, *, footer: str = "") -> None:
    """A4 종/seri text page. title 위쪽, body 본문, footer 아래."""
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(8.27, 11.69))  # A4
    ax = fig.add_axes([0.07, 0.05, 0.86, 0.90])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # 타이틀
    ax.text(
        0.0, 0.97, title,
        fontsize=16, fontweight="bold", va="top", ha="left", color="#1a1a1a",
    )
    ax.axhline(y=0.945, xmin=0.0, xmax=1.0, color="#888", linewidth=0.6)

    # 본문 wrap
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
        suffix = f" (cont.)" if start > 0 else ""
        _draw_text_page(
            pdf,
            title + suffix,
            "\n".join(chunk),
            footer=f"{footer_prefix} p.{page_num}" if footer_prefix else f"p.{page_num}",
        )


def _draw_cover(pdf, tickers: list[str], period: str, custom_question: str) -> None:
    """표지 페이지."""
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # 상단 배경 띠
    ax.add_patch(plt.Rectangle((0, 0.78), 1, 0.22, facecolor="#0b3d91", zorder=0))

    ax.text(
        0.5, 0.93, "US Earnings Call Brief",
        fontsize=28, fontweight="bold", va="center", ha="center", color="white",
    )
    ax.text(
        0.5, 0.86, "어닝콜 종합 분석 보고서",
        fontsize=14, va="center", ha="center", color="#cfe1ff",
    )

    # 본문
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
        "데이터: SEC EDGAR (XBRL US-GAAP) · 어닝콜 전문: 웹 검색 (perplexity)",
        fontsize=8, ha="center", va="center", color="#999",
    )
    pdf.savefig(fig)
    plt.close(fig)


def _draw_chart_page(pdf, fig) -> None:
    """matplotlib Figure → PDF 페이지. fig는 호출자가 만들어 넘김. 저장 후 close."""
    import matplotlib.pyplot as plt
    if fig is None:
        return
    try:
        # A4 비율로 사이즈 조정 — chart 모듈은 11x6.5
        fig.set_size_inches(8.27, 5.5)
        pdf.savefig(fig, bbox_inches="tight")
    finally:
        plt.close(fig)


def _draw_financial_table(pdf, financials_by_ticker: dict[str, Any]) -> None:
    """부록: 6년치 재무 raw 표 (CapEx / OCF / FCF / Revenue)."""
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

    # 표 데이터 구성: ticker, fy, capex, ocf, fcf, revenue
    rows: list[list[str]] = []
    for ticker, fin in financials_by_ticker.items():
        cap_by = {p.fy: p.val for p in fin.capex}
        ocf_by = {p.fy: p.val for p in fin.ocf}
        rev_by = {p.fy: p.val for p in fin.revenue}
        fcf_by = {p.fy: p.val for p in fin.fcf()}
        all_fys = sorted(set(list(cap_by.keys()) + list(ocf_by.keys()) + list(rev_by.keys())), reverse=True)[:6]
        for fy in all_fys:
            rows.append([
                ticker,
                f"FY{fy}",
                fmt_usd(cap_by[fy]) if fy in cap_by else "—",
                fmt_usd(ocf_by[fy]) if fy in ocf_by else "—",
                fmt_usd(fcf_by[fy]) if fy in fcf_by else "—",
                fmt_usd(rev_by[fy]) if fy in rev_by else "—",
            ])
        rows.append(["", "", "", "", "", ""])  # 구분 행

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
    # 헤더 폰트 흰색
    for j in range(len(col_labels)):
        cell = tbl[0, j]
        cell.set_text_props(color="white", fontweight="bold")
    # 행 색 alternation
    for i in range(1, len(rows) + 1):
        for j in range(len(col_labels)):
            if i % 2 == 0:
                tbl[i, j].set_facecolor("#f5f7fa")

    pdf.savefig(fig)
    plt.close(fig)


def build_pdf(
    output_path: Path,
    *,
    tickers: list[str],
    fiscal_period: str,
    transcripts: dict[str, dict],  # {ticker: transcript_dict}
    financials_by_ticker: dict[str, Any],  # {ticker: CompanyFinancials}
    industry_summary_kr: str,
    custom_question: str = "",
    custom_answer_kr: str = "",
    verify_lines: list[str] | None = None,
) -> Path | None:
    """전체 PDF 빌드. 성공 시 output_path 반환, 실패 None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages

        from src.earnings import charts

        _setup_korean_font()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with PdfPages(str(output_path)) as pdf:
            # 1. 표지
            _draw_cover(pdf, tickers, fiscal_period, custom_question)

            # 2. 비교 합성 (Opus, 딥리서치급)
            if industry_summary_kr:
                _draw_long_text_pages(
                    pdf,
                    "비교 분석 — 산업 분위기 & 인사이트 (Opus)",
                    industry_summary_kr,
                    footer_prefix="Comparison",
                )

            # 2.5 숫자 교차검증 (콜 ↔ SEC)
            if verify_lines:
                _draw_long_text_pages(
                    pdf,
                    "숫자 교차검증 — 어닝콜 ↔ SEC EDGAR",
                    "\n".join(verify_lines),
                    footer_prefix="Verify",
                )

            # 3. 차트 (재무 데이터가 있는 경우만)
            if financials_by_ticker:
                for builder, title in [
                    (charts.chart_capex_absolute, "CapEx (Absolute)"),
                    (charts.chart_capex_yoy, "CapEx YoY Growth"),
                    (charts.chart_fcf, "Free Cash Flow"),
                    (charts.chart_ocf_capex_ratio, "OCF / CapEx Ratio"),
                    (charts.chart_capex_intensity, "CapEx Intensity"),
                    (charts.chart_revenue, "Revenue"),
                ]:
                    fig = builder(financials_by_ticker)
                    if fig is not None:
                        _draw_chart_page(pdf, fig)

            # 4. 종목별 어닝콜 핵심
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

            # 5. 커스텀 분석 (있는 경우)
            if custom_question and custom_answer_kr:
                _draw_long_text_pages(
                    pdf,
                    f"커스텀 분석 — {custom_question[:60]}",
                    custom_answer_kr,
                    footer_prefix="Custom Analysis",
                )

            # 6. 부록 — 재무 데이터 raw 표
            if financials_by_ticker:
                _draw_financial_table(pdf, financials_by_ticker)

        log.info("PDF 생성 완료: %s (%d bytes)", output_path, output_path.stat().st_size)
        return output_path
    except Exception:
        log.exception("PDF 빌드 실패")
        return None
