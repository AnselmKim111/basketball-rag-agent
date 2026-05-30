"""US ScreenerBot — 미국 주식 기술적 신호 스크리너 (S&P500 + Nasdaq100).

매일 07:00 KST에 자동 실행 (미국 4PM ET 장마감 + 데이터 발행 버퍼). 사용자 명령:
  /start (가입), /stop (탈퇴), /help, /screen (즉시), /status, /backfill
  /list (admin), /block <chat_id> (admin), /unblock <chat_id> (admin)

한국 ScreenerBot과 완전 격리. 자체 SQLite DB(/data/us_screener.db), 자체 봇 토큰.
시장 무관 순수 함수(compute_signals_for_ticker)는 한국 코드에서 재사용.
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
from src.us_screener import subscribers as subs

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ALLOWED_ENV = "US_SCREENER_ALLOWED_CHAT_IDS"  # admin chat_ids (콤마 구분)
CHAT_ID_ENV = "US_SCREENER_CHAT_ID"  # legacy: 단일 chat_id (admin 호환용)


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
    "👋 *US ScreenerBot에 오신 걸 환영합니다!*\n\n"
    "🇺🇸 미국 주식(S&P500 + Nasdaq100) 기술적 신호를 매일 07:00 KST에 자동 발송합니다.\n"
    "(미국 4PM ET 장마감 + 데이터 발행 버퍼)\n\n"
    "신호:\n"
    "  🚀 역사적 신고가  📈 52주 신고가\n"
    "  🔥 거래량 돌파 ≥2배  🎯 52주 돌파 직전\n"
    "  💎 VCP 돌파 (변동성 수축 후 돌파)\n\n"
    "명령:\n"
    "  /screen — 지금 즉시 신호 분석 (~3-5분)\n"
    "  /help — 도움말\n"
    "  /stop — 자동 발송 해제\n\n"
    "_시총 $1B+ S&P500/Nasdaq100 종목, 섹터별 분류, 시총·상승률 복합 정렬, "
    "이중확인 통과만 발송_"
)

HELP_TEXT = (
    "🇺🇸 *US ScreenerBot* — 미국 주식 기술적 신호\n\n"
    "자동: 매일 07:00 KST (미국 4PM ET 장마감 기준)\n"
    "데이터: FDR(Yahoo) 1순위 + Stooq 폴백\n"
    "유니버스: S&P500 + Nasdaq100, 시총 ≥ $1B\n"
    "이중확인: base_date-anchored signals + 재 fetch cross-validation\n\n"
    "신호:\n"
    "  🚀 역사적 신고가 — 종가 > 보유 데이터(280일) 최고가\n"
    "  📈 52주 신고가 — 종가 > 과거 252영업일 최고가\n"
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
        await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.MARKDOWN)
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
                            f"🆕 US ScreenerBot 신규 가입자\n"
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
        await deny_message(update, "US 스크리너봇")
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
        await deny_message(update, "US 스크리너봇")
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
        await deny_message(update, "US 스크리너봇")
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
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        log.exception("help reply 실패")


async def _cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_subscribed_or_admin(update):
        await update.message.reply_text(
            "🔒 가입자만 사용 가능합니다. /start 입력으로 가입하세요."
        )
        return
    try:
        await update.message.reply_text("🔄 스크리닝 즉시 실행 중... (~2-3분 소요)")
    except Exception:
        log.exception("screen 안내 실패")
    try:
        await us_screener_daily_job(context.bot, override_chat_id=str(update.effective_chat.id))
    except Exception:
        log.exception("[screen] 실행 실패")


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "US 스크리너봇")
        return
    loop = asyncio.get_running_loop()
    try:
        from src.us_screener import db as screener_db
        info = await loop.run_in_executor(None, screener_db.status)
        await send_text_chunked(context.bot, str(update.effective_chat.id), info)
    except Exception:
        log.exception("[status] 실패")
        try:
            await update.message.reply_text("⚠️ status 실패 — 로그 확인")
        except Exception:
            pass


async def _cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "US 스크리너봇")
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
        from src.us_screener import backfill
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
def _parse_chat_ids(*env_keys: str) -> list[str]:
    """env 여러 개를 합쳐서 진짜 chat_id (숫자 token) 만 추출.

    걸러내는 케이스:
      - '*' (wildcard 인증용 sentinel — 발송 대상 아님)
      - '1813560888    (← 주석)' → '1813560888' 만 추출
      - 빈 문자열 / 공백
      - 음수·0 같은 비정상 값
    """
    import re
    out: list[str] = []
    seen: set[str] = set()
    for key in env_keys:
        raw = os.getenv(key, "") or ""
        for token in raw.split(","):
            # 숫자만 (보통 9~12자리 텔레그램 chat_id)
            m = re.search(r"-?\d{6,}", token)
            if not m:
                continue
            cid = m.group(0)
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
    return out


_BADGE = {
    "high_all": "💥 역사적 신고가 돌파",
    "high_52w": "📈 52주 신고가",
    "vcp_breakout": "💎 VCP 돌파",
    "volume_breakout": "🔥 거래량 돌파",
    "near_breakout_52w": "🎯 52주 돌파 직전",
}


def _fmt_usd(v) -> str:
    if not isinstance(v, (int, float)) or v <= 0:
        return "N/A"
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def _fmt_pct(v) -> str:
    return f"{v:+.1f}%" if isinstance(v, (int, float)) else "N/A"


def _permalink(channel: str, message_id: int) -> str | None:
    """채널 게시물 영구 링크. @username → 공개, -100… → 비공개 /c/ 형식."""
    if channel.startswith("@"):
        return f"https://t.me/{channel.lstrip('@')}/{message_id}"
    cid = channel.lstrip()
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None


def _chart_caption(ticker: str, item: dict, cats: list[str], rows: list[dict],
                   ytd, eps, market_cap=None) -> str:
    from src.us_screener import fundamentals
    from src.bot_helpers import html_escape
    name = html_escape(item.get("name") or ticker)
    chg = item.get("chg_pct") or 0.0
    turnover = fundamentals.turnover_usd(rows[-1]) if rows else 0
    badge = " ".join(_BADGE[c] for c in cats if c in _BADGE)
    lines = [
        f"🇺🇸 {html_escape(ticker)} ({chg:+.1f}%)",
        badge,
        "",
        f"✝ 종목명 : {name}",
        f"✝ 시가총액 : {_fmt_usd(market_cap)}",
        f"✝ 거래대금 : {_fmt_usd(turnover)}",
        f"✝ 연초대비 상승률 : {_fmt_pct(ytd)}",
        f"✝ 최근분기 EPS YoY : {_fmt_pct(eps)}",
        f'✝ <a href="https://finviz.com/quote.ashx?t={ticker}">최신 종목 뉴스 조회</a>',
    ]
    return "\n".join(lines)


async def _post_charts_and_meta(results: dict, max_tickers: int = 120) -> tuple[dict, dict]:
    """신호 티커별 (1) 채널 차트 게시 → permalink, (2) ytd·eps 메타 산출.

    채널 토큰/ID 미설정이면 게시는 건너뛰고 메타(ytd·eps)만 계산(4열 포맷 enrich).
    반환: (links: {ticker: url}, extra: {ticker: {ytd, eps_yoy}}).
    """
    from src.us_screener import chart, db, fundamentals

    # 카테고리 dedupe + 티커별 badge
    by_ticker: dict[str, dict] = {}
    badges: dict[str, list] = {}
    for cat, items in results.items():
        if cat == "volume_breakout":
            continue  # 거래량 돌파는 메시지·채널서 제외
        for it in items:
            t = it.get("ticker")
            if not t:
                continue
            by_ticker.setdefault(t, it)
            badges.setdefault(t, []).append(cat)

    token = os.getenv("US_SCREENER_CHART_BOT_TOKEN", "").strip()
    channel = os.getenv("US_SCREENER_CHART_CHANNEL_ID", "").strip()
    chart_bot = None
    if token and channel:
        try:
            chart_bot = Bot(token=token)
        except Exception:
            log.exception("[us_screener] 차트봇 생성 실패 — 게시 생략")
    else:
        log.info("[us_screener] 차트 채널 미설정 — 링크 없이 메타만 계산")

    links: dict[str, str] = {}
    extra: dict[str, dict] = {}
    loop = asyncio.get_event_loop()
    posted = 0
    mcap_n = 0
    for t, it in list(by_ticker.items())[:max_tickers]:
        try:
            rows = await loop.run_in_executor(None, lambda t=t: db.load_ohlcv(t, days=260))
            ytd = fundamentals.ytd_pct(rows)
            f = await loop.run_in_executor(None, lambda t=t: fundamentals.ticker_fundamentals(t))
            eps = f.get("eps_yoy")
            extra[t] = {"ytd": ytd, "eps_yoy": eps}
            # 시가총액: DB값 우선, 없으면 SEC 발행주식수 × 종가($)
            mcap = it.get("market_cap")
            if not mcap and rows and f.get("shares"):
                mcap = (rows[-1]["close"] / 100.0) * f["shares"]
            if mcap:
                mcap_n += 1
            if chart_bot and rows:
                png = await loop.run_in_executor(
                    None, lambda t=t, rows=rows, it=it: chart.render_candle_volume(
                        t, rows, title=f"{t} — {it.get('name', '')}"))
                if png:
                    from src.bot_helpers import send_channel_photo
                    cap = _chart_caption(t, it, badges[t], rows, ytd, eps, market_cap=mcap)
                    msg = await send_channel_photo(chart_bot, channel, png, cap, ParseMode.HTML)
                    if msg:
                        url = _permalink(channel, msg.message_id)
                        if url:
                            links[t] = url
                        posted += 1
                    await asyncio.sleep(3.0)  # 채널 ~20건/분 한도 → 게시 간 간격 (성공/실패 무관 페이싱)
        except Exception:
            log.exception("[us_screener] 티커 처리 실패 %s", t)
    log.info("[us_screener] 채널 게시 %d건 · 메타 %d종목 (links=%d, 시총확보=%d)",
             posted, len(extra), len(links), mcap_n)
    return links, extra


async def us_screener_daily_job(bot: Bot, override_chat_id: str | None = None) -> None:
    """매일 16:00 cron 또는 /screen 즉시 실행.

    override_chat_id: 명시되면 그 chat_id 1명에게만 (사용자 /screen 명령용).
    None이면 모든 가입자 + admin에게 broadcast (cron용).

    신호 계산은 한 번만 수행 + 결과를 모든 대상자에게 발송 (효율).
    진행 상황(universe 빌드/백필) 메시지는 첫 대상자(주로 admin)에게만 발송.
    """
    log.info("[scheduled] us_screener_daily_job 시작 override=%s", override_chat_id)

    # 발송 대상 chat_id 리스트 결정
    if override_chat_id:
        target_chat_ids = [str(override_chat_id)]
    else:
        # admin (env) + 가입자 (DB) union
        admin_ids = _parse_chat_ids(ALLOWED_ENV, CHAT_ID_ENV)
        try:
            from src.us_screener import subscribers as _subs
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
        from src.us_screener import backfill, db, formatter, incremental, signals, universe
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
        # universe 보장 — 미국은 S&P500+Nasdaq100 ~550종목으로 fetch 빠름(~2초).
        # 매번 refresh_universe (upsert) 호출해 NASDAQ100 신규 종목·섹터 항상 반영.
        await loop.run_in_executor(None, db.ensure_schema)
        try:
            count = await loop.run_in_executor(None, universe.refresh_universe)
            log.info("[scheduled] universe 갱신 %d종목", count)
        except Exception:
            log.exception("[scheduled] universe 갱신 실패 — 신호 계산은 진행")

        # 데이터 충분성 진단 (52주 신고가는 252일+ 필요)
        lengths = await loop.run_in_executor(None, db.ticker_data_lengths)
        log.info("[scheduled] ticker_data_lengths: %s", lengths)

        # 백필 트리거 조건:
        #   - DB 비었음 (첫 실행)
        #   - 강제 (US_SCREENER_FORCE_BACKFILL=1)
        #   - 252일+ 종목이 ohlcv 보유 종목의 30% 미만 (52주 신고가 산출 불가)
        #   - active universe 대비 ohlcv 60일+ 미보유 종목 10개 초과 (신규 종목 — NASDAQ100
        #     추가분 등 — 데이터 backfill 필요). backfill_done flag 무관하게 트리거.
        #   - max_len < 1000 (5년 데이터 미확보 — 역사적 신고가 ATH 계산 불가). flag 무관.
        force = os.getenv("US_SCREENER_FORCE_BACKFILL", "0") == "1"
        rc = await loop.run_in_executor(None, db.row_count)
        ge_252 = lengths.get("ge_252", 0)
        ge_60 = lengths.get("ge_60", 0)
        total_t = lengths.get("total_tickers", 0) or 1
        max_len = lengths.get("max_len", 0)
        total_active = await loop.run_in_executor(None, lambda: len(db.get_active_tickers()))
        insufficient = (ge_252 / total_t) < 0.30
        missing = total_active - ge_60  # ohlcv 60일+ 미보유 active 종목 (신규 상장/추가분)
        backfill_done = await loop.run_in_executor(None, lambda: db.meta_get("naver_backfill_done"))
        need_backfill = (
            (rc == 0) or force
            or (insufficient and not backfill_done)
            or (missing > 10)
            or (max_len < 1000)
        )

        if need_backfill:
            reason = (
                "첫 실행" if rc == 0 else ("강제" if force
                else (f"신규 종목 {missing}개 데이터 부족" if missing > 10
                      else f"252일+ 종목 부족 ({ge_252}/{total_t})"))
            )
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

        # 증분 (오늘 1일치) — KRX 16:30 이전 미발행 대비 retry
        # US_SCREENER_RETRY_INTERVAL_S(기본 300=5분), US_SCREENER_RETRY_MAX(기본 6회 → 최대 30분)
        retry_interval = int(os.getenv("US_SCREENER_RETRY_INTERVAL_S", "300"))
        retry_max = int(os.getenv("US_SCREENER_RETRY_MAX", "6"))
        inc = await loop.run_in_executor(None, incremental.update_today)
        attempt = 1
        while inc.get("empty") and inc.get("is_business_day") and attempt < retry_max:
            log.info(
                "[scheduled] today fetch 미발행 → %d초 후 재시도 (%d/%d)",
                retry_interval, attempt, retry_max,
            )
            await asyncio.sleep(retry_interval)
            inc = await loop.run_in_executor(None, incremental.update_today)
            attempt += 1

        # 영업일인데도 끝까지 today 미수신이면 어제 영업일 데이터라도 보장
        if inc.get("empty"):
            log.info("[scheduled] today 데이터 미수신 → ensure_recent_business_day_data")
            ensured = await loop.run_in_executor(None, incremental.ensure_recent_business_day_data)
            log.info("[scheduled] ensure_recent_business_day 결과: %s", ensured)

        # 진단: 대표 미국 종목 last 7일치 close 출력 (cent 단위 → /100 = $)
        try:
            for diag_t in ("AAPL", "MSFT", "NVDA", "BRKB", "BFB"):
                rows_diag = await loop.run_in_executor(None, lambda t=diag_t: db.load_ohlcv(t, days=7))
                if rows_diag:
                    snap = [(r["date"], round(r["close"] / 100, 2)) for r in rows_diag]
                    log.warning("[scheduled] DIAG %s last7($)=%s", diag_t, snap)
                else:
                    log.warning("[scheduled] DIAG %s NO DATA in DB", diag_t)
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
            from src.us_screener import validator
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

        # 티커별 채널 차트 게시 + ytd/eps 메타 (채널 미설정 시 메타만)
        try:
            links, extra = await _post_charts_and_meta(results)
        except Exception:
            log.exception("[scheduled] 차트 게시/메타 실패 — 링크 없이 발송")
            links, extra = {}, {}

        # 발송 — formatter에 base_date + stats + links + extra 전달 (4열 하이퍼링크)
        text = formatter.format_results(
            results, datetime.now(KST), base_date=base_date, stats=stats,
            links=links, extra=extra,
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
        log.exception("[scheduled] us_screener_daily_job 실패")
        try:
            await send_text_chunked(bot, chat_id, "⚠️ 스크리너 작업 실패 — 로그 확인")
        except Exception:
            pass


# ------------------------------------------------------------------
# Self-test (CLAUDE.md 자동 검증 의무)
# ------------------------------------------------------------------
async def _self_test(bot: Bot) -> None:
    """US_SCREENER_TEST_MODE=1 시 부팅 후 1회 자동 실행.

    chat_id 결정: US_SCREENER_TEST_CHAT_ID → 통합 robust 파싱
    (CHAT_ID_ENV + ALLOWED_ENV에서 진짜 chat_id만 추출. wildcard '*'·주석 등 무시).
    """
    test_chat = os.getenv("US_SCREENER_TEST_CHAT_ID", "").strip()
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
        await us_screener_daily_job(bot, override_chat_id=chat_id)
    except Exception:
        log.exception("[self-test] 파이프라인 최상위 예외")
    log.info("[self-test] 종료")


# ------------------------------------------------------------------
# Entry point (orchestrator가 호출)
# ------------------------------------------------------------------
US_SCREENER_COMMANDS = [
    ("start", "🚀 가입 (자동 발송 활성화)"),
    ("screen", "📈 즉시 스크리닝 실행"),
    ("stop", "탈퇴 (자동 발송 해제)"),
    ("help", "도움말"),
]


def build_us_screener_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    # 누구나 (가입/도움말/screen은 가입자만 내부 가드)
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("stop", _cmd_stop))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("screen", _cmd_screen))
    # admin 전용
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("backfill", _cmd_backfill))
    app.add_handler(CommandHandler("list", _cmd_list))
    app.add_handler(CommandHandler("block", _cmd_block))
    app.add_handler(CommandHandler("unblock", _cmd_unblock))

    if os.getenv("US_SCREENER_TEST_MODE", "0") == "1":
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_self_test(app.bot))
        except RuntimeError:
            pass
    return app
