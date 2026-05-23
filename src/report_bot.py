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

    # 발송: 헤드라인 + 핵심 차트 3개 + PDF + (md 본문 청크)
    md_text = Path(md_path).read_text(encoding="utf-8")
    for cid in targets:
        try:
            await send_text_chunked(bot, cid, f"📊 {headline}")
            for ch in key_charts[:3]:
                try:
                    with open(ch, "rb") as f:
                        await bot.send_photo(chat_id=cid, photo=f)
                except Exception:
                    log.exception("[report] 차트 발송 실패 %s", ch)
            if pdf_path and Path(pdf_path).exists():
                await send_pdf(bot, cid, Path(pdf_path), caption=headline[:200])
            else:
                await send_text_chunked(bot, cid, md_text)
        except Exception:
            log.exception("[report] 발송 실패 cid=%s", cid)
    log.info("[report] 발송 완료 (%d명)", len(targets))


def _build_report():
    """동기 빌드 (run_in_executor). 반환: (md_path, pdf_path, key_chart_paths, headline)."""
    from src.report.data import fetch_prices, fetch_macro, fetch_korea_flows
    from src.report.charts import index_charts, volatility_chart, korea_flow_chart, heatmap_chart
    from src.report.analysis import technical_signals, rotation_classifier
    from src.report.writer import report_writer

    today = datetime.now(KST).date()
    date_iso = today.strftime("%Y-%m-%d")
    base = Path("reports") / date_iso
    img_dir = base / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    chart_list: list[dict] = []
    key_charts: list[str] = []
    signals: list[dict] = []

    # 1) 미국 지수
    us_idx = fetch_prices.fetch_many(fetch_prices.US_INDICES, days=180)
    log.info("[report] 미국 지수 %d개", len(us_idx))
    if us_idx:
        fn = index_charts.us_indices_grid(us_idx, img_dir)
        if fn:
            chart_list.append({"filename": fn, "title": "미국 4대 지수", "caption_hint": "4대 지수 등락과 내부 색깔"})
            key_charts.append(str(img_dir / fn))
        for label, df in us_idx.items():
            signals += technical_signals.detect_signals(df, label)

    # 2) 미국 ETF (섹터 로테이션 판단)
    us_etf = fetch_prices.fetch_many(fetch_prices.US_ETFS, days=300)
    log.info("[report] 미국 ETF %d개", len(us_etf))
    rotation = rotation_classifier.classify(us_etf)
    for i, (label, df) in enumerate(us_etf.items(), 1):
        fn = index_charts.etf_chart(df, label, img_dir, f"10_etf_{i:02d}_{label}.png")
        if fn:
            chart_list.append({"filename": fn, "title": f"{label} ETF", "caption_hint": f"{label} 추세/MA/52주 위치"})
        signals += technical_signals.detect_signals(df, label)

    # 3) 매크로
    macro = fetch_macro.fetch_macro(days=180)
    if macro:
        fn = volatility_chart.macro_grid(macro, img_dir)
        if fn:
            chart_list.append({"filename": fn, "title": "매크로 대시보드", "caption_hint": "금리·유가·달러·VIX 흐름과 시장 반응"})
            key_charts.append(str(img_dir / fn))
    macro_summary = {k: round(float(v["Close"].iloc[-1]), 2) for k, v in macro.items()}

    # 4) 히트맵 (시총 상위 — 시간 절약 위해 cap 작게)
    try:
        from src.us_screener import data_source as us_ds
        caps = us_ds.fetch_market_caps()
        sectors = us_ds.fetch_sectors()
        # 상위 50종목 당일 등락
        top = sorted(caps, key=lambda s: -caps[s])[:50]
        changes = {}
        for s in top:
            df = fetch_prices.fetch_ohlcv(s, days=10)
            if df is not None and len(df) >= 2:
                changes[s] = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-2]) - 1) * 100
        if changes:
            fn = heatmap_chart.sp500_heatmap(caps, changes, sectors, img_dir)
            if fn:
                chart_list.append({"filename": fn, "title": "S&P500 히트맵", "caption_hint": "시총 가중 히트맵 — 시장 폭 판단"})
                key_charts.append(str(img_dir / fn))
    except Exception:
        log.exception("[report] 히트맵 실패 — 생략")

    # 5) 한국 수급 멀티패널
    kr_sizes = fetch_korea_flows.fetch_size_index_ohlcv(days=300)
    kr_flows = fetch_korea_flows.fetch_investor_flows(days=120)
    korea_summary = {}
    for i, (label, price_df) in enumerate(kr_sizes.items(), 1):
        # 매핑: 대형/중형/소형 → KOSPI flows, KOSDAQ → KOSDAQ flows
        flows_df = kr_flows.get("KOSDAQ") if "KOSDAQ" in label else kr_flows.get("KOSPI")
        fn = korea_flow_chart.flow_multipanel(price_df, flows_df, label, img_dir, f"30_kr_{i:02d}_{label}.png")
        if fn:
            chart_list.append({"filename": fn, "title": f"한국 {label} 수급", "caption_hint": f"{label} 가격·이격·기관·외국인·개인 누적순매수"})
            if "대형" in label or "KOSDAQ" in label:
                key_charts.append(str(img_dir / fn))
    if kr_flows:
        for mkt, fdf in kr_flows.items():
            korea_summary[mkt] = {inv: round(float(fdf[inv].iloc[-20:].sum()), 0)
                                  for inv in fdf.columns}

    if not chart_list:
        log.error("[report] 차트 0개 — 데이터 전부 미확보")
        return None, None, [], ""

    # 6) LLM 작성
    log.info("[report] 차트 %d개, 신호 %d개 → LLM 작성", len(chart_list), len(signals))
    md = report_writer.write_report(date_iso, rotation, chart_list, signals, macro_summary, korea_summary)

    # headline 추출 (첫 # 라인)
    headline = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("#")), f"{date_iso} 시장 리포트")

    md_path = base / "report.md"
    md_path.write_text(md, encoding="utf-8")
    log.info("[report] markdown 저장: %s", md_path)

    # 7) PDF (pdf_export 재사용)
    pdf_path = None
    try:
        from src import pdf_export
        # 차트 이미지 절대경로로 치환 (PDF 렌더용)
        md_for_pdf = md.replace("](images/", f"]({img_dir.resolve()}/")
        out_pdf = base / "report.pdf"
        result = asyncio.run(pdf_export.markdown_to_pdf(md_for_pdf, out_pdf, headline))
        if result:
            pdf_path = str(out_pdf)
            log.info("[report] PDF 생성: %s", pdf_path)
    except Exception:
        log.exception("[report] PDF 생성 실패 — markdown만")

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
