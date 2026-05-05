"""DisclosureBot — 사용자별 watchlist + DART 공시 실시간 알림 텔레그램 봇.

명령:
  /watch <티커 또는 종목명>     — watchlist에 추가 (즉시 dedup seeding으로 폭격 방지)
  /unwatch <티커>                — 제거
  /watchlist                     — 현재 추적 종목 + 음소거 상태
  /mute <티커> <일수>            — 일시 음소거 (예: /mute 005930 7)
  /unmute <티커>                 — 음소거 해제

자동 폴링은 disclosure_watcher.poll_and_dispatch가 APScheduler로 5분마다 실행
(orchestrator에서 등록).

격리 원칙:
  - 토큰 (`DISCLOSURE_BOT_TOKEN`) 미설정 → orchestrator가 spec.optional=True로 skip.
  - 모든 핸들러 graceful (예외 raise 안 함).
  - watchlist_store / state_store / dart_client 만 사용 — 기존 봇 코드 안 건드림.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from src import disclosure_watcher, watchlist_store
from src.bot_helpers import (
    deny_message,
    is_authorized,
    send_text_chunked,
)

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ALLOWED_ENV = "DISCLOSURE_ALLOWED_CHAT_IDS"

HELP_TEXT = (
    "📡 *DART 공시 실시간 알림 봇*\n\n"
    "*개념:* 등록한 종목에 새 공시가 뜨면 5분 안에 알림 (잠정실적·주요사항·자기주식 등).\n\n"
    "*명령어:*\n"
    "  `/watch 005930` — 삼성전자 추적 시작\n"
    "  `/watch 카카오` — 종목명도 OK (자동 ticker 매핑)\n"
    "  `/unwatch 005930` — 추적 해제\n"
    "  `/watchlist` — 현재 추적 종목 + 음소거 상태\n"
    "  `/mute 005930 7` — 7일 음소거\n"
    "  `/unmute 005930` — 음소거 해제\n\n"
    "*카테고리:* 🚨 critical (잠정실적·주요사항·자사주 등) / 📄 normal (일반)"
)


def _parse_ticker_or_name(arg: str) -> tuple[str | None, str]:
    """입력 → (6자리 ticker, 표시이름). 6자리 숫자면 그대로, 아니면 종목명 lookup.

    실패 시 (None, arg).
    """
    a = arg.strip().zfill(6) if arg.strip().isdigit() else arg.strip()
    if a.isdigit() and len(a) == 6:
        # ticker → name 역방향
        try:
            from src.deepdive import dart_client
            res = dart_client.get_corp_code(a)
            if res:
                return a, res[1]
        except Exception:
            log.exception("ticker → name lookup 실패: %s", a)
        return a, a

    # 종목명 → ticker
    try:
        from src.deepdive import dart_client
        ticker = dart_client.lookup_ticker_by_name(arg)
        if ticker:
            res = dart_client.get_corp_code(ticker)
            return ticker, (res[1] if res else arg)
    except Exception:
        log.exception("종목명 lookup 실패: %s", arg)

    return None, arg


async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "공시봇")
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def _cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "공시봇")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/watch <티커 또는 종목명>` (예: `/watch 005930`, `/watch 삼성전자`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    query = " ".join(args).strip()
    chat_id = str(update.effective_chat.id)

    ticker, name = _parse_ticker_or_name(query)
    if not ticker:
        await update.message.reply_text(
            f"❓ '{query}' 종목 매칭 실패 (6자리 ticker 또는 정확한 종목명 입력)",
        )
        return

    ok, msg = watchlist_store.add(chat_id, ticker, name)
    if ok:
        # 폭격 방지 — 등록 직후 현재 시점까지의 공시는 모두 "이미 본 것" 처리
        seeded = await disclosure_watcher.seed_dedup_for_new_ticker(chat_id, ticker)
        await update.message.reply_text(
            f"✅ {msg}\n📡 5분마다 새 공시 모니터링 시작 (과거 {seeded}건은 dedup 처리됨)",
        )
    else:
        await update.message.reply_text(f"ℹ️ {msg}")


async def _cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "공시봇")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/unwatch <티커 또는 종목명>`", parse_mode=ParseMode.MARKDOWN,
        )
        return
    query = " ".join(args).strip()
    chat_id = str(update.effective_chat.id)
    ticker, _ = _parse_ticker_or_name(query)
    if not ticker:
        await update.message.reply_text(f"❓ '{query}' 종목 매칭 실패")
        return
    ok, msg = watchlist_store.remove(chat_id, ticker)
    await update.message.reply_text(("✅ " if ok else "ℹ️ ") + msg)


async def _cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "공시봇")
        return
    chat_id = str(update.effective_chat.id)
    items = watchlist_store.list_for_chat(chat_id)
    if not items:
        await update.message.reply_text(
            "📭 watchlist 비어있음. `/watch 005930` 으로 시작하세요.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    now = datetime.now(KST)
    lines = [f"📡 watchlist ({len(items)}개)\n"]
    for it in items:
        ticker = it.get("ticker", "")
        name = it.get("name", ticker)
        added = (it.get("added_at") or "")[:10]
        mu = it.get("muted_until")
        mute_str = ""
        if mu:
            try:
                when = datetime.fromisoformat(mu)
                if when > now:
                    mute_str = f"  🔇 음소거 ~{when.strftime('%m-%d')}"
            except Exception:
                pass
        lines.append(f"• {name} ({ticker})  추가일 {added}{mute_str}")
    await send_text_chunked(context.bot, chat_id, "\n".join(lines))


async def _cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "공시봇")
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "사용법: `/mute <티커 또는 종목명> <일수>` (예: `/mute 005930 7`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    chat_id = str(update.effective_chat.id)
    query = args[0]
    try:
        days = int(args[1])
    except (ValueError, TypeError):
        await update.message.reply_text("❌ 일수는 정수여야 합니다 (예: 7)")
        return
    if not (0 <= days <= 365):
        await update.message.reply_text("❌ 일수는 0-365 사이")
        return
    ticker, _ = _parse_ticker_or_name(query)
    if not ticker:
        await update.message.reply_text(f"❓ '{query}' 종목 매칭 실패")
        return
    ok, msg = watchlist_store.mute(chat_id, ticker, days)
    await update.message.reply_text(("✅ " if ok else "ℹ️ ") + msg)


async def _cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "공시봇")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/unmute <티커 또는 종목명>`", parse_mode=ParseMode.MARKDOWN,
        )
        return
    chat_id = str(update.effective_chat.id)
    query = " ".join(args).strip()
    ticker, _ = _parse_ticker_or_name(query)
    if not ticker:
        await update.message.reply_text(f"❓ '{query}' 종목 매칭 실패")
        return
    ok, msg = watchlist_store.mute(chat_id, ticker, 0)
    await update.message.reply_text(("✅ " if ok else "ℹ️ ") + msg)


def build_disclosure_app(token: str) -> Application:
    """오케스트레이터가 호출. 토큰만 받아 Application 반환."""
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(CommandHandler("watch", _cmd_watch))
    app.add_handler(CommandHandler("unwatch", _cmd_unwatch))
    app.add_handler(CommandHandler("watchlist", _cmd_watchlist))
    app.add_handler(CommandHandler("mute", _cmd_mute))
    app.add_handler(CommandHandler("unmute", _cmd_unmute))
    return app


# 스케줄 잡 — orchestrator의 APScheduler가 호출
async def disclosure_poll_job(bot: Bot) -> None:
    """5분마다 호출. 비거래 시간이면 빈도 낮추기 (스킵 5번 중 4번)."""
    if not disclosure_watcher.is_market_hours():
        # 비거래 시간 — 30분에 1번꼴 (5분 주기 × 6 → 30분)
        # 간단한 방법: 30분 기준 분침이 0/30일 때만 실행
        now = datetime.now(KST)
        if now.minute not in (0, 30):
            return
    await disclosure_watcher.poll_and_dispatch(bot)
