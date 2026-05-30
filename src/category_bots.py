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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.bot_helpers import (
    MissingWisereportCreds,
    deny_message,
    is_authorized as _bh_is_authorized,
    safe_dirname as _bh_safe_dirname,
    send_pdf as _bh_send_pdf,
    send_text_chunked as _bh_send_text,
    wisereport_creds,
)
from src.industry_catalog import (
    Candidate as IndustryCandidate,
    lookup_by_name as industry_lookup_by_name,
    resolve_industry,
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


# OpenRouter 동시 호출 상한. 너무 크면 429. 5-8 권장 — wisereport 한 카테고리 5건
# + 인기/최신 합쳐서 10건도 한 번에 안전하게 처리 가능한 보수적 값.
_LLM_SUMMARY_CONCURRENCY = int(os.environ.get("LLM_SUMMARY_CONCURRENCY", "8"))


async def _summarize_pdfs_parallel(
    paths: list[Path], *, short_summary: bool, label: str
) -> list[str]:
    """N개 PDF를 asyncio.gather + semaphore로 동시 요약.

    PIPELINE_LOCK 바깥에서 호출 — 같은 chat의 후속 발송 응답성을 위해.
    각 호출은 to_thread로 워커 스레드에서 sync OpenRouter SDK 사용.
    실패는 자리에 (요약 실패: ...) 문자열로 들어가 발송은 계속.
    """
    if not paths:
        return []
    sum_client = get_client()
    summarize_fn = summarize_pdf_short if short_summary else summarize_pdf
    sem = asyncio.Semaphore(_LLM_SUMMARY_CONCURRENCY)

    async def _one(p: Path) -> str:
        async with sem:
            try:
                s = await asyncio.to_thread(summarize_fn, sum_client, p)
                return s.summary_text
            except OpenRouterCreditExhausted:
                log.warning("[%s] %s 요약 — OpenRouter 한도 초과", label, p.name)
                return "(요약 실패: OpenRouter 토큰 부족)"
            except Exception as e:
                log.exception("[%s] %s 요약 실패", label, p.name)
                return f"(요약 실패: {e!r})"

    return await asyncio.gather(*[_one(p) for p in paths])


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
    """카테고리 리포트 가져와서 (옵션: dedup) 다운로드 → 요약 → 텔레그램 발송.

    동시성 설계
    -----------
    PIPELINE_LOCK은 **wisereport 다운로드 구간에서만** 유지. wisereport는 같은 계정
    동시 로그인 시 세션 무효화되어 직렬화 필수. 하지만 LLM 요약·텔레그램 발송은
    lock 밖에서 진행 — 다운로드 끝나는 즉시 다른 사용자 명령이 wisereport를 잡을
    수 있다.

    LLM 요약은 `_summarize_pdfs_parallel`로 동시 호출 (semaphore 상한). 5-10개
    PDF가 한 번에 요약되어 사용자 체감 응답 속도가 5-8배 빨라진다.

    반환: 발송한 리포트 개수.
    """
    if PIPELINE_LOCK.locked():
        await _send_text(
            bot,
            chat_id,
            f"⏳ *{label}*: wisereport 사용 중 — 다운로드만 잠시 대기 (요약은 즉시 병렬 진행)",
        )

    try:
        wr_id, wr_pw = wisereport_creds()
    except MissingWisereportCreds as e:
        log.error("wisereport 자격 증명 누락 (%s): %s", label, e)
        await _send_text(bot, chat_id, f"⚠️ {label}: wisereport 자격 증명 미설정 — 관리자에게 문의")
        return 0

    loop = asyncio.get_running_loop()

    # ─── STEP A: wisereport 다운로드 (lock 안 — 직렬 필수) ───
    async with PIPELINE_LOCK:
        seen_ids: set[str] = set()
        seen_t_set: set[str] = set()
        if dedup_key:
            seen_ids = await loop.run_in_executor(None, seen, dedup_key)
            seen_t_set = await loop.run_in_executor(None, seen_titles, dedup_key)
            log.info(
                "dedup state [%s]: rpt_ids=%d, titles=%d",
                dedup_key, len(seen_ids), len(seen_t_set),
            )

        def _wisereport_only() -> tuple[list[Path], list[ReportItem]]:
            from src.summarizer import IndividualSummary  # noqa: F401
            with WisereportClient(
                user_id=wr_id,
                password=wr_pw,
                download_root=download_root,
                headless=True,
                ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
                state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
            ) as cli:
                cli.ensure_logged_in()
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
                    return [], []
                if len(raw) != len(items):
                    log.info(
                        "dedup [%s]: %d → %d (rpt_id+title+batch+freshness)",
                        dedup_key or "no-dedup", len(raw), len(items),
                    )
                target_dir = download_root / _safe_dirname(label)
                saved_paths = cli.download_reports(items, target_dir)
                return saved_paths, items[: len(saved_paths)]

        try:
            saved_paths, items_sent = await loop.run_in_executor(None, _wisereport_only)
        except Exception:
            log.exception("[%s] wisereport 다운로드 실패", label)
            await _send_text(
                bot, chat_id,
                f"⚠️ {label}: wisereport 접속 실패 (사이트 응답 지연) — 잠시 후 자동 재시도",
            )
            return 0
    # ─── lock 해제 — 이 시점 다른 사용자가 wisereport 즉시 사용 가능 ───

    if not saved_paths:
        await _send_text(bot, chat_id, f"📭 {label}: 새 리포트 없음")
        return 0

    # ─── STEP B: LLM 요약을 N개 동시 호출 (lock 밖) ───
    summaries = await _summarize_pdfs_parallel(
        saved_paths, short_summary=short_summary, label=label
    )

    # ─── STEP C: 텔레그램 발송 (순서 보존 위해 sequential) ───
    today = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    intro = f"📊 *{label}* ({today})\n총 {len(saved_paths)}건 발송"
    await _send_text(bot, chat_id, intro)
    for i, (p, summary) in enumerate(zip(saved_paths, summaries), start=1):
        header = f"━━━ [{i}/{len(saved_paths)}] {p.stem} ━━━\n\n"
        await _send_text(bot, chat_id, header + summary)
        await _send_pdf(bot, chat_id, p, caption=f"[{i}/{len(saved_paths)}] {p.name}")

    if dedup_key and items_sent:
        sent_pairs = [(it.rpt_id, it.title) for it in items_sent]
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
    "*🎯 추천 — AI 큐레이션 (오늘의 주도산업·학습가치 산업 선별):*\n"
    "  `/curate` — Top 10 똑똑한 산업 리포트 + 각 선별 이유 (5-15분)\n"
    "  `/curate 5` — Top 5\n"
    "  → 다양한 산업 분포 + 깊이 있는 사이클·테마 분석 우선\n\n"
    "*자동 발송:* 매일 오전 9시 — 신규 Top 10 (dedup 적용)\n\n"
    "*수동 요청 (특정 산업):*\n"
    "  `반도체` 또는 `/industry 반도체` — 인기 5건 + 최신 5건, 5000자\n\n"
    "*명령:*\n"
    "  /start, /help — 도움말\n"
    "  /curate [개수] — AI 큐레이션 (위 추천 참고)\n"
    "  /industry <산업명> — 특정 산업 리포트\n"
    "  /trigger — 9시 자동 작업 즉시 실행\n"
)


_is_authorized = _bh_is_authorized  # 시그니처 동일: (update, env_key) → bool


async def industry_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        await deny_message(update, "산업봇")
        return
    await update.message.reply_text(INDUSTRY_HELP, parse_mode=ParseMode.MARKDOWN)


# 인라인 버튼 callback prefix — 8자 이내 권장 (telegram 64바이트 제한)
_INDUSTRY_PICK_PREFIX = "indpick"

# CallbackQuery → 정식 산업명 매핑 (process-local). user_data 보다 단순.
_PENDING_PICKS: dict[str, list[str]] = {}


async def _send_industry_reports(
    bot: Bot, chat_id: str, picked: IndustryCandidate
) -> None:
    """정식 산업명으로 wisereport 코드 룩업 → 인기 5 + 최신 5 발송.

    industry_on_demand와 inline 버튼 callback 양쪽에서 공유.
    """
    loop = asyncio.get_running_loop()
    try:
        wr_id, wr_pw = wisereport_creds()
    except MissingWisereportCreds as e:
        log.error("wisereport 자격 증명 누락 (industry lookup): %s", e)
        await bot.send_message(chat_id=chat_id, text="⚠️ wisereport 자격 증명 미설정 — 관리자에게 문의")
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
            return cli.lookup_industry_code(picked.wisereport_query)

    code = await loop.run_in_executor(None, _lookup)
    if not code:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ wisereport에서 '{picked.name_ko}' (검색어: {picked.wisereport_query}) "
                f"GICS 코드 룩업 실패.\n전체 산업 인기순 5건으로 대체 발송합니다."
            ),
        )
        await _process_and_send_category(
            bot=bot, chat_id=chat_id,
            category="industry", label="산업 (인기 5건)",
            sort_by="popular", limit=5, days_back=30,
            dedup_key=None,
            download_root=Path(os.environ.get("DOWNLOAD_DIR", "./downloads")),
            short_summary=False,
        )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=f"✅ GICS 코드 확정: {code}\n인기·최신 10건을 한 세션에 받고 동시 요약합니다.",
    )

    download_root = Path(os.environ.get("DOWNLOAD_DIR", "./downloads"))
    await _process_industry_popular_and_latest(
        bot=bot, chat_id=chat_id,
        industry_name=picked.name_ko, gics_code=code,
        download_root=download_root, per_bucket=5,
    )


async def _process_industry_popular_and_latest(
    *, bot: Bot, chat_id: str, industry_name: str, gics_code: str,
    download_root: Path, per_bucket: int = 5,
) -> None:
    """한 wisereport 세션 + 한 번의 PIPELINE_LOCK으로 인기·최신을 모두 다운로드,
    LLM 요약 N개를 lock 밖에서 동시에 실행 → 발송.

    기존: lock×2 (인기 → 요약 → 발송 → lock 해제 → lock 재획득 → 최신 …) — 다른
    사용자가 그 사이에 끼어들기 어렵고, 요약도 sequential.
    이후: lock×1 (다운로드만) → gather로 10개 동시 요약 → 두 섹션 순차 발송.
    """
    if PIPELINE_LOCK.locked():
        await _send_text(
            bot, chat_id,
            f"⏳ *{industry_name}*: wisereport 사용 중 — 다운로드만 잠시 대기",
        )

    try:
        wr_id, wr_pw = wisereport_creds()
    except MissingWisereportCreds as e:
        log.error("wisereport 자격 증명 누락 (industry combined): %s", e)
        await _send_text(bot, chat_id, "⚠️ wisereport 자격 증명 미설정")
        return

    loop = asyncio.get_running_loop()

    async with PIPELINE_LOCK:
        def _fetch_both() -> tuple[list[Path], list[Path]]:
            with WisereportClient(
                user_id=wr_id, password=wr_pw,
                download_root=download_root, headless=True,
                ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
                state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
            ) as cli:
                cli.ensure_logged_in()
                pop_dir = download_root / _safe_dirname(f"{industry_name} 인기순 {per_bucket}건")
                lat_dir = download_root / _safe_dirname(f"{industry_name} 최신순 {per_bucket}건")
                pop_raw = cli.list_top_reports(
                    category="industry", sort_by="popular",
                    limit=per_bucket + 5, days_back=60, industry_gics=gics_code,
                )
                lat_raw = cli.list_top_reports(
                    category="industry", sort_by="latest",
                    limit=per_bucket + 5, days_back=60, industry_gics=gics_code,
                )
                # 인기 vs 최신 중복 제거 (rpt_id 기준) — 같은 리포트 두 번 다운로드 방지
                pop_items = _filter_and_order(pop_raw, seen_ids=set(), seen_titles_norm=set(), limit=per_bucket)
                pop_ids = {it.rpt_id for it in pop_items}
                lat_items = _filter_and_order(
                    lat_raw, seen_ids=pop_ids, seen_titles_norm=set(), limit=per_bucket,
                )
                pop_paths_local = cli.download_reports(pop_items, pop_dir) if pop_items else []
                lat_paths_local = cli.download_reports(lat_items, lat_dir) if lat_items else []
                return pop_paths_local, lat_paths_local

        try:
            pop_paths, lat_paths = await loop.run_in_executor(None, _fetch_both)
        except Exception:
            log.exception("[%s] wisereport 인기·최신 동시 fetch 실패", industry_name)
            await _send_text(bot, chat_id, f"⚠️ {industry_name}: wisereport 접속 실패")
            return
    # ─── lock 해제 ───

    if not pop_paths and not lat_paths:
        await _send_text(bot, chat_id, f"📭 {industry_name}: 인기·최신 모두 신규 리포트 없음")
        return

    # 두 묶음을 합쳐 한 번에 동시 요약 (gather)
    all_paths = pop_paths + lat_paths
    all_summaries = await _summarize_pdfs_parallel(
        all_paths, short_summary=False, label=industry_name,
    )
    pop_summaries = all_summaries[: len(pop_paths)]
    lat_summaries = all_summaries[len(pop_paths) :]

    today = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    if pop_paths:
        await _send_text(bot, chat_id, f"📊 *{industry_name} 인기순 {len(pop_paths)}건* ({today})")
        for i, (p, s) in enumerate(zip(pop_paths, pop_summaries), start=1):
            await _send_text(bot, chat_id, f"━━━ [{i}/{len(pop_paths)}] {p.stem} ━━━\n\n{s}")
            await _send_pdf(bot, chat_id, p, caption=f"[{i}/{len(pop_paths)}] {p.name}")
    if lat_paths:
        await _send_text(bot, chat_id, f"🆕 *{industry_name} 최신순 {len(lat_paths)}건* ({today})")
        for i, (p, s) in enumerate(zip(lat_paths, lat_summaries), start=1):
            await _send_text(bot, chat_id, f"━━━ [{i}/{len(lat_paths)}] {p.stem} ━━━\n\n{s}")
            await _send_pdf(bot, chat_id, p, caption=f"[{i}/{len(lat_paths)}] {p.name}")


async def industry_on_demand(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """사용자가 산업명 입력 → (정규화) → 인기 5 + 최신 5 발송."""
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

    # 1단계: 산업명 정규화 (사전 + sonnet 하이브리드)
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, resolve_industry, industry_name)
    except OpenRouterCreditExhausted:
        await update.message.reply_text(
            "❌ OpenRouter 한도 초과 — 산업명 해석 불가. 키 충전 후 재시도."
        )
        return
    except Exception:
        log.exception("resolve_industry 예외")
        await update.message.reply_text("❌ 산업명 해석 중 오류 — 잠시 후 재시도")
        return

    if result.failed:
        await update.message.reply_text(
            f"❌ '{industry_name}'을(를) 알아듣지 못했습니다.\n"
            "예: `반도체 소재·부품·장비`, `2차전지·배터리`, `방산·우주·항공`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # 단일 결정 — confirm 한 줄 + 즉시 진행
    if result.is_single and result.best is not None:
        picked = result.best
        await update.message.reply_text(
            f"🎯 해석: {industry_name} → *{picked.name_ko}* (conf={picked.confidence:.2f})\n"
            f"⏱️ 약 8-15분 소요",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _send_industry_reports(bot, chat_id, picked)
        return

    # 신뢰도 낮음 — 후보 3개 인라인 버튼
    names = [c.name_ko for c in result.candidates[:3]]
    if not names:
        await update.message.reply_text(
            f"❌ '{industry_name}' 매핑 후보가 없습니다. 더 명확하게 입력해 주세요."
        )
        return
    _PENDING_PICKS[chat_id] = names
    buttons = [
        [InlineKeyboardButton(f"{i+1}. {n}", callback_data=f"{_INDUSTRY_PICK_PREFIX}|{i}")]
        for i, n in enumerate(names)
    ]
    await update.message.reply_text(
        f"🤔 '{industry_name}' 해석이 애매합니다. 어느 산업인가요?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def industry_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """인라인 버튼 콜백 — 사용자가 후보 산업 1개를 선택했을 때."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    chat_id = str(query.message.chat.id) if query.message else None
    if not chat_id:
        return
    if not _bh_is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        await query.edit_message_text("권한 없음")
        return
    data = query.data or ""
    parts = data.split("|", 1)
    if len(parts) != 2 or parts[0] != _INDUSTRY_PICK_PREFIX:
        return
    try:
        idx = int(parts[1])
    except ValueError:
        return
    names = _PENDING_PICKS.get(chat_id) or []
    if not (0 <= idx < len(names)):
        await query.edit_message_text("⏰ 선택 만료 — 다시 입력해 주세요.")
        return
    picked_name = names[idx]
    picked = industry_lookup_by_name(picked_name)
    if picked is None:
        await query.edit_message_text(f"❌ '{picked_name}' 카탈로그에서 못 찾음")
        return
    _PENDING_PICKS.pop(chat_id, None)
    await query.edit_message_text(f"✅ 선택: *{picked_name}*\n⏱️ 약 8-15분 소요", parse_mode=ParseMode.MARKDOWN)
    await _send_industry_reports(context.bot, chat_id, picked)


async def industry_trigger(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """수동으로 스케줄 작업 즉시 실행 (테스트용)."""
    if not _is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        await deny_message(update, "산업봇")
        return
    await update.message.reply_text("🔄 산업 Top10 작업 수동 실행")
    await industry_top10_job(context.bot)


async def industry_curated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """산업 도메인 AI 큐레이션 — 주도산업·학습가치 산업 선별 + Top N 리포트.

    `/curate [N]`. INDUSTRY_MODE 사용 (industry 카테고리만, 다양한 산업 분포 우선).
    """
    if not _is_authorized(update, "INDUSTRY_ALLOWED_CHAT_IDS"):
        await deny_message(update, "산업봇")
        return
    text = (update.message.text or "").strip()
    n = _parse_recent_count(context.args or [], text, default=10, cap=15)
    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)
    try:
        from src.curator import INDUSTRY_MODE, run_curated
    except Exception:
        log.exception("curator 모듈 로드 실패 (industry)")
        await update.message.reply_text("❌ 큐레이션 모듈 로드 실패")
        return
    await run_curated(bot, chat_id, n=n, mode=INDUSTRY_MODE)


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
    "*🎯 추천 — AI 큐레이션 (시황 분석 → 주도주·주도산업 선별):*\n"
    "  `/curate` — Top 10 똑똑한 선별 + 각 선별 이유 (5-15분)\n"
    "  `/curate 5` — Top 5만\n"
    "  → 단순 인기/최신을 넘어 오늘 시황·학습가치까지 반영\n\n"
    "*자동 발송:* 매일 오전 9시\n"
    "  조회수 Top 투자전략/시황 리포트 중 *신규*(미발송)만\n\n"
    "*단순 인기 (큐레이션 없이):*\n"
    "  `/recent` — 인기 5건 (dedup 무시)\n"
    "  `/recent 10` — 인기 10건\n"
    "  자유 텍스트 `5` 또는 `시황` — `/recent` 와 동일\n\n"
    "*명령:*\n"
    "  /start, /help — 도움말\n"
    "  /trigger — 9시 자동 작업 즉시 실행 (신규만, dedup 적용)\n"
    "  /recent [개수] — 단순 인기순\n"
    "  /curate [개수] — AI 큐레이션 (위 설명 참고)\n"
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


# ------------------------------------------------------------------
# on-demand: 시황 인기 N건 즉시 (dedup 없이) — `/recent` 또는 자유 텍스트
# ------------------------------------------------------------------
def _parse_recent_count(args: list[str], text: str, default: int = 5, cap: int = 15) -> int:
    """슬래시 명령 args 또는 자유 텍스트에서 N 추출. 없으면 default. cap 제한.

    우선순위: args[0] → 자유 텍스트 첫 단어. 둘 다 int 변환 실패하면 default.
    """
    raw = ""
    if args:
        raw = args[0]
    elif text:
        parts = text.strip().split()
        if parts:
            raw = parts[0]
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return default
    return max(1, min(cap, n))


async def market_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """원할 때 즉시 인기 N건 발송. dedup 무시 (이미 본 것 포함). 단순 인기순."""
    if not _is_authorized(update, "MARKET_ALLOWED_CHAT_IDS"):
        await deny_message(update, "시황봇")
        return
    text = (update.message.text or "").strip()
    n = _parse_recent_count(context.args or [], text, default=5, cap=15)
    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text(f"🔄 시황 인기 {n}건 검색 중... (5000자 요약)")
    await _process_and_send_category(
        bot=bot, chat_id=chat_id,
        category="strategy",
        label=f"투자전략/시황 인기 {n}건",
        sort_by="popular", limit=n, days_back=14,
        dedup_key=None,  # ★ on-demand — dedup 안 함
        download_root=Path(os.environ.get("DOWNLOAD_DIR", "./downloads")),
        short_summary=False,  # 5000자
    )


async def market_curated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """AI 큐레이션 — 오늘 시황 분석 → 주도산업·주도주 → Top N 선별 + 이유.

    3단계 LLM 파이프라인 (curator 모듈). 단순 인기/최신 sort보다 비용 ↑ 가치 ↑.
    """
    if not _is_authorized(update, "MARKET_ALLOWED_CHAT_IDS"):
        await deny_message(update, "시황봇")
        return
    text = (update.message.text or "").strip()
    n = _parse_recent_count(context.args or [], text, default=10, cap=15)
    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)
    try:
        from src.curator import MARKET_MODE, run_curated
    except Exception:
        log.exception("curator 모듈 로드 실패")
        await update.message.reply_text("❌ 큐레이션 모듈 로드 실패 — /recent 폴백 사용")
        return
    await run_curated(bot, chat_id, n=n, mode=MARKET_MODE)


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
    "*자동 발송:* 매주 토요일 오전 9시 (신규 Top10)\n\n"
    "*on-demand (원할 때 즉시):*\n"
    "  `/recent` — 인기 5건 다시 발송 (dedup 무시, 5000자)\n"
    "  `/recent 10` — 인기 10건\n"
    "  자유 텍스트 `5` 또는 `글로벌` — `/recent` 와 동일\n\n"
    "*명령:*\n"
    "  /start, /help — 도움말\n"
    "  /trigger — 토요일 자동 작업 즉시 실행 (신규만, dedup 적용)\n"
    "  /recent [개수] — 위 설명 참고\n"
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


async def global_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """원할 때 즉시 글로벌 인기 N건 발송. dedup 무시."""
    if not _is_authorized(update, "GLOBAL_ALLOWED_CHAT_IDS"):
        await deny_message(update, "글로벌봇")
        return
    text = (update.message.text or "").strip()
    n = _parse_recent_count(context.args or [], text, default=5, cap=15)
    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text(f"🔄 글로벌 인기 {n}건 검색 중... (5000자 요약)")
    await _process_and_send_category(
        bot=bot, chat_id=chat_id,
        category="global",
        label=f"글로벌 인기 {n}건",
        sort_by="popular", limit=n, days_back=14,
        dedup_key=None,  # ★ on-demand — dedup 안 함
        download_root=Path(os.environ.get("DOWNLOAD_DIR", "./downloads")),
        short_summary=False,
    )


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
INDUSTRY_COMMANDS = [
    ("curate", "🎯 AI 큐레이션 — 주도산업·학습가치 산업 Top N"),
    ("industry", "특정 산업 리포트 (예: /industry 반도체)"),
    ("trigger", "9시 자동 작업 즉시 실행 (신규만)"),
    ("help", "도움말"),
]

MARKET_COMMANDS = [
    ("curate", "🎯 AI 큐레이션 — 주도주·주도산업 종합 Top N"),
    ("recent", "단순 인기 N건 (dedup 무시, 예: /recent 10)"),
    ("trigger", "9시 자동 작업 즉시 실행 (신규만)"),
    ("help", "도움말"),
]

GLOBAL_COMMANDS = [
    ("recent", "글로벌 인기 N건 (예: /recent 10)"),
    ("trigger", "토요일 자동 Top10 즉시 실행"),
    ("help", "도움말"),
]


def build_industry_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], industry_help))
    app.add_handler(CommandHandler("industry", industry_on_demand))
    app.add_handler(CommandHandler("trigger", industry_trigger))
    app.add_handler(CommandHandler("curate", industry_curated))
    app.add_handler(
        CallbackQueryHandler(industry_pick_callback, pattern=f"^{_INDUSTRY_PICK_PREFIX}\\|")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, industry_on_demand))
    return app


def build_market_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], market_help))
    app.add_handler(CommandHandler("trigger", market_trigger))
    app.add_handler(CommandHandler("recent", market_recent))
    app.add_handler(CommandHandler("curate", market_curated))
    # 자유 텍스트 입력 — "/recent" 와 동일하게 처리 (숫자 → N건, 그 외 → 5건 기본)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, market_recent))
    return app


def build_global_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], global_help))
    app.add_handler(CommandHandler("trigger", global_trigger))
    app.add_handler(CommandHandler("recent", global_recent))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_recent))
    return app
