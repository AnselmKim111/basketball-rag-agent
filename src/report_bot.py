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

WELCOME_TEXT = (
    "✅ *가입 완료!*\n\n"
    "📊 *버터대디봇* — 매일 아침 시장 색깔 진단 PDF\n\n"
    "내일부터 매일 *08:00 KST* (미국 마감 + 한국 개장 전)에 PDF가 자동으로 도착합니다.\n\n"
    "내용: 미국 4대 지수·히트맵·매크로·ETF + 한국 수급 멀티패널 + LLM 시황 내러티브\n\n"
    "*명령*\n"
    "  /report — 지금 즉시 리포트 생성 (~3-7분)\n"
    "  /stop — 자동 발송 탈퇴\n"
    "  /help — 도움말\n"
)

WELCOME_BACK_TEXT = (
    "👋 *이미 가입되어 있어요* — 매일 08:00 KST에 자동 발송됩니다.\n\n"
    "*명령*\n"
    "  /report — 지금 즉시 리포트 (~3-7분)\n"
    "  /stop — 자동 발송 탈퇴\n"
    "  /help — 도움말\n"
)

HELP_TEXT = (
    "📊 *버터대디봇* — 차트 기반 시장 색깔 진단 리포트\n\n"
    "매일 08:00 KST 자동 발송 (미국 직전 거래일 마감 + 한국 당일).\n"
    "내용: 미국 4대 지수·히트맵·매크로·ETF + 한국 수급 멀티패널 + LLM 시황 내러티브.\n\n"
    "명령:\n"
    "  /start — 가입 (자동 발송 활성화)\n"
    "  /stop — 탈퇴\n"
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


def _is_admin(update: Update) -> bool:
    """ALLOWED_ENV / CHAT_ID_ENV에 등록된 numeric chat_id만 admin."""
    cid = str(update.effective_chat.id) if update.effective_chat else ""
    return cid in set(_parse_chat_ids(ALLOWED_ENV, CHAT_ID_ENV))


async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        log.exception("help 실패")


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — 누구나 가입. 차단된 chat_id는 거부. admin에게 신규 가입 알림."""
    from src.report import subscribers as subs

    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    username = user.username if user else None
    full_name = user.full_name if user else None
    loop = asyncio.get_running_loop()
    try:
        is_new = await loop.run_in_executor(
            None, lambda: subs.subscribe(chat_id, username, full_name)
        )
    except Exception:
        log.exception("[start] subscribe 실패")
        try:
            await update.message.reply_text("⚠️ 가입 실패 — 관리자에게 문의해 주세요.")
        except Exception:
            pass
        return

    try:
        text = WELCOME_TEXT if is_new else WELCOME_BACK_TEXT
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        log.exception("[start] welcome 발송 실패")

    if is_new:
        try:
            admin_ids = _parse_chat_ids(ALLOWED_ENV, CHAT_ID_ENV)
            for aid in admin_ids:
                if aid == chat_id:
                    continue
                try:
                    await context.bot.send_message(
                        chat_id=aid,
                        text=(
                            f"🆕 버터대디봇 신규 가입자\n"
                            f"  chat_id: {chat_id}\n"
                            f"  username: @{username or '(없음)'}\n"
                            f"  name: {full_name or '(없음)'}\n\n"
                            f"차단하려면: /block {chat_id}"
                        ),
                    )
                except Exception:
                    log.exception("admin 알림 실패 aid=%s", aid)
        except Exception:
            log.exception("[start] admin 알림 절차 실패")


async def _cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop — 자가 탈퇴."""
    from src.report import subscribers as subs

    chat_id = str(update.effective_chat.id)
    loop = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(None, lambda: subs.unsubscribe(chat_id))
    except Exception:
        log.exception("[stop] unsubscribe 실패")
        ok = False
    try:
        await update.message.reply_text(
            "👋 탈퇴 완료. 자동 발송이 중단됩니다." if ok
            else "ℹ️ 가입되어 있지 않습니다."
        )
    except Exception:
        pass


async def _cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — admin 전용. 가입자 목록."""
    from src.report import subscribers as subs

    if not _is_admin(update):
        await update.message.reply_text("🔒 관리자 전용 명령입니다.")
        return
    loop = asyncio.get_running_loop()
    try:
        items = await loop.run_in_executor(None, subs.list_all)
    except Exception:
        log.exception("[list] 실패")
        items = []
    if not items:
        await update.message.reply_text("📭 가입자 없음")
        return
    lines = [f"👥 가입자 {len(items)}명:"]
    for it in items:
        flag = "🚫" if it["is_blocked"] else "✓"
        lines.append(
            f"{flag} {it['chat_id']} @{it['username'] or '?'} ({it['full_name'] or '?'}) "
            f"— {it['subscribed_at'][:10]}"
        )
    await send_text_chunked(context.bot, str(update.effective_chat.id), "\n".join(lines))


async def _cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from src.report import subscribers as subs

    if not _is_admin(update):
        await update.message.reply_text("🔒 관리자 전용 명령입니다.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용: /block <chat_id>")
        return
    target = args[0].strip()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: subs.block(target))
    await update.message.reply_text(f"🚫 차단 완료: {target}")


async def _cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from src.report import subscribers as subs

    if not _is_admin(update):
        await update.message.reply_text("🔒 관리자 전용 명령입니다.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용: /unblock <chat_id>")
        return
    target = args[0].strip()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: subs.unblock(target))
    await update.message.reply_text(f"✓ 차단 해제: {target}")


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
        # admin (env) + 가입자 (DB) union, 중복 제거
        admin_ids = _parse_chat_ids(CHAT_ID_ENV, ALLOWED_ENV)
        try:
            from src.report import subscribers as _subs
            sub_ids = _subs.list_active_chat_ids()
        except Exception:
            log.exception("[report] subscribers 조회 실패")
            sub_ids = []
        targets = list(dict.fromkeys(admin_ids + sub_ids))
    if not targets:
        log.error("[report] 발송 대상 없음")
        return

    loop = asyncio.get_running_loop()
    try:
        # portfolio는 첫 target chat_id 사용 (단일 chat 환경 가정).
        portfolio_chat_id = targets[0] if targets else None
        md_path, pdf_path, key_charts, headline = await loop.run_in_executor(
            None, _build_report, portfolio_chat_id)
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


def _build_portfolio_section(portfolio_chat_id, theme_summary, img_dir, date_iso, add) -> dict:
    """§B 사용자 보유 종목 섹션 — chart 2개 추가 + payload 반환.

    portfolio_chat_id 없거나 portfolio 미등록이면 빈 dict 반환 (§B 자연 생략).
    enrichment 예외 시 log + 빈 dict — PDF 빌드 영향 없음.
    """
    if not portfolio_chat_id:
        return {}
    try:
        from src import portfolio_store
        from src.report.data import portfolio_enrich
        from src.report.charts import portfolio_dashboard
        positions = portfolio_store.load_positions(portfolio_chat_id)
        if not positions:
            return {}
        theme_hot = (theme_summary or {}).get("hot", []) or []
        payload = portfolio_enrich.enrich_positions(positions, theme_hot=theme_hot)
        add(portfolio_dashboard.portfolio_summary_card(
                payload.get("summary") or {}, img_dir, date_iso=date_iso),
            "내 보유 종목 현황", "종목 수·매수금·평가금·PnL",
            "B. 내 보유 종목", key=True)
        add(portfolio_dashboard.portfolio_positions_grid(
                payload.get("positions") or [], img_dir, date_iso=date_iso),
            "내 보유 종목 grid", "종목별 매수가·현재가·PnL·thesis 분류",
            "B. 내 보유 종목", key=True)
        log.info("[report.portfolio] chat=%s 종목=%d PnL=%s%%",
                 portfolio_chat_id, payload["summary"]["total_positions"],
                 payload["summary"].get("total_pnl_pct"))
        return payload
    except Exception:
        log.exception("[report.portfolio] enrichment 실패 — §B 생략")
        return {}


def _build_report(portfolio_chat_id: str | None = None):
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
    # 기존 하드코딩 9종 — 차트용으로만 잠시 유지 (watchlist 통합 후 deprecate).
    # 향후 watchlist us 상위 종목 OHLCV로 자동 대체될 예정.
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
    # screener.db 활성화 진단 — sector_leader/trend_reversal stream 의존
    try:
        from src.report.data import screener_adapter
        us_active = len(screener_adapter.load_active_tickers("US"))
        kr_active = len(screener_adapter.load_active_tickers("KR"))
        us_latest = screener_adapter.latest_signal_date("US")
        kr_latest = screener_adapter.latest_signal_date("KR")
        log.info("[report] screener.db 상태 — US active=%d (latest=%s) · KR active=%d (latest=%s)",
                 us_active, us_latest, kr_active, kr_latest)
    except Exception:
        log.exception("[report] screener.db 진단 실패")

    risk_gauge: dict = {"score": 50, "label": "중립", "signals": {}}
    # 어제 게이지 점수 미리 fetch (snapshot 있으면) — 게이지 시각화 delta 표시용
    prev_snapshot_early = state.load_previous(date_iso) or {}
    prev_gauge_score = prev_snapshot_early.get("gauge_score") if prev_snapshot_early else None
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
        # 6대 자산군 1D·5D 평균 — Risk 게이지 기여 구조
        add(flow_charts.risk_component_breakdown(risk_rows, img_dir, date_iso=date_iso),
            "6대 자산군 1D·5D", "Risk 게이지 기여 구조 — 자산군별 평균 등락",
            "0. 글로벌 위험선호")
        # 게이지 반원 시각화 + 어제 대비 delta
        add(flow_charts.risk_gauge_visual(risk_gauge, prev_gauge_score, img_dir, date_iso=date_iso),
            "위험선호 게이지",
            f"{risk_gauge.get('label')} {risk_gauge.get('score')}/100"
            + (f" (전일 {int(prev_gauge_score)}, Δ{risk_gauge.get('score') - int(prev_gauge_score):+d})"
               if isinstance(prev_gauge_score, (int, float)) else " (전일 기준선)"),
            "0. 글로벌 위험선호")
        # 게이지 시계열 history (Risk + FX 14일)
        gauge_hist = state.load_history(date_iso, days=14)
        if gauge_hist:
            add(flow_charts.gauge_history_chart(gauge_hist, risk_gauge, {}, img_dir, date_iso=date_iso),
                "게이지 시계열", f"Risk·FX 압력 최근 {len(gauge_hist)+1}일 추세",
                "0. 글로벌 위험선호")
    except Exception:
        log.exception("[report] 글로벌 위험선호 매트릭스 실패 — 생략")

    # ---------- Follow-up 추적 표 (어제 watchlist 종목 어제 5D vs 오늘 5D) ----------
    if prev_snapshot_early.get("highlights_meta"):
        try:
            add(flow_charts.followup_tracking_table(
                    prev_snapshot_early["highlights_meta"], img_dir, date_iso=date_iso),
                "어제 watchlist 추적", "어제 5D vs 오늘 5D · 강화·약화·유지 분류",
                "Follow-up", key=True)
        except Exception:
            log.exception("[report] Follow-up 추적 표 실패 — 생략")

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
    # 한국 섹터 ETF 상대강도 (반도체·방산·2차전지·신재생·자동차·헬스케어·벤치마크)
    kr_perf: list = []
    try:
        from src.report.data import fetch_kr_sectors
        kr_perf = fetch_kr_sectors.fetch_kr_sector_strength()
        if kr_perf:
            add(rotation_charts.sector_return_bars(
                    kr_perf, img_dir, filename="07b_kr_sector_bars.png",
                    title="한국 섹터 ETF 상대강도 (1M 정렬)", date_iso=date_iso,
                    is_korea=True),
                "한국 섹터 상대강도", "KR ETF 1M/3M — 양수(적)·음수(청) 한국식",
                "3. 섹터 로테이션 맵")
    except Exception:
        log.exception("[report] KR 섹터 강도 실패 — 생략")

    # 미국 ↔ 한국 sector 페어 5D 차이 (디커플링·동조)
    if kr_perf:
        try:
            add(rotation_charts.us_kr_sector_pairs(theme_rows, kr_perf, img_dir, date_iso=date_iso),
                "미국 ↔ 한국 sector 페어", "5D 차이 — 디커플링·동조 분류",
                "3. 섹터 로테이션 맵", key=True)
        except Exception:
            log.exception("[report] 미국·한국 페어 차트 실패 — 생략")
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

    # 환전 압력 게이지 (USD/KRW + EWY + 외국인 KOSPI 20D 통합 0~100)
    fx_gauge: dict = {}
    if fx.get("USD/KRW") is not None:
        foreign_kospi_20d = None
        try:
            if kr_flows and "KOSPI" in kr_flows:
                kdf = kr_flows["KOSPI"]
                if "외국인" in kdf.columns:
                    foreign_kospi_20d = float(kdf["외국인"].iloc[-20:].sum())
        except Exception:
            pass
        prev_fx_pressure = prev_snapshot_early.get("fx_pressure_score") if prev_snapshot_early else None
        _fx_path, fx_gauge = flow_charts.fx_pressure_gauge(
            fx.get("USD/KRW"), ewy, foreign_kospi_20d, img_dir,
            prev_pressure=prev_fx_pressure, date_iso=date_iso)
        if _fx_path:
            cap = f"{fx_gauge.get('label')} {fx_gauge.get('score')}/100"
            if isinstance(prev_fx_pressure, (int, float)):
                diff = fx_gauge.get("score", 50) - int(prev_fx_pressure)
                cap += f" (전일 {int(prev_fx_pressure)}, Δ{diff:+d})"
            else:
                cap += " (전일 기준선)"
            add(_fx_path, "환전 압력 게이지", cap, "4. IPO·환전 임팩트", key=True)

    # ---------- §6 개별 종목 하이라이트 — 통합 watchlist (5 stream) ----------
    from src.report.data import watchlist as _wl
    prev_snapshot_for_wl = state.load_previous(date_iso)
    try:
        watchlist_result = _wl.build_watchlist(
            date_iso=date_iso, theme_rows=theme_rows,
            earnings=earnings, news=news, prev_snapshot=prev_snapshot_for_wl,
        )
    except Exception:
        log.exception("[report] watchlist 빌드 실패 — 기존 highlights_df 폴백")
        watchlist_result = {"us": [], "kr": []}
    n_us = len(watchlist_result.get("us") or [])
    n_kr = len(watchlist_result.get("kr") or [])
    log.info("[report.watchlist] US=%d KR=%d", n_us, n_kr)

    # ---------- §5 어닝 캘린더 (upcoming 시각화) ----------
    if earnings and isinstance(earnings.get("upcoming"), list) and earnings["upcoming"]:
        # hot 테마 매칭 종목 = upcoming에서 us_theme_linkage 핵심 종목 매칭
        hot_tkr_set: set[str] = set()
        try:
            from src.report.data import us_theme_linkage
            for lbl in (theme_summary or {}).get("hot", []) or []:
                hot_tkr_set.update(us_theme_linkage.core_tickers_for_label(lbl))
        except Exception:
            pass
        add(flow_charts.earnings_calendar_grid(
                earnings["upcoming"], img_dir, date_iso=date_iso,
                hot_tickers=hot_tkr_set),
            "다가올 어닝 캘린더",
            "시총 큰 순 · 색상=분석가 buy 변화 · 노란 outline=hot 테마 종목",
            "5. 어닝 모멘텀", key=True)

    # 차트는 highlights_df (기존) + watchlist enriched 메타 합쳐 카테고리 배지로 표시.
    # watchlist 있으면 US/KR 분리 2장, 없으면 단일 폴백 차트.
    _hl_result = stock_highlights.highlight_grid(
        highlights_df, img_dir, date_iso=date_iso,
        watchlist_us=watchlist_result.get("us") or [],
        watchlist_kr=watchlist_result.get("kr") or [])
    if isinstance(_hl_result, list):
        for _p in _hl_result:
            _basename = _p.split("/")[-1] if isinstance(_p, str) else _p
            label_kr = "🇰🇷 한국" if "_kr" in (_basename or "") else "🇺🇸 미국"
            add(_basename, f"{label_kr} 개별 종목 하이라이트",
                "watchlist 카테고리별 (📈 어닝 / 💎 IPO / 🚀 리더 / 🔄 반전 / 🔁 F/U)",
                "6. 개별 종목")
    elif _hl_result:
        add(_hl_result, "개별 종목 하이라이트", "스토리 종목 (폴백)", "6. 개별 종목")

    # ---------- §B 사용자 보유 종목 (portfolio_chat_id 있을 때만) ----------
    portfolio_payload = _build_portfolio_section(
        portfolio_chat_id, theme_summary, img_dir, date_iso, add)

    # watchlist thesis quadrant — 14종목 강화/약화/디커플링/유지 자동 분류
    add(flow_charts.watchlist_thesis_quadrant(watchlist_result, img_dir, date_iso=date_iso),
        "watchlist thesis quadrant", "14종목 자동 분류 — thesis 강도 분포",
        "6. 개별 종목", key=True)

    # IPO mini-card (OHLCV 없는 신생주 — Yahoo 404 폴백 시각화)
    _ipo_card_path = stock_highlights.ipo_cards(
        watchlist_result.get("us") or [], img_dir, date_iso=date_iso)
    if _ipo_card_path:
        _basename = _ipo_card_path.split("/")[-1] if isinstance(_ipo_card_path, str) else _ipo_card_path
        add(_basename, "다가올 IPO 카드",
            "offer price · 시총 · D-N · status — OHLCV 없는 신생주 폴백",
            "6. 개별 종목")

    # 다음날 F/U용 메타 (state snapshot에 저장)
    highlights_snapshot_meta = _wl.snapshot_tickers(watchlist_result)

    # ---------- §7 한국시장 자금흐름 ----------
    korea_summary: dict = {}
    # streak alert (≥5거래일 연속 매도/매수) — §7 헤더 시각 강조
    add(korea_flow_chart.streak_alert_card(kr_flows, img_dir, date_iso=date_iso),
        "한국 수급 streak alert", "5거래일 이상 연속 매도/매수 — thesis 격상 임계",
        "7. 한국시장 자금흐름", key=True)
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

    # 외국인 연속 매도/매수 streak 계산 (KOSPI·KOSDAQ) — thesis 격상 alert용.
    # state.streak_count 공용 헬퍼 (korea_flow_chart도 동일 함수 사용).
    for mkt, fdf in (kr_flows or {}).items():
        for inv in ("외국인", "기관", "개인"):
            if inv in fdf.columns:
                korea_summary.setdefault(mkt, {})[f"{inv}_streak"] = state.streak_count(fdf[inv])

    # ---------- §8 종합 자금흐름 다이어그램 (Sankey, Risk 게이지 허브 통합) ----------
    src_ep, dst_ep = theme_momentum.flow_endpoints(theme_rows)
    add(flow_charts.capital_flow_diagram(src_ep, dst_ep, img_dir, date_iso=date_iso,
                                          gauge_score=risk_gauge.get("score"),
                                          gauge_label=risk_gauge.get("label"),
                                          fx_score=fx_gauge.get("score") if fx_gauge else None,
                                          fx_label=fx_gauge.get("label") if fx_gauge else None),
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
                                    macro_summary, breadth, korea_summary, rsp_new_high,
                                    highlights_meta=highlights_snapshot_meta,
                                    risk_gauge=risk_gauge,
                                    fx_pressure=fx_gauge)
    prev = prev_snapshot_early or state.load_previous(date_iso)
    deltas = state.compute_deltas(snapshot, prev)
    state.save_snapshot(date_iso, snapshot)
    log.info("[report] deltas 계산 (baseline=%s, notes=%d)", deltas.get("baseline"), len(deltas.get("notes", [])))

    # ---------- LLM 작성 (8섹션 + 팔로업) ----------
    log.info("[report] 차트 %d개, 신호 %d개 → LLM 작성", len(chart_list), len(signals))
    md = report_writer.write_report(date_iso, rotation, chart_list, signals, macro_summary,
                                    korea_summary, news=news, theme_momentum=theme_summary,
                                    deltas=deltas, breadth=breadth,
                                    highlights=watchlist_result,
                                    earnings=earnings, stale=stale,
                                    risk_gauge=risk_gauge, fx_gauge=fx_gauge,
                                    prev_snapshot=prev,
                                    portfolio=portfolio_payload)

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


async def _cmd_portfolio_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/portfolio_add TICKER MARKET 매수가 수량 — 보유 종목 등록."""
    from src import portfolio_store
    chat_id = str(update.effective_chat.id)
    args = context.args if context else []
    if len(args) < 4:
        await update.message.reply_text(
            "사용법: /portfolio_add TICKER MARKET 매수가 수량\n"
            "예: /portfolio_add NVDA US 110.50 50\n"
            "예: /portfolio_add 005930 KR 89000 100"
        )
        return
    ticker, market, buy_price, shares = args[0], args[1], args[2], args[3]
    pos = portfolio_store.add_position(chat_id, ticker, market, buy_price, shares)
    if not pos:
        await update.message.reply_text(
            f"❌ 등록 실패 — 형식 확인 (MARKET=US/KR, 가격·수량 양수)"
        )
        return
    unit = "원" if pos["market"] == "KR" else "$"
    await update.message.reply_text(
        f"✅ {pos['ticker']} ({pos['market']}) 등록\n"
        f"매수가 {pos['buy_price']:,.2f}{unit} · {pos['shares']:,.0f}주\n"
        f"매일 08:00 KST 리포트에 §B 보유 종목 섹션으로 자동 포함."
    )


async def _cmd_portfolio_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/portfolio_remove TICKER — 종목 삭제."""
    from src import portfolio_store
    chat_id = str(update.effective_chat.id)
    args = context.args if context else []
    if not args:
        await update.message.reply_text("사용법: /portfolio_remove TICKER")
        return
    ok = portfolio_store.remove_position(chat_id, args[0])
    if ok:
        await update.message.reply_text(f"✅ {args[0].upper()} 삭제")
    else:
        await update.message.reply_text(f"❌ {args[0].upper()} 미등록 종목")


async def _cmd_portfolio_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/portfolio_list — 현재 보유 종목 list."""
    from src import portfolio_store
    chat_id = str(update.effective_chat.id)
    positions = portfolio_store.load_positions(chat_id)
    if not positions:
        await update.message.reply_text(
            "📭 등록된 종목 없음\n"
            "사용법: /portfolio_add TICKER MARKET 매수가 수량"
        )
        return
    lines = [f"📊 보유 종목 {len(positions)}개:"]
    for pos in positions:
        unit = "원" if pos.get("market") == "KR" else "$"
        bp = pos.get("buy_price", 0); sh = pos.get("shares", 0)
        lines.append(f"• {pos.get('ticker')} ({pos.get('market')}) — "
                     f"{bp:,.2f}{unit} × {sh:,.0f}주 (등록 {pos.get('added_at', '?')})")
    await update.message.reply_text("\n".join(lines))


async def _cmd_portfolio_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/portfolio_clear — 전체 삭제."""
    from src import portfolio_store
    chat_id = str(update.effective_chat.id)
    ok = portfolio_store.clear_positions(chat_id)
    if ok:
        await update.message.reply_text("✅ 전체 삭제")
    else:
        await update.message.reply_text("📭 등록 종목 없음")


REPORT_COMMANDS = [
    ("start", "✅ 가입 (매일 08:00 KST 자동 발송)"),
    ("report", "📊 즉시 시황 리포트 생성"),
    ("portfolio_add", "💼 보유 종목 등록"),
    ("portfolio_list", "📋 보유 종목 list"),
    ("portfolio_remove", "🗑 보유 종목 삭제"),
    ("model_eval", "🤖 모델 가성비 즉시 재평가"),
    ("model_approve", "✅ 모델 추천 승인 (Railway env 적용)"),
    ("model_reject", "❎ 모델 추천 거부"),
    ("model_status", "📡 현재 모델 + 대기 추천 보기"),
    ("stop", "🔕 자동 발송 탈퇴"),
    ("help", "도움말"),
]


def build_report_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("stop", _cmd_stop))
    app.add_handler(CommandHandler("report", _cmd_report))
    app.add_handler(CommandHandler("list", _cmd_list))
    app.add_handler(CommandHandler("block", _cmd_block))
    app.add_handler(CommandHandler("unblock", _cmd_unblock))
    app.add_handler(CommandHandler("portfolio_add", _cmd_portfolio_add))
    app.add_handler(CommandHandler("portfolio_remove", _cmd_portfolio_remove))
    app.add_handler(CommandHandler("portfolio_list", _cmd_portfolio_list))
    app.add_handler(CommandHandler("portfolio_clear", _cmd_portfolio_clear))
    from src.model_router.handler import (
        model_eval_cmd, model_approve_cmd, model_reject_cmd, model_status_cmd,
    )
    app.add_handler(CommandHandler("model_eval", model_eval_cmd))
    app.add_handler(CommandHandler("model_approve", model_approve_cmd))
    app.add_handler(CommandHandler("model_reject", model_reject_cmd))
    app.add_handler(CommandHandler("model_status", model_status_cmd))
    if os.getenv("REPORT_TEST_MODE", "0") == "1":
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_self_test(app.bot))
        except RuntimeError:
            pass
    return app
