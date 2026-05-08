"""ScreenerBot — 한국 주식 기술적 신호 스크리너.

매일 16:30 KST에 자동 실행 (장마감 + 정산 버퍼). 사용자 명령:
  /help, /screen (즉시), /status (DB 상태), /backfill (강제 백필)

격리 원칙: wisereport/PIPELINE_LOCK 미사용. 자체 SQLite DB(/data/screener.db).
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
    make_post_init_set_commands,
    send_text_chunked,
)

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ALLOWED_ENV = "SCREENER_ALLOWED_CHAT_IDS"
CHAT_ID_ENV = "SCREENER_CHAT_ID"

HELP_TEXT = (
    "📈 *ScreenerBot* — 한국 주식 기술적 신호\n\n"
    "자동: 매일 16:30 KST (장마감 후)\n"
    "데이터: KRX (pykrx + FDR) — OHLCV + 시가총액 + 섹터\n"
    "유니버스: KOSPI + KOSDAQ 보통주, 시총 ≥ 3000억\n"
    "표시: 유가증권시장 우선 · 시총·상승률 복합 정렬 · 섹터 표시\n\n"
    "신호:\n"
    "  🚀 역사적 신고가 — 종가 > 보유 데이터(280일) 최고가\n"
    "  📈 52주 신고가 — 종가 > 과거 252영업일 최고가\n"
    "  🔥 거래량 돌파 — 오늘 거래량 ≥ 20일 평균 ×2.0 + 종가 상승\n"
    "  🎯 52주 돌파 직전 — 종가 = 52주고점 95-99% + 5일 거래량 ≥ ×1.3\n\n"
    "명령:\n"
    "  /screen — 즉시 실행\n"
    "  /status — DB 상태 확인\n"
    "  /backfill — 1년치 강제 재백필 (10분+ 소요)\n"
)


# ------------------------------------------------------------------
# 핸들러
# ------------------------------------------------------------------
async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "스크리너봇")
        return
    try:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        log.exception("help reply 실패")


async def _cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "스크리너봇")
        return
    try:
        await update.message.reply_text("🔄 스크리닝 즉시 실행 중...")
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
async def screener_daily_job(bot: Bot, override_chat_id: str | None = None) -> None:
    log.info("[scheduled] screener_daily_job 시작")
    chat_id = override_chat_id or os.environ.get(CHAT_ID_ENV)
    if not chat_id:
        log.error("%s 미설정 — 스킵", CHAT_ID_ENV)
        return

    loop = asyncio.get_running_loop()
    try:
        from src.screener import backfill, db, formatter, incremental, signals, universe
    except Exception:
        log.exception("[scheduled] screener 모듈 import 실패")
        try:
            await send_text_chunked(bot, chat_id, "⚠️ screener import 실패 — 로그 확인")
        except Exception:
            pass
        return

    try:
        # universe 보장
        await loop.run_in_executor(None, db.ensure_schema)
        if not await loop.run_in_executor(None, lambda: bool(db.get_active_tickers())):
            await send_text_chunked(bot, chat_id, "🌐 종목 유니버스 빌드 중...")
            count = await loop.run_in_executor(None, universe.refresh_universe)
            await send_text_chunked(bot, chat_id, f"🌐 활성 종목 {count}개")
        else:
            # 시총 갱신 (시장 변동 반영 + 기존 DB의 NULL 시총 채우기)
            try:
                updated = await loop.run_in_executor(None, universe.refresh_market_caps)
                if updated:
                    log.info("[scheduled] 시총 갱신 %d종목", updated)
            except Exception:
                log.exception("[scheduled] 시총 갱신 실패 — 신호 계산은 진행")

        # DB 비었으면 백필
        force = os.getenv("SCREENER_FORCE_BACKFILL", "0") == "1"
        rc = await loop.run_in_executor(None, db.row_count)
        if rc == 0 or force:
            await send_text_chunked(
                bot, chat_id,
                "📥 첫 실행 — 1년치 백필 시작 (약 10분 소요)" if rc == 0
                else "📥 강제 재백필 시작",
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
                f"✅ 백필 완료: success={result['success']} fail={result['fail']} rows={result['rows']}",
            )

        # 증분 (오늘 1일치)
        inc = await loop.run_in_executor(None, incremental.update_today)
        if inc.get("empty"):
            msg = "📭 오늘 데이터 없음 (휴장일?)" if inc.get("is_business_day") else "📭 주말 — 스킵"
            await send_text_chunked(bot, chat_id, msg)
            # 휴장일이라도 어제까지의 누적 데이터로 신호 계산을 진행 (사용자 요청 시 의미 있음)
            if not inc.get("is_business_day"):
                return

        # 신호 계산
        await send_text_chunked(bot, chat_id, "🔍 신호 계산 중...")
        results = await loop.run_in_executor(None, signals.compute_all)
        if not results:
            await send_text_chunked(bot, chat_id, "⚠️ 신호 계산 결과 비어있음 — DB 확인")
            return

        # 히스토리 저장
        try:
            today_iso = datetime.now(KST).strftime("%Y-%m-%d")
            await loop.run_in_executor(None, lambda: db.save_signals(today_iso, results))
        except Exception:
            log.exception("signal 히스토리 저장 실패")

        # 발송
        text = formatter.format_results(results, datetime.now(KST))
        await send_text_chunked(bot, chat_id, text)
        log.info("[scheduled] 발송 완료")
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
    """SCREENER_TEST_MODE=1 시 부팅 후 1회 자동 실행."""
    chat_id = (
        os.getenv("SCREENER_TEST_CHAT_ID")
        or os.getenv(CHAT_ID_ENV)
        or (os.getenv(ALLOWED_ENV, "").split(",") + [""])[0].strip()
    )
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


# ------------------------------------------------------------------
# Entry point (orchestrator가 호출)
# ------------------------------------------------------------------
SCREENER_COMMANDS = [
    ("screen", "📈 즉시 스크리닝 실행 (52주/일목/거래량 신호)"),
    ("status", "DB 상태 + 데이터 최신성"),
    ("backfill", "1년치 강제 재백필 (10분+)"),
    ("help", "도움말"),
]


def build_screener_app(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .post_init(make_post_init_set_commands(SCREENER_COMMANDS))
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(CommandHandler("screen", _cmd_screen))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("backfill", _cmd_backfill))

    if os.getenv("SCREENER_TEST_MODE", "0") == "1":
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_self_test(app.bot))
        except RuntimeError:
            pass
    return app
