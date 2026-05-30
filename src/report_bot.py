"""버터대디봇 (MarketReportBot) — 차트 기반 시장 색깔 진단 리포트.

매일 08:00 KST 자동 생성 (미국 직전 거래일 마감 + 한국 당일 개장 전).
파이프라인: 데이터 fetch → 차트 생성 → 신호 탐지 → LLM 내러티브 → Markdown/PDF → 발송.

명령: /start /help /report(즉시 생성). 발송 대상: REPORT_CHAT_ID / REPORT_ALLOWED_CHAT_IDS.
실데이터 미확보 항목은 graceful skip (벤치마크 §품질기준 fallback).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from src.bot_helpers import is_authorized, send_pdf, send_text_chunked

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ALLOWED_ENV = "REPORT_ALLOWED_CHAT_IDS"
CHAT_ID_ENV = "REPORT_CHAT_ID"

HELP_TEXT = (
    "📊 *버터대디봇* — 차트 기반 시장 색깔 진단 리포트\n\n"
    "매일 08:00 KST 자동 발송 (미국 직전 거래일 마감 + 한국 당일).\n"
    "내용: 미국 4대 지수·히트맵·매크로·ETF + 한국 수급 멀티패널 + LLM 시황 내러티브.\n\n"
    "명령:\n"
    "  /report — 지금 즉시 리포트 생성 (~3-7분)\n"
    "  /help — 도움말\n"
)


def _parse_chat_ids(*env_keys: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for key in env_keys:
        for token in (os.getenv(key, "") or "").split(","):
            m = re.search(r"-?\d{6,}", token)
            if m and m.group(0) not in seen:
                seen.add(m.group(0)); out.append(m.group(0))
    return out


async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        log.exception("help 실패")


async def _cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await update.message.reply_text("🔒 권한이 없습니다.")
        return
    await update.message.reply_text("📊 리포트 생성 시작 (~3-7분 소요)...")
    await report_daily_job(context.bot, override_chat_id=str(update.effective_chat.id))


# ------------------------------------------------------------------
# 메인 파이프라인
# ------------------------------------------------------------------
async def report_daily_job(bot: Bot, override_chat_id: str | None = None) -> None:
    log.info("[report] daily_job 시작 override=%s", override_chat_id)
    if override_chat_id:
        targets = [str(override_chat_id)]
    else:
        targets = _parse_chat_ids(CHAT_ID_ENV, ALLOWED_ENV)
    if not targets:
        log.error("[report] 발송 대상 없음")
        return

    loop = asyncio.get_running_loop()
    try:
        md_path, pdf_path, key_charts, headline = await loop.run_in_executor(None, _build_report)
    except Exception:
        log.exception("[report] 빌드 실패")
        for cid in targets:
            await send_text_chunked(bot, cid, "⚠️ 리포트 생성 실패 — 로그 확인")
        return

    if not md_path:
        for cid in targets:
            await send_text_chunked(bot, cid, "⚠️ 리포트 데이터 미확보 — 생성 중단")
        return

    # 발송: 모든 차트가 맥락에 박힌 PDF "한 개"만. (PDF 실패 시에만 텍스트 폴백)
    md_text = Path(md_path).read_text(encoding="utf-8")
    for cid in targets:
        try:
            if pdf_path and Path(pdf_path).exists():
                await send_pdf(bot, cid, Path(pdf_path), caption=f"📊 {headline}"[:1000])
            else:
                await send_text_chunked(bot, cid, f"📊 {headline}")
                await send_text_chunked(bot, cid, md_text)
        except Exception:
            log.exception("[report] 발송 실패 cid=%s", cid)
    log.info("[report] 발송 완료 (%d명, 단일 PDF)", len(targets))


def _build_report():
    """동기 빌드 (run_in_executor). 8섹션 + 전일 대비 팔로업.

    반환: (md_path, pdf_path, key_chart_paths, headline).
    """
    from src.report.data import (fetch_prices as fp, fetch_macro, fetch_korea_flows,
                                  fetch_news, fetch_earnings, cache)
    from src.report.charts import (index_charts, volatility_chart, korea_flow_chart,
                                   heatmap_chart, signal_charts, rotation_charts,
                                   flow_charts, stock_highlights)
    from src.report.analysis import technical_signals, rotation_classifier, theme_momentum
    from src.report.writer import report_writer
    from src.report import state

    today = datetime.now(KST).date()
    date_iso = today.strftime("%Y-%m-%d")
    base = Path("reports") / date_iso
    img_dir = base / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    chart_list: list[dict] = []
    key_charts: list[str] = []
    signals: list[dict] = []

    def add(fn, title, hint, section, key=False):
        if fn:
            chart_list.append({"filename": fn, "title": title, "caption_hint": hint, "section": section})
            if key:
                key_charts.append(str(img_dir / fn))

    # ---------- 데이터 fetch (단일소스 실패 시 전일 캐시 재사용) ----------
    stale: list[dict] = []
    def foc(key, fn):
        return cache.fetch_or_cache(key, fn, date_iso, stale=stale)
    us_idx = foc("idx", lambda: fp.fetch_many(fp.US_INDICES, days=365))
    deconc = foc("deconc", lambda: fp.fetch_many(fp.DECONCENTRATION, days=365))
    sector_etfs = foc("sector", lambda: fp.fetch_many(fp.US_SECTOR_ETFS, days=365))
    theme_etfs = foc("theme", lambda: fp.fetch_many(fp.US_THEME_ETFS, days=365))
    region_etfs = foc("region", lambda: fp.fetch_many(fp.REGION_ETFS, days=365))
    fx = foc("fx", lambda: fp.fetch_many(fp.FX_SYMBOLS, days=365))
    highlights_df = foc("highlights", lambda: fp.fetch_many(fp.HIGHLIGHT_STOCKS, days=365))
    macro = foc("macro", lambda: fetch_macro.fetch_macro(days=365))
    fred = foc("macro_fred", lambda: fetch_macro.fetch_fred_many())
    kr_sizes = foc("kr_size", lambda: fetch_korea_flows.fetch_size_index_ohlcv(days=365))
    kr_flows = foc("kr_flow", lambda: fetch_korea_flows.fetch_investor_flows(days=120))
    news = foc("news", lambda: fetch_news.fetch_market_news())
    earnings = cache.fetch_or_cache("earnings", lambda: fetch_earnings.fetch_earnings_momentum(),
                                    date_iso, stale=stale,
                                    is_empty=lambda o: not o or not o.get("recent"))
    log.info("[report] fetch: idx=%d deconc=%d sector=%d theme=%d region=%d fx=%d hl=%d macro=%d fred=%d kr_size=%d kr_flow=%d news=%d earn=%s stale=%s",
             len(us_idx), len(deconc), len(sector_etfs), len(theme_etfs), len(region_etfs),
             len(fx), len(highlights_df), len(macro), len(fred), len(kr_sizes), len(kr_flows),
             len(news or []), bool(earnings), [s["key"] for s in stale])

    # 테마 모멘텀 (섹터 + 테마 통합)
    combined_themes = {**sector_etfs, **theme_etfs}
    theme_rows = theme_momentum.compute(combined_themes)
    theme_summary = theme_momentum.summarize(theme_rows)
    log.info("[report] 테마 모멘텀 %d개 (hot=%s)", len(theme_rows), theme_summary.get("hot"))

    for label, df in us_idx.items():
        signals += technical_signals.detect_signals(df, label)

    # ---------- §0 글로벌 위험선호 (표지 직후 시각 요약) ----------
    risk_gauge: dict = {"score": 50, "label": "중립", "signals": {}}
    try:
        from src.report.data import fetch_global_risk
        from src.report.charts import global_risk_matrix
        risk_rows, risk_gauge = fetch_global_risk.load_risk_map(days=220)
        if risk_rows:
            add(global_risk_matrix.risk_matrix(risk_rows, risk_gauge, img_dir, date_iso=date_iso),
                "글로벌 위험선호 매트릭스",
                f"자산 클래스별 1D·5D · {risk_gauge.get('label')} {risk_gauge.get('score')}/100",
                "0. 글로벌 위험선호", key=True)
            log.info("[report] §0 글로벌 위험선호: %d자산·게이지 %d(%s)",
                     len(risk_rows), risk_gauge.get("score"), risk_gauge.get("label"))
    except Exception:
        log.exception("[report] 글로벌 위험선호 매트릭스 실패 — 생략")

    # ---------- §1 매크로 ----------
    add(index_charts.us_indices_grid(us_idx, img_dir, date_iso=date_iso),
        "미국 4대 지수", "4대 지수 등락", "1. 매크로 컨텍스트")
    add(index_charts.indices_normalized(us_idx, img_dir, date_iso=date_iso),
        "미국 4대 지수 (1Y 리베이스)", "상대 강도", "1. 매크로 컨텍스트")
    if macro:
        add(volatility_chart.rates_curve(macro, img_dir, date_iso=date_iso),
            "미국 국채 금리 곡선", "3M·10Y·30Y + 장단기 스프레드", "1. 매크로 컨텍스트", key=True)
        add(volatility_chart.oil_chart(macro, img_dir, date_iso=date_iso),
            "국제 유가", "WTI·Brent", "1. 매크로 컨텍스트")
        add(volatility_chart.single_macro(macro, img_dir, "23_macro_dxy.png", "달러 인덱스", date_iso=date_iso),
            "달러 인덱스", "DXY — 위험선호 척도", "1. 매크로 컨텍스트")
        add(volatility_chart.vol_chart(macro, img_dir, date_iso=date_iso),
            "변동성 게이지", "VIX·OVX", "1. 매크로 컨텍스트", key=True)
        add(volatility_chart.single_macro(macro, img_dir, "24_macro_btc.png", "비트코인", date_iso=date_iso),
            "비트코인", "위험선호 자산", "1. 매크로 컨텍스트")
    for i, (label, df) in enumerate(fred.items(), 1):
        add(volatility_chart.macro_line(df, label, img_dir, f"04_fred_{i:02d}.png"),
            label, "FRED 매크로", "1. 매크로 컨텍스트")

    # ---------- §2 쏠림 둔화 시그널 (메인) ----------
    add(signal_charts.deconcentration_signal(deconc, img_dir, date_iso=date_iso),
        "쏠림 둔화 시그널", "RSP(동일가중) vs SPY/QQQ/M7", "2. 쏠림 둔화 시그널", key=True)
    if deconc.get("RSP") is not None and deconc.get("SPY") is not None:
        add(signal_charts.rsp_spy_ratio(deconc["RSP"], deconc["SPY"], img_dir, date_iso=date_iso),
            "RSP/SPY 비율", "동일가중 우위 여부", "2. 쏠림 둔화 시그널", key=True)
    rsp_new_high = bool(technical_signals.analyze(deconc.get("RSP")).get("is_new_high")) if deconc.get("RSP") is not None else False

    # ---------- §3 섹터 로테이션 맵 (S&P500 개별종목 히트맵 우선 + 테마 로테이션) ----------
    breadth: dict = {}
    try:
        from src.report.data import fetch_us_breadth
        caps, changes, sectors, industries = fetch_us_breadth.load_us_market_map(top_n=120)
        log.info("[report] S&P500 맵: caps=%d sectors=%d industries=%d changes=%d",
                 len(caps), len(sectors), len(industries), len(changes))
        if changes:  # TradingView식 개별 종목 시총 트리맵 — §3 최상단(시그니처)
            add(heatmap_chart.sp500_heatmap(caps, changes, sectors, img_dir,
                date_iso=date_iso, industries=industries),
                "S&P500 히트맵", "개별 종목 시총 가중 — 종목명·당일등락 (녹=상승)", "3. 섹터 로테이션 맵", key=True)
            adv = sum(1 for v in changes.values() if v > 0)
            breadth["advancers"] = adv
            breadth["total"] = len(changes)
        else:
            log.warning("[report] S&P500 히트맵 등락 0개 — 생략")
    except Exception:
        log.exception("[report] S&P500 히트맵 실패 — 생략")

    add(heatmap_chart.theme_rotation_heatmap(theme_rows, img_dir, date_iso=date_iso),
        "테마 로테이션 히트맵", "5일 모멘텀 — 돈이 어디로", "3. 섹터 로테이션 맵", key=True)
    add(rotation_charts.sector_return_bars(theme_rows, img_dir, date_iso=date_iso),
        "섹터·테마 상대강도", "1M/3M 정렬", "3. 섹터 로테이션 맵")
    add(rotation_charts.region_compare(region_etfs, img_dir, date_iso=date_iso),
        "글로벌 지역 비교", "지역 디커플링", "3. 섹터 로테이션 맵")
    # 테마 자금 흐름 시계열 (B: 20일 누적, 좌 유입·우 이탈) — 시간 진화 통찰
    add(flow_charts.theme_flow_timeline_chart(theme_rows, combined_themes, img_dir,
                                              date_iso=date_iso),
        "테마 자금 흐름 시계열", "20일 누적 — 어디서 언제부터 빠져 어디로",
        "3. 섹터 로테이션 맵", key=True)
    for i, r in enumerate(theme_rows[:16], 1):
        lbl = r["label"]
        add(index_charts.theme_chart(combined_themes.get(lbl), lbl, img_dir,
            f"12_theme_{i:02d}.png", date_iso=date_iso), lbl, "캔들+MA+거래량", "3. 섹터 로테이션 맵")

    # 200일선 위 테마 비율 (breadth proxy)
    if theme_rows:
        above = sum(1 for r in theme_rows if r.get("above_ma200"))
        breadth["pct_above_200ma"] = round(above / len(theme_rows) * 100, 1)

    # ---------- §4 IPO·환전 임팩트 ----------
    ewy = region_etfs.get("한국(EWY)")
    if fx.get("USD/KRW") is not None and ewy is not None:
        add(flow_charts.usdkrw_ewy_dual(fx["USD/KRW"], ewy, img_dir, date_iso=date_iso),
            "USD/KRW ↔ EWY", "환전 압력 (가설)", "4. IPO·환전 임팩트", key=False)

    # ---------- §6 개별 종목 하이라이트 ----------
    add(stock_highlights.highlight_grid(highlights_df, img_dir, date_iso=date_iso),
        "개별 종목 하이라이트", "스토리 종목", "6. 개별 종목")
    highlights_meta = []
    for label, df in highlights_df.items():
        dc = fp.day_change(df)
        if dc is not None:
            highlights_meta.append({"label": label, "chg": round(dc, 2)})

    # ---------- §7 한국시장 자금흐름 ----------
    korea_summary: dict = {}
    for i, (label, price_df) in enumerate(kr_sizes.items(), 1):
        flows_df = kr_flows.get("KOSDAQ") if "KOSDAQ" in label else kr_flows.get("KOSPI")
        fn = korea_flow_chart.flow_multipanel(price_df, flows_df, label, img_dir, f"32_kr_{i:02d}.png")
        add(fn, f"한국 {label} 수급", "가격·이격·기관·외국인·개인 누적순매수", "7. 한국시장 자금흐름",
            key=("대형" in label or "KOSDAQ" in label))
    add(heatmap_chart.korea_flow_heatmap(kr_flows, img_dir, date_iso=date_iso),
        "한국 수급 히트맵", "투자자 방향 (20일)", "7. 한국시장 자금흐름")
    add(flow_charts.us_korea_linkage(theme_rows, img_dir, date_iso=date_iso),
        "미국→한국 연결 흐름", "강한 미국 테마 → 국내 수혜주", "7. 한국시장 자금흐름", key=True)
    for mkt, fdf in (kr_flows or {}).items():
        korea_summary[mkt] = {inv: round(float(fdf[inv].iloc[-20:].sum()), 0) for inv in fdf.columns}

    # ---------- §8 종합 자금흐름 다이어그램 (Sankey, Risk 게이지 허브 통합) ----------
    src_ep, dst_ep = theme_momentum.flow_endpoints(theme_rows)
    add(flow_charts.capital_flow_diagram(src_ep, dst_ep, img_dir, date_iso=date_iso,
                                          gauge_score=risk_gauge.get("score"),
                                          gauge_label=risk_gauge.get("label")),
        "종합 자금흐름 다이어그램", "Sankey · 굵기=강도 · 허브=Risk 게이지",
        "8. 종합 자금흐름", key=True)

    if not chart_list:
        log.error("[report] 차트 0개 — 데이터 전부 미확보")
        return None, None, [], ""

    # ---------- 시장 색깔 + 전일 대비 deltas ----------
    classify_input = {"QQQ": deconc.get("QQQ"), "SPY": deconc.get("SPY"),
                      "RSP": deconc.get("RSP"), "IWM": us_idx.get("Russell2000")}
    rotation = rotation_classifier.classify({k: v for k, v in classify_input.items() if v is not None},
                                            breadth=breadth)
    macro_summary = {k: round(float(v["Close"].iloc[-1]), 2) for k, v in macro.items()}
    if fx.get("USD/KRW") is not None:
        macro_summary["USD/KRW"] = round(float(fx["USD/KRW"]["Close"].iloc[-1]), 2)

    snapshot = state.build_snapshot(date_iso, rotation, theme_summary, theme_rows,
                                    macro_summary, breadth, korea_summary, rsp_new_high)
    prev = state.load_previous(date_iso)
    deltas = state.compute_deltas(snapshot, prev)
    state.save_snapshot(date_iso, snapshot)
    log.info("[report] deltas 계산 (baseline=%s, notes=%d)", deltas.get("baseline"), len(deltas.get("notes", [])))

    # ---------- LLM 작성 (8섹션 + 팔로업) ----------
    log.info("[report] 차트 %d개, 신호 %d개 → LLM 작성", len(chart_list), len(signals))
    md = report_writer.write_report(date_iso, rotation, chart_list, signals, macro_summary,
                                    korea_summary, news=news, theme_momentum=theme_summary,
                                    deltas=deltas, breadth=breadth, highlights=highlights_meta,
                                    earnings=earnings, stale=stale)

    # headline 추출 (첫 # 라인)
    headline = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("#")), f"{date_iso} 시장 리포트")

    md_path = base / "report.md"
    md_path.write_text(md, encoding="utf-8")
    log.info("[report] markdown 저장: %s", md_path)

    # 7) PDF (pdf_export 재사용)
    pdf_path = None
    try:
        from src import pdf_export
        # 차트 이미지를 base64 data URI로 인라인.
        # Playwright set_content는 about:blank origin이라 로컬 파일 경로(/app/.. 나 file://)를
        # 로드하지 못함 → 이미지가 통째로 안 박힘. 자기완결 HTML로 만들어 확실히 렌더.
        import base64
        import re as _re

        def _inline_img(m):
            p = img_dir / m.group(1)
            if not p.exists():
                return ""
            return "](data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii") + ")"

        md_for_pdf = _re.sub(r"\]\(images/([^)\s]+)\)", _inline_img, md)
        out_pdf = base / "report.pdf"
        n_imgs = md_for_pdf.count("data:image/png;base64,")
        result = asyncio.run(pdf_export.markdown_to_pdf(md_for_pdf, out_pdf, headline))
        if result:
            pdf_path = str(out_pdf)
            size_mb = out_pdf.stat().st_size / 1_048_576
            log.info("[report] PDF 생성: %s (%.2f MB, 인라인 차트 %d개)", pdf_path, size_mb, n_imgs)
    except Exception:
        log.exception("[report] PDF 생성 실패 — markdown만")

    # 8) HTML 부가 산출 (모바일 열람용 — images/ 상대경로 유지)
    try:
        import markdown as _md
        html_body = _md.markdown(md, extensions=["tables", "fenced_code"])
        html = (f"<!doctype html><meta charset='utf-8'>"
                f"<title>{headline}</title>"
                "<style>body{max-width:880px;margin:24px auto;padding:0 16px;"
                "font-family:-apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;"
                "line-height:1.6;color:#1a1a1a}img{max-width:100%;height:auto;margin:8px 0;"
                "border:1px solid #eee;border-radius:6px}h1{font-size:1.5rem}h2{font-size:1.2rem;"
                "margin-top:1.6em;border-top:1px solid #eee;padding-top:.6em}</style>"
                f"<body>{html_body}</body>")
        (base / "report.html").write_text(html, encoding="utf-8")
    except Exception:
        log.exception("[report] HTML 생성 실패 — 생략")

    return str(md_path), pdf_path, key_charts, headline


# ------------------------------------------------------------------
async def _self_test(bot: Bot) -> None:
    chat_id = (_parse_chat_ids("REPORT_TEST_CHAT_ID", CHAT_ID_ENV, ALLOWED_ENV) or [""])[0]
    if not chat_id:
        log.warning("[report.self-test] chat_id 없음 — 스킵")
        return
    log.info("=" * 50)
    log.info("[report.self-test] 시작 chat_id=%s", chat_id)
    await asyncio.sleep(15)
    try:
        await report_daily_job(bot, override_chat_id=chat_id)
    except Exception:
        log.exception("[report.self-test] 예외")
    log.info("[report.self-test] 종료")


REPORT_COMMANDS = [
    ("report", "📊 즉시 시황 리포트 생성"),
    ("help", "도움말"),
]


def build_report_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(CommandHandler("report", _cmd_report))
    if os.getenv("REPORT_TEST_MODE", "0") == "1":
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_self_test(app.bot))
        except RuntimeError:
            pass
    return app
