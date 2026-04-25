"""산업봇 / 시황봇 / 글로벌봇 — 조회수 Top 카테고리 리포트 자동 발송.

각 봇은 다음 둘 중 하나 이상을 수행:
  - 스케줄 작업 (orchestrator의 APScheduler가 트리거)
  - 사용자 메시지 핸들러 (산업봇만 — 산업명 받아 인기/최신 5+5)

bot 모듈 1개로 통합. orchestrator가 각 봇 token으로 Application 인스턴스를 만들고
이 모듈의 핸들러/잡을 연결.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic  # noqa: F401  (downstream consistency check)
import httpx
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from src.state_store import mark_seen, seen
from src.summarizer import (
    OpenRouterCreditExhausted,
    get_client,
    summarize_pdf,
    summarize_pdf_short,
)
from src.telegram_sender import TELEGRAM_API
from src.wisereport import ReportItem, WisereportClient

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _safe_dirname(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name)
    return name.strip().strip(".") or "category"


# ------------------------------------------------------------------
# 공통: 봇이 직접 PDF/텍스트 발송 (telegram_sender의 동기 httpx 대신
# 비동기 PTB Bot 사용 — orchestrator 이벤트 루프 친화적)
# ------------------------------------------------------------------
async def _send_text(bot: Bot, chat_id: str, text: str) -> None:
    while text:
        chunk, text = text[:4000], text[4000:]
        try:
            await bot.send_message(chat_id=chat_id, text=chunk)
        except Exception:
            log.exception("send_message 실패")


async def _send_pdf(bot: Bot, chat_id: str, path: Path, caption: str = "") -> None:
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > 49:
        log.warning("파일 너무 큼 (%.1fMB) 스킵: %s", size_mb, path.name)
        return
    try:
        with path.open("rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=path.name,
                caption=caption[:1024],
            )
    except Exception:
        log.exception("send_document 실패: %s", path.name)


# ------------------------------------------------------------------
# 공통: 카테고리 → 다운로드 + 짧은 요약 + 발송
# ------------------------------------------------------------------
async def _process_and_send_category(
    bot: Bot,
    chat_id: str,
    category: str,
    label: str,
    sort_by: str,
    limit: int,
    days_back: int,
    dedup_key: str | None,
    download_root: Path,
    short_summary: bool = True,
    industry_gics: str | None = None,
) -> int:
    """카테고리 리포트 가져와서 (옵션: dedup) 다운로드 → 짧은 요약 → 텔레그램 발송.

    blocking I/O (Playwright + OpenRouter) 는 thread executor에서 실행.
    반환: 발송한 리포트 개수.
    """
    loop = asyncio.get_running_loop()

    seen_ids: set[str] = set()
    if dedup_key:
        seen_ids = await loop.run_in_executor(None, seen, dedup_key)

    # blocking 파이프라인을 thread에서 실행
    def _blocking() -> tuple[list[Path], list[str], list[str]]:
        from src.summarizer import IndividualSummary  # noqa: F401
        items: list[ReportItem] = []
        with WisereportClient(
            user_id=os.environ["WISEREPORT_ID"],
            password=os.environ["WISEREPORT_PW"],
            download_root=download_root,
            headless=True,
            ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
            state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
        ) as cli:
            cli.ensure_logged_in()
            items = cli.list_top_reports(
                category=category,  # type: ignore[arg-type]
                sort_by=sort_by,    # type: ignore[arg-type]
                limit=limit + len(seen_ids),  # dedup으로 잘릴 가능성 보정
                days_back=days_back,
                industry_gics=industry_gics,
            )
            # dedup
            if dedup_key:
                items = [it for it in items if it.rpt_id not in seen_ids][:limit]
            else:
                items = items[:limit]

            if not items:
                return [], [], []

            target_dir = download_root / _safe_dirname(label)
            saved_paths = cli.download_reports(items, target_dir)

        # OpenRouter 요약
        summaries: list[str] = []
        sum_client = get_client()
        for p in saved_paths:
            try:
                if short_summary:
                    s = summarize_pdf_short(sum_client, p)
                else:
                    s = summarize_pdf(sum_client, p)
                summaries.append(s.summary_text)
            except OpenRouterCreditExhausted:
                summaries.append("(요약 실패: OpenRouter 토큰 부족)")
            except Exception as e:
                summaries.append(f"(요약 실패: {e!r})")

        return saved_paths, summaries, [it.rpt_id for it in items[: len(saved_paths)]]

    saved_paths, summaries, sent_rpt_ids = await loop.run_in_executor(None, _blocking)

    if not saved_paths:
        await _send_text(bot, chat_id, f"📭 {label}: 새 리포트 없음")
        return 0

    # 인트로
    today = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    intro = f"📊 *{label}* ({today})\n총 {len(saved_paths)}건 발송"
    await _send_text(bot, chat_id, intro)

    # 각 (요약 + PDF) 한 쌍씩 발송
    for i, (p, summary) in enumerate(zip(saved_paths, summaries), start=1):
        header = f"━━━ [{i}/{len(saved_paths)}] {p.stem} ━━━\n\n"
        await _send_text(bot, chat_id, header + summary)
        await _send_pdf(bot, chat_id, p, caption=f"[{i}/{len(saved_paths)}] {p.name}")

    if dedup_key and sent_rpt_ids:
        await loop.run_in_executor(None, mark_seen, dedup_key, sent_rpt_ids)

    return len(saved_paths)


# ------------------------------------------------------------------
# 산업봇: 스케줄 작업 + 사용자 핸들러
# ------------------------------------------------------------------
INDUSTRY_HELP = (
    "📊 *산업 리서치 봇*\n\n"
    "*자동 발송:* 매주 월/수/금 오전 9시\n"
    "  조회수 Top 산업 리포트 10건 (중복 제외)\n\n"
    "*수동 요청:* 산업명 입력하면 5+5 발송\n"
    "  - 인기순 5건 + 최신순 5건\n"
    "  - 각 5000자 요약\n"
    "  예: `반도체` 또는 `/industry 반도체`\n\n"
    "*명령:*\n"
    "  /start, /help — 도움말\n"
    "  /industry <산업명> — 해당 산업 리포트\n"
    "  /trigger — 자동 발송 작업 즉시 실행 (테스트)\n"
)


def _allowed_chat_ids(env_key: str) -> set[str]:
    raw = os.getenv(env_key, "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _is_authorized(update: Update, env_key: str) -> bool:
    if not update.effective_chat:
        return False
    return str(update.effective_chat.id) in _allowed_chat_ids(env_key)


async def industry_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        await update.message.reply_text(
            f"이 봇은 인가된 사용자만 사용 가능합니다.\nchat_id: `{update.effective_chat.id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await update.message.reply_text(INDUSTRY_HELP, parse_mode=ParseMode.MARKDOWN)


async def industry_on_demand(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """사용자가 산업명 입력 → 인기 5 + 최신 5 발송."""
    if not _is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        return
    industry_name = " ".join(context.args) if context.args else (
        (update.message.text or "").strip()
    )
    industry_name = industry_name.lstrip("/").strip()
    # /industry 같은 명령어 자체는 빈 입력으로 인식
    if industry_name.startswith("industry "):
        industry_name = industry_name[len("industry "):].strip()
    if not industry_name or len(industry_name) > 30:
        await update.message.reply_text(
            "산업명을 입력해주세요. 예: `반도체` 또는 `/industry 반도체`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text(
        f"🔎 *{industry_name}* 산업 리포트 검색 중...\n⏱️ 약 8-15분 소요",
        parse_mode=ParseMode.MARKDOWN,
    )

    # 산업명 → gicscode (best effort)
    loop = asyncio.get_running_loop()

    def _lookup() -> str | None:
        with WisereportClient(
            user_id=os.environ["WISEREPORT_ID"],
            password=os.environ["WISEREPORT_PW"],
            download_root=Path("/tmp"),
            headless=True,
            ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
            state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
        ) as cli:
            cli.ensure_logged_in()
            return cli.lookup_industry_code(industry_name)

    code = await loop.run_in_executor(None, _lookup)
    if not code:
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ 산업 코드 룩업 실패: {industry_name}\n전체 산업 인기순 5건 발송으로 대체합니다.",
        )
        # 폴백: 전체 산업 인기순 5건
        await _process_and_send_category(
            bot=bot, chat_id=chat_id,
            category="industry", label=f"산업 (인기 5건)",
            sort_by="popular", limit=5, days_back=30,
            dedup_key=None,
            download_root=Path(os.environ.get("DOWNLOAD_DIR", "./downloads")),
            short_summary=False,  # 5000자
        )
        return

    download_root = Path(os.environ.get("DOWNLOAD_DIR", "./downloads"))

    # 인기 5건
    await _process_and_send_category(
        bot=bot, chat_id=chat_id,
        category="industry",
        label=f"{industry_name} 인기순 5건",
        sort_by="popular", limit=5, days_back=60,
        dedup_key=None,
        download_root=download_root,
        short_summary=False,
        industry_gics=code,
    )
    # 최신 5건
    await _process_and_send_category(
        bot=bot, chat_id=chat_id,
        category="industry",
        label=f"{industry_name} 최신순 5건",
        sort_by="latest", limit=5, days_back=60,
        dedup_key=None,
        download_root=download_root,
        short_summary=False,
        industry_gics=code,
    )


async def industry_trigger(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """수동으로 스케줄 작업 즉시 실행 (테스트용)."""
    if not _is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        return
    await update.message.reply_text("🔄 산업 Top10 작업 수동 실행")
    await industry_top10_job(context.bot)


# 스케줄 잡 — orchestrator의 APScheduler가 호출
async def industry_top10_job(bot: Bot) -> None:
    """월/수/금 09:00 KST: 산업 카테고리 조회수 Top 10 (중복 제외)."""
    log.info("[scheduled] industry_top10_job 시작")
    chat_id = os.environ.get("INDUSTRY_CHAT_ID")
    if not chat_id:
        log.error("INDUSTRY_CHAT_ID 미설정")
        return
    await _process_and_send_category(
        bot=bot, chat_id=chat_id,
        category="industry",
        label="산업 조회수 Top10",
        sort_by="popular", limit=10, days_back=14,
        dedup_key="industry_top10",
        download_root=Path(os.environ.get("DOWNLOAD_DIR", "./downloads")),
        short_summary=True,
    )
    log.info("[scheduled] industry_top10_job 완료")


# ------------------------------------------------------------------
# 시황봇: 매일 09:00 시황 신규 체크
# ------------------------------------------------------------------
MARKET_HELP = (
    "📊 *시황 리서치 봇*\n\n"
    "*자동 발송:* 매일 오전 9시\n"
    "  조회수 Top 투자전략/시황 리포트 중 신규(미발송) 모두\n"
    "  각 1000자 요약 + PDF\n\n"
    "*명령:*\n"
    "  /start, /help — 도움말\n"
    "  /trigger — 자동 발송 작업 즉시 실행\n"
)


async def market_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "MARKET_ALLOWED_CHAT_IDS"):
        await update.message.reply_text(
            f"인가된 사용자만 사용 가능. chat_id: `{update.effective_chat.id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await update.message.reply_text(MARKET_HELP, parse_mode=ParseMode.MARKDOWN)


async def market_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "MARKET_ALLOWED_CHAT_IDS"):
        return
    await update.message.reply_text("🔄 시황 작업 수동 실행")
    await market_daily_job(context.bot)


async def market_daily_job(bot: Bot) -> None:
    """매일 09:00 KST: 투자전략/시황 신규 리포트만 발송."""
    log.info("[scheduled] market_daily_job 시작")
    chat_id = os.environ.get("MARKET_CHAT_ID")
    if not chat_id:
        log.error("MARKET_CHAT_ID 미설정")
        return
    # 시황은 신규 발견되는 만큼 모두 (최대 10건 안전 cap), 1000자 요약
    await _process_and_send_category(
        bot=bot, chat_id=chat_id,
        category="strategy",
        label="투자전략/시황 신규",
        sort_by="popular", limit=10, days_back=3,
        dedup_key="strategy_daily",
        download_root=Path(os.environ.get("DOWNLOAD_DIR", "./downloads")),
        short_summary=True,
    )
    log.info("[scheduled] market_daily_job 완료")


# ------------------------------------------------------------------
# 글로벌봇: 매주 토 09:00 글로벌 Top 10
# ------------------------------------------------------------------
GLOBAL_HELP = (
    "📊 *글로벌 리서치 봇*\n\n"
    "*자동 발송:* 매주 토요일 오전 9시\n"
    "  조회수 Top 글로벌 리포트 10건\n"
    "  각 1000자 요약 + PDF\n\n"
    "*명령:*\n"
    "  /start, /help — 도움말\n"
    "  /trigger — 자동 발송 작업 즉시 실행\n"
)


async def global_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "GLOBAL_ALLOWED_CHAT_IDS"):
        await update.message.reply_text(
            f"인가된 사용자만 사용 가능. chat_id: `{update.effective_chat.id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await update.message.reply_text(GLOBAL_HELP, parse_mode=ParseMode.MARKDOWN)


async def global_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "GLOBAL_ALLOWED_CHAT_IDS"):
        return
    await update.message.reply_text("🔄 글로벌 작업 수동 실행")
    await global_top10_job(context.bot)


async def global_top10_job(bot: Bot) -> None:
    log.info("[scheduled] global_top10_job 시작")
    chat_id = os.environ.get("GLOBAL_CHAT_ID")
    if not chat_id:
        log.error("GLOBAL_CHAT_ID 미설정")
        return
    await _process_and_send_category(
        bot=bot, chat_id=chat_id,
        category="global",
        label="글로벌 조회수 Top10",
        sort_by="popular", limit=10, days_back=14,
        dedup_key="global_top10",
        download_root=Path(os.environ.get("DOWNLOAD_DIR", "./downloads")),
        short_summary=True,
    )
    log.info("[scheduled] global_top10_job 완료")


# ------------------------------------------------------------------
# Application 빌더 — orchestrator가 호출
# ------------------------------------------------------------------
def build_industry_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], industry_help))
    app.add_handler(CommandHandler("industry", industry_on_demand))
    app.add_handler(CommandHandler("trigger", industry_trigger))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, industry_on_demand))
    return app


def build_market_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], market_help))
    app.add_handler(CommandHandler("trigger", market_trigger))
    return app


def build_global_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], global_help))
    app.add_handler(CommandHandler("trigger", global_trigger))
    return app
