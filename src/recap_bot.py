"""RecapBot — 주간 회고 봇 (자동 복기·학습 자산).

설계 (plan: /root/.claude/plans/hidden-wobbling-hellman.md)
- 매주 일요일 19:00 KST cron → 4섹션 (signal/ideas/disclosures/themes) 수집 → sonnet 통합
- /recap 수동 명령으로 즉시 실행 가능
- 모든 LLM 호출은 src.summarizer.chat_with_retry (재시도 + 폴백)
- wisereport 접근(curator)은 PIPELINE_LOCK 안 (다른 봇과 충돌 방지)

CLAUDE.md §2 모델 티어 준수: aggregator·scorer LLM 없음, 합성만 sonnet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.bot_helpers import (
    KST,
    deny_message,
    is_authorized,
    send_pdf,
    send_text_chunked,
)
from src.pipeline_lock import PIPELINE_LOCK

log = logging.getLogger(__name__)

ALLOWED_ENV = "RECAP_ALLOWED_CHAT_IDS"
SYNTHESIS_MODEL_ENV = "IDEA_SYNTHESIS_MODEL"
NARROW_MODEL_ENV = "IDEA_NARROW_MODEL"
# sonnet 합성 출력 안전 상한 — 프롬프트는 800-1200자 가이드, 4x 마진.
MAX_SYNTHESIS_CHARS = 4000


HELP_TEXT = (
    "📋 *RecapBot — 주간 회고*\n\n"
    "*기본:*\n"
    "  `/recap` — 지난 7일 회고 즉시 발송 (신호 PnL + 큐레이션 후속 + 테마 사이클 + sonnet 합성)\n"
    "  `/recap_global` — 글로벌 회고만 (사용자 watchlist 무관)\n"
    "  `/recap_me` — 본인 watchlist 종목만 추가 (DisclosureBot watchlist 재사용)\n\n"
    "*자동:* 매주 일요일 19:00 KST cron 발송\n\n"
    "*섹션:*\n"
    "  🎯 신호 사후 추적 (screener.db, D+5 PnL)\n"
    "  💡 IdeaBot picks 후속\n"
    "  📋 공시 트리거 (DisclosureBot 알림 이력 + 공시 후 수익률)\n"
    "  📊 테마 사이클 (wisereport 시황 리포트 키워드)\n"
    "  🧠 학습 포인트 (sonnet 통합)\n"
    "\n_⚠️ wisereport 접근 = PIPELINE_LOCK 잠시 점유 (1-2분)_"
)


# ------------------------------------------------------------------
# 핸들러
# ------------------------------------------------------------------
async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "회고봇")
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def _cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "회고봇")
        return
    chat_id = str(update.effective_chat.id)
    asyncio.create_task(_run_recap(context.bot, chat_id, scope="full"))


async def _cmd_recap_global(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "회고봇")
        return
    chat_id = str(update.effective_chat.id)
    asyncio.create_task(_run_recap(context.bot, chat_id, scope="global"))


async def _cmd_recap_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "회고봇")
        return
    chat_id = str(update.effective_chat.id)
    asyncio.create_task(_run_recap(context.bot, chat_id, scope="me"))


# ------------------------------------------------------------------
# 핵심 파이프라인
# ------------------------------------------------------------------
async def _run_recap(bot: Bot, chat_id: str, scope: str = "full") -> None:
    """4섹션 수집 → sonnet 합성 → 텍스트 + 차트 2장 발송.

    scope:
      full   — 글로벌 + 본인 watchlist 동시 (현재는 watchlist 단순 표시)
      global — 본인 watchlist 무시
      me     — watchlist 종목 hit만 표시 (필터)
    """
    from src.recap import aggregator, formatter

    today = datetime.now(KST).date()
    started_ack = await bot.send_message(
        chat_id=int(chat_id),
        text=(
            f"📋 *주간 회고 시작* — {today.isoformat()} 기준 최근 7일\n"
            f"_4섹션 수집 → sonnet 합성 → 약 1-3분 소요_"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    # ─── scope 결정: "me"면 본인 watchlist 종목으로 §1·§3 한정 ───
    ticker_filter: set[str] | None = None
    disc_chat_filter: str | None = None
    if scope == "me":
        try:
            from src import watchlist_store
            items = watchlist_store.list_for_chat(chat_id)
            ticker_filter = {it["ticker"] for it in items if it.get("ticker")}
        except Exception:
            log.exception("[recap] watchlist 로드 실패 — 전체 scope로 진행")
            ticker_filter = None
        if not ticker_filter:
            await send_text_chunked(
                bot, chat_id,
                "📭 watchlist 비어있음 — 공시봇에서 `/watch <종목>` 등록 후 사용. "
                "이번에는 전체(global) 회고로 진행합니다.",
            )
            ticker_filter = None
        else:
            disc_chat_filter = chat_id
            await send_text_chunked(
                bot, chat_id,
                f"👤 watchlist {len(ticker_filter)}종목 한정 회고",
            )

    # ─── §1 §2 §3는 LLM 없이 즉시 수집 ───
    try:
        # build_recap_input 안에서 themes(§4)는 wisereport 호출 — 그것만 lock 안.
        sig_section = aggregator.collect_signal_section(
            today, lookback_days=7, ticker_filter=ticker_filter,
        )
        ideas_section = aggregator.collect_ideas_section(days_back=7)
        disc_section = aggregator.collect_disclosure_section(
            today=today, days_back=7, chat_id=disc_chat_filter,
        )
    except Exception:
        log.exception("[recap] §1-§3 수집 실패")
        await send_text_chunked(bot, chat_id, "❌ 수집 단계 실패 — 로그 확인")
        return

    # ─── §4 wisereport (themes) — PIPELINE_LOCK 안에서 ───
    if PIPELINE_LOCK.locked():
        await send_text_chunked(
            bot, chat_id, "⏳ wisereport 사용 중 — 테마 분석 잠시 대기"
        )
    theme_section: dict
    try:
        async with PIPELINE_LOCK:
            theme_section = await asyncio.get_running_loop().run_in_executor(
                None, aggregator.collect_theme_section, 14,
            )
    except Exception:
        log.exception("[recap] §4 테마 수집 실패 — 빈 dict로 계속")
        theme_section = {"recent": {}, "previous": {}, "rising": [], "fading": [], "skipped": True}

    data = {
        "today": today.isoformat(),
        "lookback_days": 7,
        "signal": sig_section,
        "ideas": ideas_section,
        "disclosures": disc_section,
        "themes": theme_section,
        "scope": scope,
    }

    log.info(
        "[recap] aggregator collected: signals=%d, ideas=%d, themes_rising=%d",
        sig_section.get("summary", {}).get("total_hits", 0),
        ideas_section.get("entry_count", 0),
        len(theme_section.get("rising") or []),
    )

    # ─── §5 sonnet 합성 ───
    synthesis = ""
    try:
        synthesis = await _synthesize_recap(data)
    except Exception:
        log.exception("[recap] sonnet 합성 실패 — raw 4섹션만 발송")

    # ─── 텍스트 발송 ───
    text = formatter.format_recap_text(data, synthesis_md=synthesis)
    await send_text_chunked(bot, chat_id, text, parse_mode="Markdown")

    # ─── 차트 2장 ───
    try:
        sig_png = formatter.render_signal_bar_chart(sig_section.get("stats") or [])
        if sig_png:
            await bot.send_photo(
                chat_id=int(chat_id), photo=sig_png,
                caption=f"🎯 신호별 평균 PnL (D+{sig_section.get('horizon_days', 5)})",
            )
    except Exception:
        log.exception("[recap] signal chart 발송 실패")

    try:
        theme_png = formatter.render_theme_chart(theme_section)
        if theme_png:
            await bot.send_photo(
                chat_id=int(chat_id), photo=theme_png,
                caption="📊 테마 사이클 (최근 7일 - 이전 7일)",
            )
    except Exception:
        log.exception("[recap] theme chart 발송 실패")

    await send_text_chunked(bot, chat_id, "✅ 주간 회고 완료")
    log.info("[recap] send_results 완료 chat_id=%s", chat_id)


async def _synthesize_recap(data: dict) -> str:
    """sonnet 합성 1회 (kimi/haiku 폴백). 실패 시 빈 string → 호출자가 raw만 발송."""
    loop = asyncio.get_running_loop()

    def _blocking() -> str:
        from src import idea_prompts, summarizer
        try:
            client = summarizer.get_client()
        except Exception:
            log.exception("[recap] OpenRouter client init 실패")
            return ""
        try:
            sys_prompt = idea_prompts.load("recap_synthesis")
        except Exception:
            log.exception("[recap] prompt load 실패")
            sys_prompt = ""
        # signal stats는 dataclass → dict 변환
        signal_dump = {
            "summary": data["signal"].get("summary", {}),
            "stats": [
                {
                    "signal": s.signal, "hit_count": s.hit_count,
                    "avg_pnl": s.avg_pnl, "win_rate": s.win_rate,
                    "top": [{"name": h.name or h.ticker, "pnl_pct": h.pnl_pct} for h in s.top],
                    "bottom": [{"name": h.name or h.ticker, "pnl_pct": h.pnl_pct} for h in s.bottom],
                }
                for s in (data["signal"].get("stats") or [])
            ],
        }
        payload = {
            "today": data["today"],
            "lookback_days": data["lookback_days"],
            "signal": signal_dump,
            "ideas": data["ideas"],
            "disclosures": data["disclosures"],
            "themes": data["themes"],
        }
        user_msg = (
            "다음 4섹션 회고 데이터를 시스템 프롬프트 형식대로 합성:\n\n"
            + json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)[:15_000]
        )
        try:
            content = summarizer.chat_with_retry(
                client,
                model=_synthesis_model(),
                fallback_model=_narrow_model(),
                max_tokens=2500,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                context="recap_synthesis",
            )
        except summarizer.OpenRouterCreditExhausted:
            log.warning("[recap] OpenRouter 한도 초과 — 합성 스킵")
            return ""
        except Exception:
            log.exception("[recap] sonnet 호출 실패")
            return ""
        log.info("[recap] sonnet synthesis done, len=%d", len(content or ""))
        content = (content or "").strip()
        # 프롬프트 가이드 800-1200자, 4x 안전 마진 — 모델이 폭주해도 텔레그램 메시지
        # 분할(4000 char) 안에서 한 청크로 끝나도록.
        if len(content) > MAX_SYNTHESIS_CHARS:
            log.warning(
                "[recap] sonnet 출력 %d자 → %d자 truncate",
                len(content), MAX_SYNTHESIS_CHARS,
            )
            content = content[:MAX_SYNTHESIS_CHARS] + "\n…(truncated)"
        return content

    return await loop.run_in_executor(None, _blocking)


from src.llm_models import synthesis_model as _make_synthesis, narrow_model as _make_narrow


def _synthesis_model() -> str:
    return _make_synthesis(SYNTHESIS_MODEL_ENV)


def _narrow_model() -> str:
    return _make_narrow(NARROW_MODEL_ENV)


# ------------------------------------------------------------------
# Cron (orchestrator가 등록)
# ------------------------------------------------------------------
async def recap_weekly_job(bot: Bot, override_chat_id: str | None = None) -> None:
    """매주 일요일 19:00 KST cron. RECAP_CHAT_ID 또는 override로 발송."""
    chat_id = override_chat_id or os.environ.get("RECAP_CHAT_ID")
    if not chat_id:
        log.warning("RECAP_CHAT_ID 미설정 — cron 스킵")
        return
    await _run_recap(bot, chat_id, scope="full")


# ------------------------------------------------------------------
# Self-test
# ------------------------------------------------------------------
async def _self_test(bot: Bot) -> None:
    chat_id = os.environ.get("RECAP_CHAT_ID")
    if not chat_id:
        log.warning("RECAP_TEST_PROMPT 있으나 RECAP_CHAT_ID 없음 — 스킵")
        return
    await asyncio.sleep(15)  # 다른 봇 폴링 안정화 대기 (CLAUDE.md §4)
    try:
        await _run_recap(bot, chat_id, scope="full")
    except Exception:
        log.exception("[recap self-test] 최상위 예외")


# ------------------------------------------------------------------
# Application 빌더
# ------------------------------------------------------------------
RECAP_COMMANDS = [
    ("recap", "📋 지난 주 회고 즉시 (4섹션 + sonnet 합성)"),
    ("recap_global", "🌐 글로벌 회고만"),
    ("recap_me", "👤 본인 watchlist 한정"),
    ("help", "도움말"),
]


def build_recap_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(CommandHandler("recap", _cmd_recap))
    app.add_handler(CommandHandler("recap_global", _cmd_recap_global))
    app.add_handler(CommandHandler("recap_me", _cmd_recap_me))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _help))

    # RECAP_TEST_PROMPT는 '플래그' 패턴 — 값 내용은 안 보고 설정 여부만 체크
    # (다른 봇의 SCREENER_TEST_MODE / IDEA_TEST_PROMPT와 동일 컨벤션).
    if os.getenv("RECAP_TEST_PROMPT"):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_self_test(app.bot))
        except RuntimeError:
            # orchestrator가 build를 사전 호출 — 그땐 loop 없음. polling 시작 후 별도 트리거.
            pass
    return app
