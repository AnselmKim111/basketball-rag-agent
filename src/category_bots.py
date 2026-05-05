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

import httpx
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from src.bot_helpers import (
    MissingWisereportCreds,
    deny_message,
    is_authorized as _bh_is_authorized,
    safe_dirname as _bh_safe_dirname,
    send_pdf as _bh_send_pdf,
    send_text_chunked as _bh_send_text,
    wisereport_creds,
)
from src.pipeline_lock import PIPELINE_LOCK
from src.state_store import mark_seen, mark_seen_with_titles, normalize_title, seen, seen_titles
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


# 헬퍼는 src.bot_helpers의 public 버전을 thin wrapper로 노출 — 기존 호출부 보존.
_safe_dirname = _bh_safe_dirname
_send_text = _bh_send_text
_send_pdf = _bh_send_pdf


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
    PIPELINE_LOCK으로 직렬화 — 다른 작업 진행 중이면 큐 대기.
    반환: 발송한 리포트 개수.
    """
    # 다른 wisereport 작업이 돌고 있으면 큐 안내
    if PIPELINE_LOCK.locked():
        await _send_text(
            bot,
            chat_id,
            f"⏳ *{label}*: 다른 작업 진행 중 — 끝나면 순차 처리합니다",
        )

    try:
        wr_id, wr_pw = wisereport_creds()
    except MissingWisereportCreds as e:
        log.error("wisereport 자격 증명 누락 (%s): %s", label, e)
        await _send_text(bot, chat_id, f"⚠️ {label}: wisereport 자격 증명 미설정 — 관리자에게 문의")
        return 0

    async with PIPELINE_LOCK:
        loop = asyncio.get_running_loop()

        seen_ids: set[str] = set()
        seen_t_set: set[str] = set()
        if dedup_key:
            seen_ids = await loop.run_in_executor(None, seen, dedup_key)
            seen_t_set = await loop.run_in_executor(None, seen_titles, dedup_key)
            log.info(
                "dedup state [%s]: rpt_ids=%d, titles=%d",
                dedup_key, len(seen_ids), len(seen_t_set),
            )

        # blocking 파이프라인을 thread에서 실행
        def _blocking() -> tuple[list[Path], list[str], list[tuple[str, str]]]:
            from src.summarizer import IndividualSummary  # noqa: F401
            items: list[ReportItem] = []
            with WisereportClient(
                user_id=wr_id,
                password=wr_pw,
                download_root=download_root,
                headless=True,
                ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
                state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
            ) as cli:
                cli.ensure_logged_in()
                # dedup으로 잘릴 가능성 보정 — 넉넉하게 가져온 뒤 필터.
                fetch_limit = limit + len(seen_ids) + 30
                raw = cli.list_top_reports(
                    category=category,  # type: ignore[arg-type]
                    sort_by=sort_by,    # type: ignore[arg-type]
                    limit=fetch_limit,
                    days_back=days_back,
                    industry_gics=industry_gics,
                )
                items = _filter_and_order(
                    raw,
                    seen_ids=seen_ids if dedup_key else set(),
                    seen_titles_norm=seen_t_set if dedup_key else set(),
                    limit=limit,
                )
                if not items:
                    return [], [], []
                if len(raw) != len(items):
                    log.info(
                        "dedup [%s]: %d → %d (rpt_id+title+batch+freshness)",
                        dedup_key or "no-dedup", len(raw), len(items),
                    )

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

            sent_pairs = [(it.rpt_id, it.title) for it in items[: len(saved_paths)]]
            return saved_paths, summaries, sent_pairs

        saved_paths, summaries, sent_pairs = await loop.run_in_executor(None, _blocking)

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

        if dedup_key and sent_pairs:
            await loop.run_in_executor(
                None, mark_seen_with_titles, dedup_key, sent_pairs, 5000,
            )

        return len(saved_paths)


def _filter_and_order(
    raw: list[ReportItem],
    *,
    seen_ids: set[str],
    seen_titles_norm: set[str],
    limit: int,
) -> list[ReportItem]:
    """rpt_id + 정규화 title + 같은 batch 내 dedup → 최신 날짜 우선 재정렬 → top-limit.

    재정렬 규칙: 같은 sch_dt(날짜)끼리 묶고 날짜 내림차순으로 이어붙이기.
    같은 날짜 내부에서는 wisereport 응답 순서(인기순) 그대로 보존 → "오늘 인기순 먼저,
    부족하면 어제, 그 다음 그제..." 효과.
    """
    # 1) dedup
    batch_titles: set[str] = set()
    filtered: list[ReportItem] = []
    for it in raw:
        if it.rpt_id in seen_ids:
            continue
        norm = normalize_title(it.title)
        if norm and norm in seen_titles_norm:
            continue
        if norm and norm in batch_titles:
            continue
        if norm:
            batch_titles.add(norm)
        filtered.append(it)

    # 2) 같은 날짜끼리 묶고 날짜 내림차순 — Python sort는 stable이라
    # 같은 날짜 내부의 인기순(원본 응답 순서)이 보존됨.
    filtered.sort(key=lambda it: (it.sch_dt or ""), reverse=True)
    return filtered[:limit]


# ------------------------------------------------------------------
# 산업봇: 스케줄 작업 + 사용자 핸들러
# ------------------------------------------------------------------
INDUSTRY_HELP = (
    "📊 *산업 리서치 봇*\n\n"
    "*자동 발송:* 매일 오전 9시 (시황봇과 동일 인터벌)\n"
    "  조회수 Top 산업 리포트 10건 (rpt_id+title 중복 제외, 최신 날짜 우선)\n\n"
    "*수동 요청:* 산업명 입력하면 5+5 발송\n"
    "  - 인기순 5건 + 최신순 5건\n"
    "  - 각 5000자 요약\n"
    "  예: `반도체` 또는 `/industry 반도체`\n\n"
    "*명령:*\n"
    "  /start, /help — 도움말\n"
    "  /industry <산업명> — 해당 산업 리포트\n"
    "  /trigger — 자동 발송 작업 즉시 실행 (테스트)\n"
)


_is_authorized = _bh_is_authorized  # 시그니처 동일: (update, env_key) → bool


async def industry_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        await deny_message(update, "산업봇")
        return
    await update.message.reply_text(INDUSTRY_HELP, parse_mode=ParseMode.MARKDOWN)


async def industry_on_demand(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """사용자가 산업명 입력 → 인기 5 + 최신 5 발송."""
    if not _is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        await deny_message(update, "산업봇")
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

    try:
        wr_id, wr_pw = wisereport_creds()
    except MissingWisereportCreds as e:
        log.error("wisereport 자격 증명 누락 (industry lookup): %s", e)
        await update.message.reply_text("⚠️ wisereport 자격 증명 미설정 — 관리자에게 문의")
        return

    def _lookup() -> str | None:
        with WisereportClient(
            user_id=wr_id,
            password=wr_pw,
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
        await deny_message(update, "산업봇")
        return
    await update.message.reply_text("🔄 산업 Top10 작업 수동 실행")
    await industry_top10_job(context.bot)


# 스케줄 잡 — orchestrator의 APScheduler가 호출
async def industry_top10_job(bot: Bot) -> None:
    """매일 09:00 KST: 산업 카테고리 조회수 Top 10 (rpt_id+title 중복 제외, 최신 날짜 우선)."""
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
        await deny_message(update, "시황봇")
        return
    await update.message.reply_text(MARKET_HELP, parse_mode=ParseMode.MARKDOWN)


async def market_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "MARKET_ALLOWED_CHAT_IDS"):
        await deny_message(update, "시황봇")
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
        await deny_message(update, "글로벌봇")
        return
    await update.message.reply_text(GLOBAL_HELP, parse_mode=ParseMode.MARKDOWN)


async def global_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "GLOBAL_ALLOWED_CHAT_IDS"):
        await deny_message(update, "글로벌봇")
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
