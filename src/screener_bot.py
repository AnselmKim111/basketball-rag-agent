"""ScreenerBot — 한국 주식 기술적 신호 스크리너.

매일 16:00 KST에 자동 실행 (15:30 장마감 + 30분 정산 버퍼). 사용자 명령:
  /start (가입), /stop (탈퇴), /help, /screen (즉시), /status, /backfill
  /list (admin), /block <chat_id> (admin), /unblock <chat_id> (admin)

격리 원칙: wisereport/PIPELINE_LOCK 미사용. 자체 SQLite DB(/data/screener.db).
가입자 (subscribers) DB로 친구 공유 가능 — 봇 link만 주면 자동 가입 + 자동 발송.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from src.bot_helpers import (
    deny_message,
    is_authorized,
    send_text_chunked,
)
from src.screener import subscribers as subs

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ALLOWED_ENV = "SCREENER_ALLOWED_CHAT_IDS"  # admin chat_ids (콤마 구분)
CHAT_ID_ENV = "SCREENER_CHAT_ID"  # legacy: 단일 chat_id (admin 호환용)


def _is_admin(update: Update) -> bool:
    """관리자 권한 (env vars의 ALLOWED_CHAT_IDS / CHAT_ID에 등록된 chat_id)."""
    return is_authorized(update, ALLOWED_ENV)


def _is_subscribed_or_admin(update: Update) -> bool:
    """관리자 또는 활성 가입자."""
    if _is_admin(update):
        return True
    cid = str(update.effective_chat.id)
    try:
        return subs.is_subscribed(cid)
    except Exception:
        log.exception("subscriber check 실패")
        return False


WELCOME_TEXT = (
    "👋 <b>ScreenerBot에 오신 걸 환영합니다!</b>\n\n"
    "📈 한국 주식 기술적 신호를 매일 16:00 KST에 자동 발송합니다.\n"
    "(15:30 장마감 + 30분 정산 버퍼)\n\n"
    "신호:\n"
    "  🚀 역사적 신고가  📈 52주 신고가\n"
    "  🔥 거래량 돌파 ≥2배  🎯 52주 돌파 직전\n"
    "  💎 VCP 돌파 (변동성 수축 후 돌파)\n\n"
    "명령:\n"
    "  /screen — 지금 즉시 신호 분석 (~2-3분)\n"
    "  /help — 도움말\n"
    "  /stop — 자동 발송 해제\n\n"
    "<i>시총 3000억+ KOSPI/KOSDAQ 종목, 섹터별 분류, 시총·상승률 복합 정렬, "
    "Naver 이중확인 통과만 발송</i>"
)

HELP_TEXT = (
    "📈 <b>ScreenerBot</b> — 한국 주식 기술적 신호\n\n"
    "자동: 매일 16:00 KST (15:30 종가 기준 · 30분 정산 버퍼)\n"
    "데이터: KRX (Naver Finance 1순위 + pykrx/FDR 폴백)\n"
    "유니버스: KOSPI + KOSDAQ 보통주, 시총 ≥ 3000억\n"
    "이중확인: base_date-anchored signals + Naver 재 fetch cross-validation\n\n"
    "신호:\n"
    "  🚀 역사적 신고가 — 종가 &gt; 보유 데이터(280일) 최고가\n"
    "  📈 52주 신고가 — 종가 &gt; 과거 252영업일 최고가\n"
    "  🔥 거래량 돌파 — 오늘 거래량 ≥ 20일 평균 ×2.0 + 종가 상승\n"
    "  🎯 52주 돌파 직전 — 종가 = 52주고점 95-99% + 5일 거래량 ≥ ×1.3\n"
    "  💎 VCP 돌파 — 50일 박스권 + ATR 30%+ 수축 + 거래량 dry-up + 돌파\n\n"
    "명령:\n"
    "  /start — 가입 (자동 발송 활성화)\n"
    "  /stop — 탈퇴\n"
    "  /screen — 즉시 실행\n"
    "  /help — 이 메시지\n"
)


# ------------------------------------------------------------------
# 가입/탈퇴 핸들러 (누구나 가능)
# ------------------------------------------------------------------
async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — 누구나 가입. 차단된 chat_id는 거부."""
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
        await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("[start] welcome 발송 실패")

    # admin에게 신규 가입 알림
    if is_new:
        try:
            admin_ids = _parse_chat_ids(ALLOWED_ENV, CHAT_ID_ENV)
            for aid in admin_ids:
                if aid == chat_id:
                    continue  # admin 본인 알림 skip
                try:
                    await context.bot.send_message(
                        chat_id=aid,
                        text=(
                            f"🆕 ScreenerBot 신규 가입자\n"
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
    """/list — admin 전용. 가입자 목록 조회."""
    if not _is_admin(update):
        await deny_message(update, "스크리너봇")
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
    """/block <chat_id> — admin 전용."""
    if not _is_admin(update):
        await deny_message(update, "스크리너봇")
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
    if not _is_admin(update):
        await deny_message(update, "스크리너봇")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용: /unblock <chat_id>")
        return
    target = args[0].strip()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: subs.unblock(target))
    await update.message.reply_text(f"✓ 차단 해제: {target}")


# ------------------------------------------------------------------
# 기존 핸들러 (가입자/admin 모두 사용)
# ------------------------------------------------------------------
async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /help는 누구나 — 봇 안내용
    try:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("help reply 실패")


async def _cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_subscribed_or_admin(update):
        await update.message.reply_text(
            "🔒 가입자만 사용 가능합니다. /start 입력으로 가입하세요."
        )
        return
    try:
        await update.message.reply_text("🔄 스크리닝 실행 중 (기존 DB 기준, ~3분 소요)")
    except Exception:
        log.exception("screen 안내 실패")
    try:
        await screener_daily_job(context.bot, override_chat_id=str(update.effective_chat.id))
    except Exception:
        log.exception("[screen] 실행 실패")


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "스크리너봇")
        return
    loop = asyncio.get_running_loop()
    try:
        from src.screener import db as screener_db
        info = await loop.run_in_executor(None, screener_db.status)
        await send_text_chunked(context.bot, str(update.effective_chat.id), info)
    except Exception:
        log.exception("[status] 실패")
        try:
            await update.message.reply_text("⚠️ status 실패 — 로그 확인")
        except Exception:
            pass


def _diag_report(query: str) -> str:
    """한 종목을 파이프라인 게이트별로 추적 (blocking — executor에서 호출)."""
    import re
    from src.screener import db, signals

    q = query.strip()
    if re.match(r"^\d{6}$", q):
        ticker = q
    else:
        cands = db.search_tickers_by_name(q)
        if not cands:
            return f"❓ '{q}' 매칭 종목 없음 (유니버스 미포함 가능성 — universe refresh 필요)"
        if len(cands) > 1:
            lines = [f"🔎 '{q}' 후보 여러 개 — 6자리 코드로 다시:"]
            lines += [f"  {t} {n}" for t, n in cands]
            return "\n".join(lines)
        ticker = cands[0][0]

    row = db.get_ticker_row(ticker)
    base_date = db.latest_date()
    min_cap = signals._get_float_env("SCREENER_MIN_MARKET_CAP", signals.DEFAULT_MIN_MARKET_CAP)
    out = [f"🩺 진단: {ticker} ({(row or {}).get('name') or '?'})", f"기준일(base_date): {base_date}"]

    # 1) 유니버스
    if not row:
        out.append("① 유니버스: ❌ 없음 — tickers 테이블 미존재. universe refresh 또는 비상장/신규.")
        return "\n".join(out)
    out.append(f"① 유니버스: ✅ 존재 / is_active={row['is_active']}"
               + ("" if row["is_active"] else "  ⚠️ 비활성 → 신호 계산 제외"))

    # 2) 시총
    cap = row.get("market_cap")
    if cap is None:
        out.append(f"② 시총: NULL — 필터 우회(통과). fetch 실패 의심.")
    else:
        ok = cap >= min_cap
        out.append(f"② 시총: {cap/1e8:,.0f}억 / 필터 {min_cap/1e8:,.0f}억 → "
                   + ("✅ 통과" if ok else "❌ 탈락(skipped_cap)"))

    # 3) 데이터
    rows = db.load_ohlcv(ticker, days=1300)
    n = len(rows)
    latest = rows[-1]["date"] if rows else "없음"
    has_base = any(r["date"] == base_date for r in rows)
    out.append(f"③ 데이터: {n}행 / 최신일 {latest} / base_date row "
               + ("✅ 보유" if has_base else "❌ 누락(skipped_no_base)"))
    if n < 60:
        out.append("   ⚠️ <60행 → silent skip (skipped_short)")

    # 4) 신호 + 종가신고가 진단
    if has_base and n >= 60:
        try:
            sigs = signals.compute_signals_for_ticker(rows, base_date=base_date)
            fired = ", ".join(sigs.keys()) if sigs else "없음"
            out.append(f"④ 발화 신호: {fired}")
        except Exception as e:
            out.append(f"④ 신호 계산 실패: {e!r}")
        # 종가신고가 vs 장중고가 진단
        import pandas as pd
        df = pd.DataFrame(rows)
        bi = df.index[df["date"] == base_date].tolist()
        if bi:
            df = df.iloc[: bi[0] + 1]
            tc = int(df.iloc[-1]["close"])
            pch = int(df["close"].iloc[:-1].max()) if len(df) > 1 else 0
            phh = int(df["high"].iloc[:-1].max()) if len(df) > 1 else 0
            out.append(f"⑤ 종가신고가 진단: 오늘종가={tc:,} / 과거최고종가={pch:,} / 과거최고장중={phh:,}")
            if tc > pch and tc <= phh:
                out.append("   → 종가 신고가지만 과거 장중고가에 막힘 (종가기준 수정으로 해결됨)")
            elif tc > pch:
                out.append("   → 종가 신고가 + 장중고가도 돌파 (양 정의 모두 발화)")
            else:
                out.append("   → 종가 신고가 아님")
    return "\n".join(out)


async def _cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/diag <6자리코드|종목명> — 종목 누락 원인 파이프라인 추적 (admin)."""
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "스크리너봇")
        return
    chat_id = str(update.effective_chat.id)
    q = " ".join(context.args or []).strip()
    if not q:
        await send_text_chunked(context.bot, chat_id, "사용법: /diag 005930  또는  /diag 디앤디파마텍")
        return
    loop = asyncio.get_running_loop()
    try:
        report = await loop.run_in_executor(None, lambda: _diag_report(q))
    except Exception:
        log.exception("[diag] 실패")
        report = "⚠️ 진단 실패 — 로그 확인"
    await send_text_chunked(context.bot, chat_id, report)


async def _cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backtest [lookback] [horizon] — 신호 forward 수익률 백테스트 (admin)."""
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "스크리너봇")
        return
    chat_id = str(update.effective_chat.id)
    args = context.args or []
    lookback = 120
    horizons = (5, 10, 20)
    try:
        if len(args) >= 1:
            lookback = max(30, min(250, int(args[0])))
        if len(args) >= 2:
            horizons = (int(args[1]),)
    except ValueError:
        await send_text_chunked(context.bot, chat_id, "사용법: /backtest [lookback일수] [horizon일]")
        return
    await send_text_chunked(context.bot, chat_id,
                            f"📊 백테스트 실행 중 (최근 {lookback}거래일, ~1-3분)...")
    loop = asyncio.get_running_loop()
    try:
        from src.screener import backtest
        result = await loop.run_in_executor(
            None, lambda: backtest.backtest_signals(lookback=lookback, horizons=horizons))
        report = backtest.format_backtest(result)
    except Exception:
        log.exception("[backtest] 실패")
        report = "⚠️ 백테스트 실패 — 로그 확인"
    await send_text_chunked(context.bot, chat_id, report)


async def _cmd_breadth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/breadth — 시장 폭 지표 (admin). 신고가 부재가 시장 탓인지 판별."""
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "스크리너봇")
        return
    chat_id = str(update.effective_chat.id)
    loop = asyncio.get_running_loop()
    try:
        from src.screener import breadth
        b = await loop.run_in_executor(None, breadth.compute_breadth)
        report = breadth.format_breadth_full(b)
    except Exception:
        log.exception("[breadth] 실패")
        report = "⚠️ breadth 실패 — 로그 확인"
    await send_text_chunked(context.bot, chat_id, report)


async def _cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "스크리너봇")
        return
    chat_id = str(update.effective_chat.id)
    try:
        await update.message.reply_text("📥 1년치 백필 시작 (~10분 소요, 진행률 push)")
    except Exception:
        pass
    loop = asyncio.get_running_loop()

    def _progress(done: int, total: int, success: int) -> None:
        # blocking thread → 메인 loop로 안전하게 push
        try:
            asyncio.run_coroutine_threadsafe(
                send_text_chunked(
                    context.bot, chat_id,
                    f"📥 백필 진행 {done}/{total} (성공 {success})",
                ),
                loop,
            )
        except Exception:
            log.exception("progress push 실패")

    try:
        from src.screener import backfill
        result = await loop.run_in_executor(
            None, lambda: backfill.run_full_backfill(progress_cb=_progress)
        )
        await send_text_chunked(
            context.bot, chat_id,
            f"✅ 백필 완료: success={result['success']} fail={result['fail']} rows={result['rows']}",
        )
    except Exception:
        log.exception("[backfill] 실패")
        try:
            await send_text_chunked(context.bot, chat_id, "⚠️ 백필 실패 — 로그 확인")
        except Exception:
            pass


# ------------------------------------------------------------------
# 일일 스케줄 잡 (orchestrator가 호출)
# ------------------------------------------------------------------
from src.bot_helpers import parse_admin_chat_ids as _parse_chat_ids  # legacy alias


from src.screener_common import permalink as _permalink, render_caption, fmt_won as _fmt_won, fmt_pct as _fmt_pct


def _chart_caption(ticker: str, item: dict, cats: list[str], rows: list[dict], ytd, eps,
                   data_date: str | None = None) -> str:
    from src.screener import fundamentals
    turnover = fundamentals.turnover_won(rows[-1]) if rows else 0
    return render_caption(
        market_label="🇰🇷",
        ticker=ticker, item=item, cats=cats,
        turnover=turnover, ytd=ytd, eps=eps,
        market_cap=item.get("market_cap"),
        fmt_money=_fmt_won,
        news_url_template="https://finance.naver.com/item/news.naver?code={ticker}",
        header_use_name=True,
        show_ticker_in_name=True,
        eps_label="최근 EPS YoY",
        data_date=data_date,
    )


# 차트/메타 대상 = formatter.format_results 표시 섹션과 1:1 계약 (링크·YTD는 표시
# 종목에만 필요). 미표시 신호(추세전환 4종·volume_breakout)까지 게시하면 채널 flood +
# max_tickers cap 소모 — 실측 하루 ~94건 낭비. 섹션 추가 시 여기도 함께 갱신할 것.
DISPLAY_CATEGORIES = ("near_breakout_52w", "high_all", "high_52w", "vcp_breakout", "rs_leaders")


async def _post_charts_and_meta(results: dict, base_date: str,
                                max_tickers: int = 120) -> tuple[dict, dict]:
    """신호 종목별 (1) 채널 차트 게시 → permalink, (2) ytd·eps 메타 산출.

    채널 토큰/ID 미설정이면 게시는 건너뛰고 메타(ytd·eps)만 계산(4열 enrich).
    반환: (links: {ticker: url}, extra: {ticker: {ytd, eps_yoy}}).
    """
    from src.screener import chart, db, fundamentals

    by_ticker: dict[str, dict] = {}
    badges: dict[str, list] = {}
    for cat, items in results.items():
        if cat not in DISPLAY_CATEGORIES:
            continue
        for it in items:
            t = it.get("ticker")
            if not t:
                continue
            by_ticker.setdefault(t, it)
            badges.setdefault(t, []).append(cat)

    tickers = list(by_ticker.keys())[:max_tickers]
    # EPS YoY는 pykrx 일괄(2회) — 캐시 우선
    loop = asyncio.get_event_loop()
    eps_map = await loop.run_in_executor(
        None, lambda: fundamentals.eps_yoy_map(tickers, base_date))

    token = os.getenv("SCREENER_CHART_BOT_TOKEN", "").strip()
    channel = os.getenv("SCREENER_CHART_CHANNEL_ID", "").strip()
    chart_bot = None
    if token and channel:
        try:
            chart_bot = Bot(token=token)
        except Exception:
            log.exception("[screener] 차트봇 생성 실패 — 게시 생략")
    else:
        log.info("[screener] 차트 채널 미설정 — 링크 없이 메타만 계산")

    links: dict[str, str] = {}
    extra: dict[str, dict] = {}
    posted = 0
    for t in tickers:
        it = by_ticker[t]
        try:
            rows = await loop.run_in_executor(None, lambda t=t: db.load_ohlcv(t, days=260))
            ytd = fundamentals.ytd_pct(rows)
            eps = eps_map.get(t)
            extra[t] = {"ytd": ytd, "eps_yoy": eps}
            if chart_bot and rows:
                # freshness 가드 — cron 경로에선 절대 stale이 아니어야 정상 (뜨면 조기 경보)
                data_date = rows[-1]["date"]
                if data_date != base_date:
                    log.warning("[chart] %s 데이터 stale (%s != base %s)", t, data_date, base_date)
                    data_date += " ⚠️"
                title = f"{it.get('name', t)} ({t})"
                png = await loop.run_in_executor(
                    None, lambda t=t, rows=rows, title=title: chart.render_candle_volume(t, rows, title=title))
                if png:
                    from src.bot_helpers import send_channel_photo
                    cap = _chart_caption(t, it, badges[t], rows, ytd, eps, data_date=data_date)
                    msg = await send_channel_photo(chart_bot, channel, png, cap, ParseMode.HTML)
                    if msg:
                        url = _permalink(channel, msg.message_id)
                        if url:
                            links[t] = url
                        posted += 1
                    await asyncio.sleep(3.0)  # 채널 ~20건/분 한도 → 게시 간 간격 (성공/실패 무관 페이싱)
        except Exception:
            log.exception("[screener] 종목 처리 실패 %s", t)
    log.info("[screener] 채널 게시 %d건 · 메타 %d종목 (links=%d)", posted, len(extra), len(links))
    return links, extra


_screener_lock = asyncio.Lock()  # 동시 /screen·cron 중복 실행 방지 (atomic acquire)


async def screener_daily_job(bot: Bot, override_chat_id: str | None = None) -> None:
    """매일 16:00 cron 또는 /screen 즉시 실행.

    override_chat_id: 명시되면 그 chat_id 1명에게만 (사용자 /screen 명령용).
    None이면 모든 가입자 + admin에게 broadcast (cron용).

    신호 계산은 한 번만 수행 + 결과를 모든 대상자에게 발송 (효율).
    진행 상황(universe 빌드/백필) 메시지는 첫 대상자(주로 admin)에게만 발송.
    """
    log.info("[scheduled] screener_daily_job 시작 override=%s", override_chat_id)
    if _screener_lock.locked():
        log.info("[scheduled] 이미 실행 중 — 중복 호출 스킵")
        if override_chat_id:
            try:
                await send_text_chunked(bot, str(override_chat_id),
                                        "⏳ 이미 스크리닝 실행 중입니다 — 곧 결과가 옵니다. (잠시만요)")
            except Exception:
                pass
        return
    # locked() 체크 직후 즉시 acquire — 사이에 await 없어 cooperative 스케줄링에서 atomic
    # (체크~acquire 사이 await가 있으면 두 호출이 직렬 중복 실행되는 TOCTOU 발생)
    await _screener_lock.acquire()
    try:
        await _screener_daily_job_locked(bot, override_chat_id)
    finally:
        _screener_lock.release()


async def _screener_daily_job_locked(bot: Bot, override_chat_id: str | None) -> None:
    """screener_daily_job 본체 — 호출자가 _screener_lock을 잡은 상태."""
    # 발송 대상 chat_id 리스트 결정
    if override_chat_id:
        target_chat_ids = [str(override_chat_id)]
    else:
        # admin (env) + 가입자 (DB) union
        admin_ids = _parse_chat_ids(ALLOWED_ENV, CHAT_ID_ENV)
        try:
            from src.screener import subscribers as _subs
            sub_ids = _subs.list_active_chat_ids()
        except Exception:
            log.exception("[scheduled] subscribers 조회 실패")
            sub_ids = []
        # admin + subscribers union, 중복 제거, '*' 같은 비정상 값 자동 필터됨
        target_chat_ids = list(dict.fromkeys(admin_ids + sub_ids))

    if not target_chat_ids:
        log.error("발송 대상 chat_id 없음 — 스킵 (admin env 또는 subscribers 등록 필요)")
        return

    log.info("[scheduled] 발송 대상 %d명: %s", len(target_chat_ids), target_chat_ids)
    progress_chat = target_chat_ids[0]  # 진행 메시지는 첫 대상자(admin 우선)

    loop = asyncio.get_running_loop()
    try:
        from src.screener import backfill, db, formatter, incremental, signals, universe
    except Exception:
        log.exception("[scheduled] screener 모듈 import 실패")
        try:
            await send_text_chunked(bot, progress_chat, "⚠️ screener import 실패 — 로그 확인")
        except Exception:
            pass
        return

    # 진행 메시지 helper (admin에게만)
    chat_id = progress_chat  # 기존 코드 변수명 유지 (진행 메시지용)

    try:
        # universe 보장
        await loop.run_in_executor(None, db.ensure_schema)
        if not await loop.run_in_executor(None, lambda: bool(db.get_active_tickers())):
            await send_text_chunked(bot, chat_id, "🌐 종목 유니버스 빌드 중...")
            count = await loop.run_in_executor(None, universe.refresh_universe)
            await loop.run_in_executor(
                None, lambda: db.meta_set("universe_refreshed_at", datetime.now(KST).date().isoformat()))
            await send_text_chunked(bot, chat_id, f"🌐 활성 종목 {count}개")
        else:
            # 주 1회 universe 풀 갱신 — 신규 상장·섹터 변경 반영.
            # cron에서만 (override는 /screen fast path — 기존 DB 사용 설계).
            if override_chat_id is None:
                try:
                    last = await loop.run_in_executor(
                        None, lambda: db.meta_get("universe_refreshed_at"))
                    stale = True
                    if last:
                        stale = (datetime.now(KST).date()
                                 - datetime.fromisoformat(last).date()).days >= 7
                    if stale:
                        count = await loop.run_in_executor(None, universe.refresh_universe)
                        await loop.run_in_executor(
                            None, lambda: db.meta_set(
                                "universe_refreshed_at", datetime.now(KST).date().isoformat()))
                        log.info("[scheduled] 주간 universe 갱신: 활성 %d종목", count)
                except Exception:
                    log.exception("[scheduled] 주간 universe 갱신 실패 — 기존 universe로 진행")
            # 시총 갱신 (시장 변동 반영 + 기존 DB의 NULL 시총 채우기)
            try:
                updated = await loop.run_in_executor(None, universe.refresh_market_caps)
                if updated:
                    log.info("[scheduled] 시총 갱신 %d종목", updated)
                # 헬스 워치독: 평소 ~2700종목, 베이스라인 미만이면 admin에 ⚠
                try:
                    from src.admin_alerts import alert_admin, BASELINES
                    if updated is not None and updated < BASELINES["kr_caps"]:
                        await alert_admin(bot, ("SCREENER_ALLOWED_CHAT_IDS", "SCREENER_CHAT_ID"),
                                          "⚠ KR 시총 fetch 급감",
                                          f"평소 ~2700종목, 오늘 {updated}종목 — pykrx 포맷 변경 의심")
                except Exception:
                    log.exception("[scheduled] kr 헬스 알림 실패")
            except Exception:
                log.exception("[scheduled] 시총 갱신 실패 — 신호 계산은 진행")

        # 데이터 충분성 진단 (52주 신고가는 252일+ 필요)
        lengths = await loop.run_in_executor(None, db.ticker_data_lengths)
        log.info("[scheduled] ticker_data_lengths: %s", lengths)

        # 백필 트리거 조건:
        #   - DB 비었음 (첫 실행)
        #   - 강제 (SCREENER_FORCE_BACKFILL=1)
        #   - 252일+ 종목이 ohlcv 보유의 30% 미만 (52주 신고가 산출 불가)
        #   - max_len < 1000 (5년 데이터 미확보 — 역사적 신고가 ATH 계산 불가). flag 무관.
        #     5년 backfill 1회 성공 후 max_len≈1260 → 다음부터 skip.
        force = os.getenv("SCREENER_FORCE_BACKFILL", "0") == "1"
        rc = await loop.run_in_executor(None, db.row_count)
        ge_252 = lengths.get("ge_252", 0)
        total_t = lengths.get("total_tickers", 0) or 1
        max_len = lengths.get("max_len", 0)
        insufficient = (ge_252 / total_t) < 0.30
        backfill_done = await loop.run_in_executor(None, lambda: db.meta_get("naver_backfill_done"))
        need_backfill = (
            (rc == 0) or force
            or (insufficient and not backfill_done)
            or (max_len < 1000)
        )

        if need_backfill:
            reason = "첫 실행" if rc == 0 else ("강제" if force else f"252일+ 종목 부족 ({ge_252}/{total_t})")
            await send_text_chunked(
                bot, chat_id,
                f"📥 1년치 백필 시작 ({reason}, Naver 기반 ~6-15분)",
            )

            def _progress(done: int, total: int, success: int) -> None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        send_text_chunked(
                            bot, chat_id,
                            f"📥 백필 진행 {done}/{total} (성공 {success})",
                        ),
                        loop,
                    )
                except Exception:
                    log.exception("progress push 실패")

            result = await loop.run_in_executor(
                None, lambda: backfill.run_full_backfill(progress_cb=_progress)
            )
            await send_text_chunked(
                bot, chat_id,
                f"✅ 백필 완료: mode={result.get('mode')} success={result['success']} "
                f"fail={result['fail']} rows={result['rows']}",
            )
            # 백필 후 데이터 길이 재진단
            lengths2 = await loop.run_in_executor(None, db.ticker_data_lengths)
            log.info("[scheduled] 백필 후 ticker_data_lengths: %s", lengths2)

        if override_chat_id:
            # 수동 /screen: 무거운 재수집(Naver 1200종목, ~10분) 생략 → 기존 DB 최신일자로 즉시 계산.
            # validator가 신호 발생 종목을 Naver로 재검증하므로 정확성 유지. DB 비었을 때만 최소 보장.
            latest = await loop.run_in_executor(None, db.latest_date)
            if not latest:
                log.info("[scheduled] /screen: DB 비어있음 → ensure_recent_business_day_data")
                await loop.run_in_executor(None, incremental.ensure_recent_business_day_data)
            else:
                log.info("[scheduled] /screen override → Naver 재수집 생략, DB 최신=%s 사용", latest)
        else:
            # 16:00 cron: 오늘 1일치 fetch + KRX 미발행 대비 retry(5분×6=최대 30분) + 폴백
            retry_interval = int(os.getenv("SCREENER_RETRY_INTERVAL_S", "300"))
            retry_max = int(os.getenv("SCREENER_RETRY_MAX", "6"))
            inc = await loop.run_in_executor(None, incremental.update_today)
            attempt = 1
            while inc.get("empty") and inc.get("is_business_day") and attempt < retry_max:
                log.info("[scheduled] today fetch 미발행 → %d초 후 재시도 (%d/%d)",
                         retry_interval, attempt, retry_max)
                await asyncio.sleep(retry_interval)
                inc = await loop.run_in_executor(None, incremental.update_today)
                attempt += 1
            if inc.get("empty"):
                log.info("[scheduled] today 데이터 미수신 → ensure_recent_business_day_data")
                ensured = await loop.run_in_executor(None, incremental.ensure_recent_business_day_data)
                log.info("[scheduled] ensure_recent_business_day 결과: %s", ensured)

        # 진단: 사용자 메시지에 나온 종목들의 last 7일치 close 출력 (DB값과 실제 비교 위함)
        # 삼성E&A(028050), 가온전선(000500), 한솔테크닉스(004710), 두산에너빌리티(034020),
        # 삼성E&A는 사용자가 -3.11%로 검증, 우리 메시지는 +21.5% — 어느 ticker가 잘못됐는지 진단
        try:
            for diag_t in ("005930", "000660", "028050", "000500", "004710", "034020", "001120", "021820"):
                rows_diag = await loop.run_in_executor(None, lambda t=diag_t: db.load_ohlcv(t, days=7))
                tinfo = await loop.run_in_executor(None, lambda t=diag_t: db.get_ticker_name(t))
                if rows_diag:
                    snap = [(r["date"], r["close"]) for r in rows_diag]
                    log.warning("[scheduled] DIAG %s(%s) last7=%s", diag_t, tinfo, snap)
                else:
                    log.warning("[scheduled] DIAG %s(%s) NO DATA in DB", diag_t, tinfo)
            # name으로도 검색: '삼성E' 또는 '삼성엔지니어링' 들어간 ticker 모두 출력
            from src.screener import db as _db
            with _db._conn() as c:
                cur = c.execute(
                    "SELECT ticker, name, market FROM tickers "
                    "WHERE name LIKE '%삼성E%' OR name LIKE '%삼성엔지%' OR name LIKE '%E&A%' "
                    "ORDER BY ticker"
                )
                for row in cur.fetchall():
                    log.warning("[scheduled] DIAG name match: ticker=%s name=%s market=%s", *row)
        except Exception:
            log.exception("[scheduled] DIAG load_ohlcv 실패")

        # 기준일 = DB의 가장 최근 OHLCV 날짜 (명시적 결정 → signals에 전달)
        base_date = await loop.run_in_executor(None, db.latest_date) or datetime.now(KST).strftime("%Y-%m-%d")
        log.info("[scheduled] base_date for signals: %s", base_date)

        # 신호 계산 — 모든 종목이 동일 base_date 강제, 미보유 종목 자동 skip
        results, stats = await loop.run_in_executor(
            None, lambda: signals.compute_all(base_date=base_date)
        )
        log.info("[scheduled] signals stats: %s", stats)
        if not results:
            await send_text_chunked(bot, chat_id, "⚠️ 신호 계산 결과 비어있음 — DB 확인")
            return

        # 이중확인: 신호 종목들의 base_date close를 Naver에서 다시 fetch → DB와 대조
        # 불일치(또는 fetch 실패) 종목은 메시지에서 제외 (잘못된 신호 영구 차단)
        try:
            from src.screener import validator
            results, val_stats = await loop.run_in_executor(
                None, lambda: validator.cross_validate(results, base_date)
            )
            log.info("[scheduled] validator stats: %s", val_stats)
            stats["validated"] = val_stats.get("validated", 0)
            stats["rejected"] = val_stats.get("rejected", 0)
        except Exception:
            log.exception("[scheduled] validator 실패 — 검증 없이 발송")
            stats["validated"] = -1
            stats["rejected"] = -1

        # 히스토리 저장 (검증 통과만)
        try:
            await loop.run_in_executor(None, lambda: db.save_signals(base_date, results))
        except Exception:
            log.exception("signal 히스토리 저장 실패")

        # 진행 ping (수동 /screen 한정) — 사용자가 진행 상황 알도록
        if override_chat_id:
            try:
                uniq = {it.get("ticker") for items in results.values() for it in items
                        if it.get("ticker")}
                if uniq:
                    eta = max(30, len(uniq) * 3)
                    await send_text_chunked(
                        bot, str(override_chat_id),
                        f"📈 신호 {len(uniq)}개 발견 — 차트 채널 게시 중 (~{eta}초)")
            except Exception:
                log.exception("[scheduled] 진행 ping 실패 — 무시하고 진행")

        # 종목별 채널 차트 게시 + ytd/eps 메타 (채널 미설정 시 메타만)
        try:
            links, extra = await _post_charts_and_meta(results, base_date)
        except Exception:
            log.exception("[scheduled] 차트 게시/메타 실패 — 링크 없이 발송")
            links, extra = {}, {}

        # 회고: 지난 신호 종목들의 5영업일 후 평균 수익률 (DB signals 히스토리)
        retro = {}
        try:
            from src.screener import retrospective as retro_mod
            retro = await loop.run_in_executor(
                None, lambda: retro_mod.signal_returns(
                    days_back=10, days_ahead=5, today_iso=base_date))
            if retro:
                log.info("[scheduled] 회고 집계: %s",
                         {k: f"n={v['n']} avg={v['avg_return_pct']}" for k, v in retro.items()})
        except Exception:
            log.exception("[scheduled] 회고 집계 실패 — 회고 없이 발송")

        # 발송 — formatter에 base_date + stats + links + extra + retro 전달
        text = formatter.format_results(
            results, datetime.now(KST), base_date=base_date, stats=stats,
            links=links, extra=extra, retro=retro,
        )
        # 모든 대상자에게 broadcast (한 번 계산 → N번 발송)
        sent_count = 0
        for cid in target_chat_ids:
            try:
                await send_text_chunked(bot, cid, text, parse_mode=ParseMode.HTML)
                sent_count += 1
            except Exception:
                log.exception("[scheduled] 발송 실패 cid=%s", cid)
        log.info("[scheduled] 발송 완료 (base_date=%s, sent=%d/%d)",
                 base_date, sent_count, len(target_chat_ids))
    except Exception:
        log.exception("[scheduled] screener_daily_job 실패")
        try:
            await send_text_chunked(bot, chat_id, "⚠️ 스크리너 작업 실패 — 로그 확인")
        except Exception:
            pass


# ------------------------------------------------------------------
# Self-test (CLAUDE.md 자동 검증 의무)
# ------------------------------------------------------------------
async def _self_test(bot: Bot) -> None:
    """SCREENER_TEST_MODE=1 시 부팅 후 1회 자동 실행.

    chat_id 결정: SCREENER_TEST_CHAT_ID → 통합 robust 파싱
    (CHAT_ID_ENV + ALLOWED_ENV에서 진짜 chat_id만 추출. wildcard '*'·주석 등 무시).
    """
    test_chat = os.getenv("SCREENER_TEST_CHAT_ID", "").strip()
    chat_id = None
    if test_chat:
        import re
        m = re.search(r"-?\d{6,}", test_chat)
        if m:
            chat_id = m.group(0)
    if not chat_id:
        ids = _parse_chat_ids(CHAT_ID_ENV, ALLOWED_ENV)
        if ids:
            chat_id = ids[0]
    if not chat_id:
        log.warning("[self-test] chat_id 없음 — 스킵")
        return
    log.info("=" * 60)
    log.info("[self-test] screener 자동 검증 시작 chat_id=%s", chat_id)
    log.info("=" * 60)
    await asyncio.sleep(15)  # 폴링 안정화 대기
    try:
        await screener_daily_job(bot, override_chat_id=chat_id)
    except Exception:
        log.exception("[self-test] 파이프라인 최상위 예외")
    log.info("[self-test] 종료")


async def _verify_once(bot: Bot) -> None:
    """SCREENER_VERIFY_ONCE=1 시 부팅 후 1회 — breadth + backtest를 기존 DB로 계산해
    로그·admin에 출력(라이브 검증용). Naver 재fetch 없음(빠름). 실패는 로그만."""
    await asyncio.sleep(20)  # 폴링 안정화 대기
    loop = asyncio.get_running_loop()
    ids = _parse_chat_ids(CHAT_ID_ENV, ALLOWED_ENV)
    admin = ids[0] if ids else None
    log.info("=" * 60)
    log.info("[verify] 라이브 검증 시작 (breadth + backtest)")

    # breadth
    try:
        from src.screener import breadth
        b = await loop.run_in_executor(None, breadth.compute_breadth)
        report = breadth.format_breadth_full(b)
        log.info("[verify] BREADTH\n%s", report)
        if admin:
            await send_text_chunked(bot, admin, report)
    except Exception:
        log.exception("[verify] breadth 실패")

    # backtest
    try:
        from src.screener import backtest
        res = await loop.run_in_executor(
            None, lambda: backtest.backtest_signals(lookback=90, sample_every=5,
                                                    horizons=(5, 10, 20)))
        report = backtest.format_backtest(res)
        log.info("[verify] BACKTEST\n%s", report)
        if admin:
            await send_text_chunked(bot, admin, report)
    except Exception:
        log.exception("[verify] backtest 실패")
    log.info("[verify] 종료")


# ------------------------------------------------------------------
# Entry point (orchestrator가 호출)
# ------------------------------------------------------------------
SCREENER_COMMANDS = [
    # 가입자 공개
    ("start", "🚀 가입 (자동 발송 활성화)"),
    ("screen", "📈 즉시 스크리닝 실행"),
    ("stop", "탈퇴 (자동 발송 해제)"),
    ("help", "도움말"),
    # admin 전용 — 비인가자는 누르면 deny 메시지
    ("status", "🔧 DB·신호 상태 (admin)"),
    ("diag", "🔧 종목 누락 원인 추적 — /diag <코드|이름> (admin)"),
    ("backtest", "🔧 신호 forward 수익률 백테스트 (admin)"),
    ("breadth", "🔧 시장 폭 지표 (admin)"),
    ("backfill", "🔧 1년치 OHLCV 백필 (admin)"),
    ("list", "🔧 가입자/차단 목록 (admin)"),
    ("block", "🔧 chat_id 차단 (admin)"),
    ("unblock", "🔧 차단 해제 (admin)"),
]


def build_screener_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    # 누구나 (가입/도움말/screen은 가입자만 내부 가드)
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("stop", _cmd_stop))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("screen", _cmd_screen))
    # admin 전용
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("diag", _cmd_diag))
    app.add_handler(CommandHandler("backtest", _cmd_backtest))
    app.add_handler(CommandHandler("breadth", _cmd_breadth))
    app.add_handler(CommandHandler("backfill", _cmd_backfill))
    app.add_handler(CommandHandler("list", _cmd_list))
    app.add_handler(CommandHandler("block", _cmd_block))
    app.add_handler(CommandHandler("unblock", _cmd_unblock))

    if os.getenv("SCREENER_TEST_MODE", "0") == "1":
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_self_test(app.bot))
        except RuntimeError:
            pass
    if os.getenv("SCREENER_VERIFY_ONCE", "0") == "1":
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_verify_once(app.bot))
        except RuntimeError:
            pass
    return app
