"""IdeaBot — 투자 아이디어 → 영업레버리지 Top 5 종목 추천.

사용 흐름:
  사용자 입력(텍스트 or /idea <text>)
    → (0.5) parse: thesis + 제약 추출 (cheap model)
    → (1) research: 웹검색 + 가치사슬 분해 + 30 후보 (perplexity)
    → (1.5) importance: 현상 중요도 비판 평가 (premium)
    → (2) wisereport 산업 리포트 수집 (PIPELINE_LOCK)
    → (3) narrow: 30 → 10 + all30_scored 4축 점수 (mid model — haiku)
        → 30종목 4축 산점도 PNG 발송
    → (4) wisereport 종목 리포트 수집 (PIPELINE_LOCK)
    → (5) synthesis: Top 5 + 영업레버리지 thesis (premium)
    → (6) 텔레그램 발송: 텍스트 + Top 5 참고 PDF + 공통 산업 PDF

# 모델 티어
  - **summary tier** (OPENROUTER_MODEL, kimi 등) — 갓성비. 단순 추출·요약.
    · 0.5 parse / PDF 요약 / DART 잠정실적 / forward 컨센서스 / deepdive 요약
  - **research tier** (IDEA_RESEARCH_MODEL, perplexity/sonar-pro)
    · 1단계 웹검색
  - **narrow tier** (IDEA_NARROW_MODEL, haiku 등) — mid. 큰 출력 + 점수화.
    · 3단계 30 → 10
  - **synthesis tier** (IDEA_SYNTHESIS_MODEL, sonnet/opus) — 진짜 지능 필요.
    · 1.5단계 importance / 5단계 Top 5 thesis

격리 원칙 (BOTS.md):
  - 모든 wisereport 호출은 async with PIPELINE_LOCK 안.
  - 외부 호출은 try/except로 감싸 봇 프로세스를 죽이지 않음.
  - 신규 모듈 — category_bots.py / bot_worker.py 수정 안 함.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src import idea_prompts
from src.bot_helpers import (
    deny_message,
    download_root_for,
    is_authorized,
    safe_dirname,
    send_pdf,
    send_text_chunked,
)
from src.pipeline_lock import PIPELINE_LOCK

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 환경변수 키
ALLOWED_ENV = "IDEA_ALLOWED_CHAT_IDS"
RESEARCH_MODEL_ENV = "IDEA_RESEARCH_MODEL"   # 1단계 웹검색 (perplexity 등)
NARROW_MODEL_ENV = "IDEA_NARROW_MODEL"       # 3단계 30→10 (지능 덜 필요, 기본 OPENROUTER_MODEL)
SYNTHESIS_MODEL_ENV = "IDEA_SYNTHESIS_MODEL" # 1.5/5단계 깊은 추론 (지능 필요, claude-sonnet 등)

DEFAULT_RESEARCH_MODEL = "perplexity/sonar-pro"

# 파이프라인 파라미터
INDUSTRY_REPORTS_PER_INDUSTRY = 3
COMPANY_REPORTS_PER_TICKER = 2
INDUSTRY_TEXT_MAX = 12_000   # PDF 텍스트 추출 시 자르는 문자 수
COMPANY_TEXT_MAX = 10_000
NARROW_TARGET = 10           # 30 → 10
TOP_PICK_COUNT = 5

# idea_bot 자체의 작업 상태 (다른 봇과 별도)
CURRENT_IDEA: dict | None = None


HELP_TEXT = (
    "💡 *IdeaBot — 투자 아이디어 → 영업레버리지 Top 5*\n\n"
    "*기본 사용:*\n"
    "  - 슬래시 없이 아이디어를 그냥 보내거나\n"
    "  - `/idea <아이디어 텍스트>`\n\n"
    "*제약 표현 지원* (자연어 그대로):\n"
    "  - `1조 이하 소부장 중 AI 데이터센터 수혜`\n"
    "  - `중소형 K-방산`\n"
    "  - `코스닥만, 5천억 이하 반도체 장비`\n\n"
    "*Tinkering 명령:*\n"
    "  `/history` — 최근 20개 idea 분석 목록\n"
    "  `/show <id>` — 과거 결과 다시 보기 (id 끝 6자리만 OK)\n"
    "  `/dive <rank> [<id>]` — Top N 종목 deepdive 자동 실행\n"
    "    예: `/dive 1` (가장 최근 idea의 Top 1)\n"
    "  `/refine <id> <추가 제약>` — cached 데이터 재사용해서 빠르게 변형\n"
    "    예: `/refine 143005 시총 1조 이하만 specialty pure-play`\n"
    "  `/contrarian <id>` — thesis가 깨질 때 가장 취약한 RISK Top 5\n"
    "    예: `/contrarian 143005`\n"
    "  `/compare <id1> <id2>` — 두 idea의 Top 10 교집합·차별 분석\n"
    "    예: `/compare 143005 162018`\n\n"
    "*동작:*\n"
    "  🧭 0.5단계: 아이디어 파싱 (thesis + 시총/산업 제약 추출)\n"
    "  🌐 1단계: 웹 검색 + 30 후보 발굴 (제약 적용)\n"
    "  ⚖️ 1.5단계: 현상 중요도 비판적 평가\n"
    "  📊 2단계: 산업 리포트 다운로드\n"
    "  🎯 3단계: 영업레버리지 4축 점수로 30→10 + 산점도\n"
    "  📈 4단계: Top10 종목 리포트 다운로드\n"
    "  🧠 5단계: 사업부→폭→기울기 단계적 사고로 Top 5\n"
    "  🏆 6단계: Top 5 thesis + Top 1 분기차트 + 참고 PDF\n\n"
    "_⏱️ /idea 약 15-25분 / /refine 약 2-4분_\n"
    "_프롬프트 수정: GitHub `prompts/idea_*.txt` 편집 후 push_"
)


# ------------------------------------------------------------------
# 핸들러
# ------------------------------------------------------------------
async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "아이디어봇")
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def _cmd_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "아이디어봇")
        return
    args = " ".join(context.args or []).strip()
    if not args:
        await update.message.reply_text(
            "사용법: `/idea <아이디어 텍스트>` 또는 슬래시 없이 그냥 입력",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    asyncio.create_task(_run_pipeline(update, context, args))


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "아이디어봇")
        return
    text = (update.message.text or "").strip()
    if not text or len(text) > 500:
        await update.message.reply_text(
            "아이디어를 1-500자 이내로 입력해주세요. /help 참고.",
        )
        return
    asyncio.create_task(_run_pipeline(update, context, text))


async def _cmd_dive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dive <rank> [<id>] — 가장 최근 idea의 Top 5 중 N번째 종목으로 deepdive 자동 실행.

    예:
      /dive 1     → 가장 최근 idea의 Top 1
      /dive 3     → 가장 최근 idea의 Top 3
      /dive 2 20260430-143005 → 특정 entry id의 Top 2
    """
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "아이디어봇")
        return
    from src import idea_cache
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/dive <rank>` (예: `/dive 1`) — 가장 최근 idea의 Top N 종목 deepdive\n"
            "또는: `/dive <rank> <id>` — 특정 entry의 Top N",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        rank = int(args[0])
    except (ValueError, TypeError):
        await send_text_chunked(bot, chat_id, "❌ rank는 1-5 사이 정수여야 합니다.")
        return
    if not (1 <= rank <= 5):
        await send_text_chunked(bot, chat_id, "❌ rank는 1-5 사이.")
        return

    # 특정 id 지정 또는 latest
    if len(args) >= 2:
        record = idea_cache.find_by_partial_id(args[1])
        if not record:
            await send_text_chunked(bot, chat_id, f"❓ id='{args[1]}' 매칭 entry 없음")
            return
    else:
        record = idea_cache.latest()
        if not record:
            await send_text_chunked(bot, chat_id, "📭 캐시된 idea 없음 — 먼저 idea 분석부터")
            return

    top5 = (record.get("synthesis") or {}).get("top5") or []
    pick = next((p for p in top5 if int(p.get("rank", 0) or 0) == rank), None)
    if not pick:
        # rank 매칭 안 되면 인덱스로 fallback (일부 LLM이 rank 누락)
        if rank - 1 < len(top5):
            pick = top5[rank - 1]
    if not pick:
        await send_text_chunked(bot, chat_id, f"❓ Top {rank} 종목 없음 (top5 길이 {len(top5)})")
        return

    name = pick.get("name", "?")
    ticker = (pick.get("ticker6") or "").strip()
    if not re.match(r"^\d{6}$", ticker):
        await send_text_chunked(
            bot, chat_id,
            f"❌ '{name}' ticker 없음/유효 X — deepdive 불가 (비상장 가능성)",
        )
        return

    await send_text_chunked(
        bot, chat_id,
        f"🔍 *Deepdive 자동 체이닝*\n"
        f"Idea: `{(record.get('idea_text') or '')[:80]}`\n"
        f"Top {rank}: *{name}* ({ticker})\n"
        f"⏱️ 5-10분 소요 — DART 사업보고서 + IR + 분기차트 통합 분석",
        parse_mode=ParseMode.MARKDOWN,
    )

    # deepdive _run 호출 — update/context 그대로 전달 (chat_id, bot 사용)
    try:
        from src.deepdive.handler import _run as deepdive_run
    except Exception:
        log.exception("deepdive 모듈 import 실패")
        await send_text_chunked(bot, chat_id, "❌ deepdive 모듈 로드 실패 (DART_API_KEY 미설정 가능성)")
        return
    asyncio.create_task(deepdive_run(update, context, ticker))


async def _cmd_refine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/refine <id> <추가 제약/변경> — cached research·industry 재사용 + narrow·synthesis 재실행.

    cached 데이터 재사용으로 ~3분 (full pipeline 15분 대비). 같은 idea에
    다른 제약을 빠르게 시도하기 위함.

    예: /refine 20260430-143005 시총 1조 이하만
        /refine 143005 코스닥 + 시총 5천억 이하
    """
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "아이디어봇")
        return
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "사용법: `/refine <id> <추가 제약>`\n"
            "예: `/refine 143005 시총 1조 이하만 specialty pure-play`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    asyncio.create_task(_run_refine(update, context, args[0], " ".join(args[1:])))


async def _run_refine(
    update: Update, context: ContextTypes.DEFAULT_TYPE, entry_id: str, refinement: str,
) -> None:
    """cached entry 기반 narrow + synthesis 재실행. PIPELINE_LOCK 잡지 않음 (wisereport 호출 없음)."""
    bot = context.bot
    chat_id = str(update.effective_chat.id)

    from src import idea_cache
    record = idea_cache.find_by_partial_id(entry_id)
    if not record:
        await send_text_chunked(bot, chat_id, f"❓ id='{entry_id}' 매칭 entry 없음")
        return

    original_idea = record.get("idea_text", "")
    refined_idea = f"{original_idea}\n\n[추가 제약] {refinement}"
    await send_text_chunked(
        bot, chat_id,
        f"🔧 *Refine* (cached `{record.get('id','?')}`)\n"
        f"📌 원본: {original_idea[:100]}\n"
        f"➕ 추가: {refinement}\n"
        f"⏱️ 약 2-4분 — research/산업 캐시 재사용, narrow+synthesis만 재실행",
        parse_mode=ParseMode.MARKDOWN,
    )

    # cached 데이터 추출
    cached_research = record.get("research") or {}
    cached_industry_texts = record.get("industry_texts") or {}
    cached_industry_pdfs = [Path(p) for p in (record.get("industry_pdfs") or []) if Path(p).exists()]
    cached_company_pdfs_by_ticker = {
        k: [Path(p) for p in v if Path(p).exists()]
        for k, v in (record.get("company_pdfs_by_ticker") or {}).items()
    }
    cached_company_texts_by_ticker = record.get("company_texts_by_ticker") or {}
    cached_top10 = record.get("narrow", {}).get("top10") or record.get("top10") or []
    candidates = cached_research.get("candidates") or []

    if not candidates:
        await send_text_chunked(bot, chat_id, "❌ cached candidates 없음 — refine 불가")
        return

    # parse: refined idea (제약 다시 추출)
    parsed = await _parse_idea(refined_idea)
    if parsed:
        await _send_parse_summary(bot, chat_id, parsed)

    # importance: 보존 또는 빠르게 재평가 (시간 절약 위해 보존)
    importance = record.get("synthesis", {}).get("importance") or {}

    # narrow 재실행 — refined idea + parsed constraints + cached industry texts + candidates
    await send_text_chunked(bot, chat_id, "🎯 3단계: narrow 재실행 (cached industry 재사용)")
    narrow = await _narrow_candidates(
        refined_idea,
        cached_research if not parsed else {**cached_research, "_refined_constraints": (parsed or {}).get("constraints", {})},
        cached_industry_texts,
        candidates,
    )
    if not narrow or not narrow.get("top10"):
        log.warning("refine narrow 실패 — 원본 top10 재사용")
        narrow = {"narrowing_summary": "(refine narrow 실패 — 원본 top10 폴백)", "top10": cached_top10}
    new_top10 = narrow["top10"]
    await _send_narrow_summary(bot, chat_id, narrow)

    # 산점도 (refined narrow가 all30_scored 줬으면 재발송)
    all30_scored = (narrow or {}).get("all30_scored") or []
    if all30_scored:
        await _send_scatter_chart(bot, chat_id, refined_idea, all30_scored)

    # ticker 보강 (cached top10에는 이미 검증된 ticker — refine top10은 재검증)
    new_top10 = await _fix_tickers(new_top10)
    new_top10 = [c for c in new_top10 if c.get("ticker6")]

    # synthesis 재실행 — cached company_texts 재사용 (있는 종목)
    await send_text_chunked(bot, chat_id, "🧠 5단계: synthesis 재실행 (cached 종목 리포트 재사용)")
    synthesis = await _synthesize_top5(
        refined_idea, cached_research, importance,
        cached_industry_pdfs, cached_industry_texts,
        new_top10,
        cached_company_pdfs_by_ticker,
        cached_company_texts_by_ticker,
    )
    if not synthesis or not synthesis.get("top5"):
        await send_text_chunked(bot, chat_id, "❌ refine synthesis 실패 — 종료")
        return

    await _send_results(
        bot, chat_id, synthesis,
        cached_industry_pdfs,
        cached_company_pdfs_by_ticker,
        cached_company_texts_by_ticker,
    )

    # 새 cache entry 저장
    try:
        new_id = idea_cache.save(
            idea_text=refined_idea,
            parsed=parsed,
            research=cached_research,
            industry_pdfs=cached_industry_pdfs,
            industry_texts=cached_industry_texts,
            narrow=narrow,
            top10=new_top10,
            company_pdfs_by_ticker=cached_company_pdfs_by_ticker,
            company_texts_by_ticker=cached_company_texts_by_ticker,
            synthesis=synthesis,
            download_root=Path(record.get("download_root") or ""),
        )
    except Exception:
        log.exception("refine cache 저장 실패")
        new_id = ""
    suffix = f" · id={new_id}" if new_id else ""
    await send_text_chunked(bot, chat_id, f"✅ Refine 완료{suffix}")


async def _cmd_contrarian(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/contrarian <id> — cached idea의 thesis가 깨질 때 가장 취약한 RISK Top 5.

    cache 재사용: research/industry/company 데이터 그대로, synthesis 프롬프트만 contrarian.
    """
    if not is_authorized(update, ALLOWED_ENV):
        return
    from src import idea_cache
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/contrarian <id>` — cached idea의 RISK Top 5 (반대 시각).\n"
            "예: `/contrarian 143005`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    record = idea_cache.find_by_partial_id(args[0])
    if not record:
        await send_text_chunked(bot, chat_id, f"❓ id='{args[0]}' 매칭 entry 없음")
        return
    asyncio.create_task(_run_contrarian(update, context, record))


async def _run_contrarian(
    update: Update, context: ContextTypes.DEFAULT_TYPE, record: dict,
) -> None:
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    idea_text = record.get("idea_text", "")

    await send_text_chunked(
        bot, chat_id,
        f"⚠️ *Contrarian 분석* (cached `{record.get('id','?')}`)\n"
        f"📌 Thesis: {idea_text[:100]}\n"
        f"⏱️ 약 1-2분 — synthesis 프롬프트만 contrarian으로 swap, cache 재사용",
        parse_mode=ParseMode.MARKDOWN,
    )

    # cached 데이터
    cached_research = record.get("research") or {}
    cached_industry_texts = record.get("industry_texts") or {}
    cached_industry_pdfs = [Path(p) for p in (record.get("industry_pdfs") or []) if Path(p).exists()]
    cached_top10 = record.get("narrow", {}).get("top10") or record.get("top10") or []
    cached_company_texts_by_ticker = record.get("company_texts_by_ticker") or {}
    cached_company_pdfs_by_ticker = {
        k: [Path(p) for p in v if Path(p).exists()]
        for k, v in (record.get("company_pdfs_by_ticker") or {}).items()
    }
    importance = record.get("synthesis", {}).get("importance") or {}

    # contrarian synthesis — _synthesize_top5와 동일 흐름이지만 다른 system prompt
    loop = asyncio.get_running_loop()

    def _blocking() -> dict | None:
        from src import summarizer
        try:
            client = summarizer.get_client()
        except Exception:
            log.exception("OpenRouter client init 실패 (contrarian)")
            return None
        sys_prompt = idea_prompts.load("idea_contrarian")

        ind_block = "\n\n".join(
            f"=== 산업 리포트 파일: {fn} ===\n{txt}"
            for fn, txt in cached_industry_texts.items()
        ) or "(산업 리포트 없음)"

        company_blocks: list[str] = []
        for c in cached_top10:
            ticker = (c.get("ticker6") or "")
            name = c.get("name", "")
            texts = cached_company_texts_by_ticker.get(ticker, {})
            if not texts:
                company_blocks.append(
                    f"=== {name} ({ticker}) — 종목 리포트 없음 ===\n산업/리서치만으로 평가."
                )
                continue
            for fn, txt in texts.items():
                company_blocks.append(f"=== {name} ({ticker}) 리포트: {fn} ===\n{txt}")
        company_block = "\n\n".join(company_blocks) or "(종목 리포트 없음)"

        user_msg = (
            f"# 사용자 투자 thesis (반대 시각으로 평가)\n{idea_text}\n\n"
            f"# 1단계 리서치 요약\n"
            f"{json.dumps(cached_research.get('logic_gradient_text',''), ensure_ascii=False)[:3000]}\n\n"
            f"# 1.5 importance 평가\n{json.dumps(importance, ensure_ascii=False)[:1500]}\n\n"
            f"# top10 후보\n{json.dumps(cached_top10, ensure_ascii=False, indent=2)[:8000]}\n\n"
            f"# 산업 리포트 텍스트\n{ind_block[:50_000]}\n\n"
            f"# 종목 리포트 텍스트\n{company_block[:60_000]}\n\n"
            "thesis가 깨질 시나리오와 그때 가장 취약한 Top 5를 시스템 프롬프트 형식대로 JSON으로 출력해주세요."
        )
        content = ""
        for attempt in (1, 2):
            try:
                resp = client.chat.completions.create(
                    model=_synthesis_model(),  # contrarian도 지능 필요 — synthesis tier
                    max_tokens=14000,
                    temperature=0.4 if attempt == 1 else 0.6,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                )
            except summarizer.APIStatusError as e:
                if summarizer._is_credit_error(e):
                    raise summarizer.OpenRouterCreditExhausted(str(e)) from e
                log.exception("contrarian LLM API 에러 (attempt %d)", attempt)
                return None
            except Exception as e:
                _maybe_raise_credit(e)
                log.exception("contrarian LLM 호출 실패 (attempt %d)", attempt)
                if attempt == 2:
                    return None
                continue
            try:
                content = (resp.choices[0].message.content or "").strip()
            except Exception:
                if attempt == 2:
                    return None
                continue
            if content:
                break
            log.warning("contrarian 빈 응답 (attempt %d)", attempt)
        if not content:
            return None
        log.info("contrarian LLM 응답: %d chars", len(content))
        parsed_resp = _parse_json(content)
        if parsed_resp is None:
            log.warning("contrarian JSON 파싱 실패")
        return parsed_resp

    try:
        contrarian = await loop.run_in_executor(None, _blocking)
    except Exception:
        log.exception("contrarian 호출 실패")
        contrarian = None

    if not contrarian or not contrarian.get("top5"):
        await send_text_chunked(bot, chat_id, "❌ contrarian 분석 실패")
        return

    # 발송 — _send_results 재사용 (구조 같음). 헤더만 RISK 표시.
    methodology = contrarian.get("ranking_methodology", "")
    top5 = contrarian.get("top5") or []
    intro = (
        "⚠️ Contrarian RISK Top 5 — thesis 깨질 때 가장 취약한 종목\n\n"
        f"📌 원 thesis: {idea_text[:100]}\n\n"
        f"분석 근거:\n{methodology}\n"
    )
    await send_text_chunked(bot, chat_id, intro)

    sent_pdf_names: set[str] = set()
    for pick in top5:
        rank = pick.get("rank", "?")
        name = pick.get("name", "?")
        ticker = pick.get("ticker6", "??????")
        industry = pick.get("industry", "")
        thesis = pick.get("operating_leverage_thesis", "(thesis 없음)")
        kn = pick.get("key_numbers") or {}
        refs = pick.get("referenced_reports") or []
        # 가격 정보
        try:
            from src import price_fetcher
            quote = price_fetcher.fetch_quote(ticker) if re.match(r"^\d{6}$", ticker) else None
            price_brief = price_fetcher.format_quote_brief(quote)
        except Exception:
            price_brief = ""

        own_pdfs = cached_company_pdfs_by_ticker.get(ticker, [])
        own_pdf_names = {p.name for p in own_pdfs}
        valid_refs = [r for r in refs if (r or "").strip() in own_pdf_names]

        header = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🚨 RISK {rank}. {name} ({ticker}) — {industry}\n"
        )
        if price_brief:
            header += f"   💰 {price_brief}\n"
        header += "━━━━━━━━━━━━━━━━━━━━"
        await send_text_chunked(bot, chat_id, header)

        body = thesis
        if kn:
            body += "\n\n📊 Key Numbers\n" + "\n".join(f"  • {k}: {v}" for k, v in kn.items())
        if valid_refs:
            body += "\n📎 참고 리포트: " + ", ".join(valid_refs)
        await send_text_chunked(bot, chat_id, body)

        for p in own_pdfs:
            if p.name in sent_pdf_names:
                continue
            await send_pdf(bot, chat_id, p, caption=f"[RISK {rank} {name}] {p.name}")
            sent_pdf_names.add(p.name)

    log.info(
        "[contrarian 완료] top5=%d개 — %s",
        len(top5),
        " / ".join(f"#{p.get('rank','?')} {p.get('name','?')}" for p in top5),
    )


async def _cmd_compare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/compare <id1> <id2> — 두 idea의 Top 10 종목 교집합 + 차별 분석.

    같은 종목이 두 thesis 모두에 등장 = conviction 강한 신호.
    각 idea에만 등장 = 차별화 메커니즘.
    """
    if not is_authorized(update, ALLOWED_ENV):
        return
    from src import idea_cache
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "사용법: `/compare <id1> <id2>` (id 끝 6자리만 입력해도 OK)\n"
            "예: `/compare 143005 162018`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    rec1 = idea_cache.find_by_partial_id(args[0])
    rec2 = idea_cache.find_by_partial_id(args[1])
    if not rec1 or not rec2:
        missing = [a for a, r in zip(args[:2], [rec1, rec2]) if not r]
        await send_text_chunked(bot, chat_id, f"❓ 매칭 안 되는 id: {missing}")
        return

    def _picks(rec: dict, key: str) -> list[dict]:
        if key == "top5":
            return (rec.get("synthesis") or {}).get("top5") or []
        return rec.get("narrow", {}).get("top10") or rec.get("top10") or []

    top10_1 = _picks(rec1, "top10")
    top10_2 = _picks(rec2, "top10")
    top5_1 = _picks(rec1, "top5")
    top5_2 = _picks(rec2, "top5")

    def _name_set(picks: list[dict]) -> set[tuple[str, str]]:
        return {((p.get("name") or "").strip(), (p.get("ticker6") or "").strip()) for p in picks if p.get("name")}

    s10_1, s10_2 = _name_set(top10_1), _name_set(top10_2)
    s5_1, s5_2 = _name_set(top5_1), _name_set(top5_2)

    common10 = sorted(s10_1 & s10_2, key=lambda x: x[0])
    common5 = sorted(s5_1 & s5_2, key=lambda x: x[0])
    only1_10 = sorted(s10_1 - s10_2, key=lambda x: x[0])[:8]
    only2_10 = sorted(s10_2 - s10_1, key=lambda x: x[0])[:8]

    idea1_text = (rec1.get("idea_text") or "")[:80]
    idea2_text = (rec2.get("idea_text") or "")[:80]

    msg = (
        f"🔀 *두 아이디어 교차 분석*\n\n"
        f"📌 A `{rec1.get('id','?')}`: {idea1_text}\n"
        f"📌 B `{rec2.get('id','?')}`: {idea2_text}\n\n"
        f"━━━ ⭐ Top 5 양쪽 모두 ({len(common5)}개) — 가장 강한 conviction ━━━\n"
    )
    if common5:
        for name, ticker in common5:
            msg += f"  ⭐ {name} ({ticker})\n"
    else:
        msg += "  (양쪽 Top 5 교집합 없음)\n"
    msg += f"\n━━━ ✓ Top 10 양쪽 모두 ({len(common10)}개) ━━━\n"
    if common10:
        for name, ticker in common10:
            msg += f"  ✓ {name} ({ticker})\n"
    else:
        msg += "  (Top 10 교집합 없음)\n"
    msg += f"\n━━━ A에만 ({len(only1_10)}개) ━━━\n"
    for name, ticker in only1_10:
        msg += f"  🅰 {name} ({ticker})\n"
    msg += f"\n━━━ B에만 ({len(only2_10)}개) ━━━\n"
    for name, ticker in only2_10:
        msg += f"  🅱 {name} ({ticker})\n"
    await send_text_chunked(bot, chat_id, msg)

    # 짧은 LLM 해석 — 공통/차별 종목 메커니즘 매핑 (옵션, 1회 호출)
    if not (common10 or only1_10 or only2_10):
        return
    loop = asyncio.get_running_loop()

    def _blocking() -> str:
        from src import summarizer
        try:
            client = summarizer.get_client()
        except Exception:
            return ""
        sys_prompt = (
            "두 한국 주식 투자 아이디어의 후보 종목 비교 분석. "
            "공통 등장 종목 = 강한 conviction (왜 두 thesis가 같은 종목으로 수렴?). "
            "각 idea만의 종목 = 차별화 메커니즘. "
            "한국어 600자 이내, 압축적으로."
        )
        user_msg = (
            f"# Idea A\n{idea1_text}\n\n"
            f"# Idea B\n{idea2_text}\n\n"
            f"# 공통 Top 5: {[n for n,_ in common5]}\n"
            f"# 공통 Top 10: {[n for n,_ in common10]}\n"
            f"# A only: {[n for n,_ in only1_10]}\n"
            f"# B only: {[n for n,_ in only2_10]}\n\n"
            "공통 종목이 두 thesis로 수렴하는 메커니즘 + 각 idea만의 차별 종목 분석을 600자 이내."
        )
        try:
            content = summarizer.chat_with_retry(
                client,
                model=_summary_model(),
                fallback_model=_narrow_model(),
                max_tokens=1200,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                context="idea_compare",
            )
        except Exception:
            log.exception("compare LLM 호출 실패")
            return ""
        return content or ""

    interp = await loop.run_in_executor(None, _blocking)
    if interp:
        await send_text_chunked(bot, chat_id, f"🧠 *교차 분석 해석*\n\n{interp}")


async def _cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/history — 최근 20개 아이디어 분석 목록."""
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "아이디어봇")
        return
    from src import idea_cache
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    try:
        entries = idea_cache.list_recent(limit=20)
    except Exception:
        log.exception("history 로드 실패")
        await send_text_chunked(bot, chat_id, "❌ history 로드 실패")
        return
    if not entries:
        await send_text_chunked(bot, chat_id, "📭 캐시된 아이디어 없음 — 먼저 아이디어 하나 시도해보세요")
        return
    lines = ["📜 최근 아이디어 분석 (최신 → 오래된 순)\n"]
    for e in entries:
        ts = (e.get("created_at") or "")[:16].replace("T", " ")
        idea_short = e.get("idea_text", "")[:60].replace("\n", " ")
        top5 = ", ".join(e.get("top5_brief", [])[:5]) or "(top5 없음)"
        lines.append(f"• `{e['id']}` [{ts}]\n  💡 {idea_short}\n  🏆 {top5}\n")
    lines.append("\n사용법:\n  `/show <id>` — 과거 결과 다시 보기 (id 끝 6자리만 입력해도 OK)")
    await send_text_chunked(bot, chat_id, "\n".join(lines))


async def _cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/show <id> — 과거 idea 결과 텍스트 재발송. PDF는 첨부 안 함 (path만 안내)."""
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "아이디어봇")
        return
    from src import idea_cache
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    args = " ".join(context.args or []).strip()
    if not args:
        await update.message.reply_text(
            "사용법: `/show <id>` (예: `/show 20260430-143005` 또는 `/show 143005`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    record = idea_cache.find_by_partial_id(args)
    if not record:
        await send_text_chunked(bot, chat_id, f"❓ id='{args}' 매칭 entry 없음. /history 로 목록 확인.")
        return

    idea_text = record.get("idea_text", "")
    created_at = (record.get("created_at") or "")[:16].replace("T", " ")
    parsed = record.get("parsed") or {}
    research = record.get("research") or {}
    importance = record.get("synthesis", {}) or {}  # 후속 호환을 위해 비워둠 (실제 importance는 별도 저장 안 됨)
    synthesis = record.get("synthesis") or {}
    top5 = synthesis.get("top5") or []

    # 헤더
    msg = f"📌 [캐시 재발송] `{record.get('id','?')}` ({created_at})\n💡 {idea_text}\n"
    if parsed.get("constraints_summary"):
        msg += f"\n🎯 {parsed['constraints_summary']}\n"
    await send_text_chunked(bot, chat_id, msg)

    # methodology + Top 5 thesis (간략)
    methodology = synthesis.get("ranking_methodology", "")
    if methodology:
        await send_text_chunked(bot, chat_id, f"🏆 영업레버리지 Top 5\n\n랭킹 근거:\n{methodology}\n")
    for pick in top5:
        rank = pick.get("rank", "?")
        name = pick.get("name", "?")
        ticker = pick.get("ticker6", "??????")
        industry = pick.get("industry", "")
        thesis = pick.get("operating_leverage_thesis", "(thesis 없음)")
        body = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🥇 Top {rank}. {name} ({ticker}) — {industry}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n{thesis}"
        )
        kn = pick.get("key_numbers") or {}
        if kn:
            body += "\n\n📊 Key Numbers\n" + "\n".join(f"  • {k}: {v}" for k, v in kn.items())
        await send_text_chunked(bot, chat_id, body)

    # 원본 PDF는 다시 안 보냄 (이미 전송됨). 경로만 안내.
    pdf_paths = record.get("industry_pdfs", []) + [
        p for paths in (record.get("company_pdfs_by_ticker") or {}).values() for p in paths
    ]
    if pdf_paths:
        await send_text_chunked(
            bot, chat_id,
            f"📎 원본 PDF {len(pdf_paths)}건은 처음 발송 시 이미 전달됨 (경로 기록만 보존).",
        )


# ------------------------------------------------------------------
# 메인 파이프라인
# ------------------------------------------------------------------
async def _run_pipeline(
    update: Update, context: ContextTypes.DEFAULT_TYPE, idea_text: str,
) -> None:
    """전체 7단계 파이프라인. 각 단계 try/except — 한 단계 실패 시 가능하면 폴백."""
    global CURRENT_IDEA
    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)

    if PIPELINE_LOCK.locked():
        await send_text_chunked(
            bot, chat_id,
            f"⏳ 다른 작업 진행 중 — 끝나면 순차 처리합니다\n💡 {idea_text[:80]}",
        )

    started = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    await send_text_chunked(
        bot, chat_id,
        f"💡 아이디어: {idea_text}\n{started} 시작 — 약 15-25분 소요",
    )

    download_root = download_root_for("idea") / safe_dirname(idea_text[:30] + "_" + datetime.now(KST).strftime("%H%M"))
    download_root.mkdir(parents=True, exist_ok=True)

    CURRENT_IDEA = {"idea": idea_text, "started_at": started}
    try:
        # ---- (0.5) 아이디어 파싱: thesis + constraints 분리 (cheap 모델, graceful)
        parsed = await _parse_idea(idea_text)
        if parsed:
            await _send_parse_summary(bot, chat_id, parsed)
        else:
            log.info("idea parse 단계 실패 또는 비어있음 — 제약 없이 진행")
            parsed = {}  # 빈 dict — research에서 제약 없는 것으로 간주

        # ---- (1) 리서치 (constraints 명시 주입)
        await send_text_chunked(bot, chat_id, "🌐 1단계: 웹 검색 + 후보 30 발굴")
        research = await _research_idea(idea_text, parsed)
        if not research:
            await send_text_chunked(bot, chat_id, "❌ 리서치 실패 — 종료합니다.")
            return
        await _send_research_summary(bot, chat_id, research)

        industries: list[dict] = research.get("industries") or []
        candidates: list[dict] = research.get("candidates") or []
        if not industries or not candidates:
            await send_text_chunked(bot, chat_id, "❌ 리서치 결과에 산업/후보가 비어있음 — 종료")
            return

        # ---- (1.5) 중요도 평가 (graceful — 실패해도 계속)
        await send_text_chunked(bot, chat_id, "⚖️ 1.5단계: 현상의 중요도 비판적 검증")
        importance = await _evaluate_importance(idea_text, research)
        if importance:
            await _send_importance_summary(bot, chat_id, importance)
        else:
            await send_text_chunked(
                bot, chat_id, "ℹ️ 중요도 평가 단계 실패 — Top 5 분석은 계속 진행",
            )
            importance = {}  # 빈 dict로 placeholder, synthesis에 전달 가능

        # ---- (2) 산업 리포트 수집
        await send_text_chunked(
            bot, chat_id, f"📊 2단계: 산업 리포트 다운로드 ({len(industries)}개 산업)",
        )
        industry_pdfs, industry_texts = await _collect_industry_reports(
            industries, download_root / "industry",
        )
        await send_text_chunked(
            bot, chat_id, f"  ✅ 산업 리포트 {len(industry_pdfs)}건 수집",
        )

        # ---- (3) 30 → 10 narrow
        await send_text_chunked(bot, chat_id, "🎯 3단계: 영업레버리지 4축 점수로 30→10 narrowing")
        narrow = await _narrow_candidates(idea_text, research, industry_texts, candidates)
        if not narrow or not narrow.get("top10"):
            # LLM narrow 실패 → 후보 앞 10개로 폴백 (시총 상위 순)
            log.warning("narrow 실패 — candidates 앞 10개로 폴백")
            await send_text_chunked(
                bot, chat_id,
                "⚠️ narrow LLM 실패 — 후보 시총 상위 10개로 폴백해서 계속 진행합니다",
            )
            top10 = [
                {
                    "name": c.get("name", ""),
                    "ticker6": c.get("ticker6", ""),
                    "industry": c.get("industry", ""),
                    "operating_leverage_score": "?",
                    "rationale": c.get("mechanism", ""),
                }
                for c in candidates[:10]
            ]
            narrow = {
                "narrowing_summary": "(LLM narrow 실패 — 시총 상위 10개로 폴백)",
                "top10": top10,
            }
        else:
            top10 = narrow["top10"]
        await _send_narrow_summary(bot, chat_id, narrow)

        # 30종목 4축 산점도 발송 (LLM narrow 성공한 경우만 — all30_scored가 있어야 함)
        all30_scored = (narrow or {}).get("all30_scored") or []
        if all30_scored:
            await _send_scatter_chart(bot, chat_id, idea_text, all30_scored)

        # ticker6 누락 보강 (DART 종목명 룩업)
        top10 = await _fix_tickers(top10)
        top10 = [c for c in top10 if c.get("ticker6")]
        if not top10:
            await send_text_chunked(
                bot, chat_id, "❌ top10 종목 ticker 매핑 모두 실패 — 종료",
            )
            return

        # ---- (4) 종목 리포트 수집
        await send_text_chunked(
            bot, chat_id, f"📈 4단계: top {len(top10)} 종목 리포트 다운로드 (각 {COMPANY_REPORTS_PER_TICKER}건)",
        )
        company_pdfs_by_ticker, company_texts_by_ticker = await _collect_company_reports(
            top10, download_root / "company",
        )
        total_company_pdfs = sum(len(v) for v in company_pdfs_by_ticker.values())
        await send_text_chunked(
            bot, chat_id, f"  ✅ 종목 리포트 총 {total_company_pdfs}건 수집",
        )

        # ---- (5) 최종 synthesis
        await send_text_chunked(
            bot, chat_id,
            "🧠 5단계: 사업부 식별 → 폭(magnitude) → 기울기(timing) → 영업레버리지 Top 5",
        )
        synthesis = await _synthesize_top5(
            idea_text, research, importance, industry_pdfs, industry_texts,
            top10, company_pdfs_by_ticker, company_texts_by_ticker,
        )
        if not synthesis or not synthesis.get("top5"):
            await send_text_chunked(bot, chat_id, "❌ 최종 분석 실패 — 종료")
            return

        # ---- (6) 발송
        await _send_results(
            bot, chat_id,
            synthesis,
            industry_pdfs,
            company_pdfs_by_ticker,
            company_texts_by_ticker,
        )

        # ---- (7) 캐시 저장 — /history /show /refine /dive 등 후속 명령에서 사용
        try:
            from src import idea_cache
            cache_id = idea_cache.save(
                idea_text=idea_text,
                parsed=parsed,
                research=research,
                industry_pdfs=industry_pdfs,
                industry_texts=industry_texts,
                narrow=narrow,
                top10=top10,
                company_pdfs_by_ticker=company_pdfs_by_ticker,
                company_texts_by_ticker=company_texts_by_ticker,
                synthesis=synthesis,
                download_root=download_root,
            )
            idea_cache.cleanup_old(keep=200)
        except Exception:
            log.exception("idea_cache 저장 단계 실패 — 결과는 이미 발송됨")
            cache_id = ""

        ended = datetime.now(KST).strftime("%H:%M")
        suffix = f" · id={cache_id}" if cache_id else ""
        await send_text_chunked(
            bot, chat_id, f"✅ 완료 ({started} → {ended}){suffix}",
        )
    except Exception as e:
        # OpenRouter 키 한도 초과는 사용자가 즉시 조치 가능 → 명확한 안내.
        from src import summarizer as _sm
        if isinstance(e, _sm.OpenRouterCreditExhausted):
            log.error("OpenRouter 한도 초과로 파이프라인 중단")
            try:
                await send_text_chunked(
                    bot, chat_id,
                    "❌ OpenRouter API 키 한도 초과\n\n"
                    "다음 중 하나로 해결:\n"
                    "  1) https://openrouter.ai/settings/keys 에서 사용 중인 키의 한도 상향\n"
                    "  2) 새 키 발급 → Railway 환경변수 OPENROUTER_API_KEY 업데이트\n"
                    "  3) 결제 잔액 충전 (https://openrouter.ai/credits)\n\n"
                    "조치 후 다시 시도해주세요.",
                )
            except Exception:
                pass
        else:
            log.exception("idea pipeline 최상위 예외")
            try:
                await send_text_chunked(bot, chat_id, "❌ 예상치 못한 오류 — 봇 로그 확인 필요")
            except Exception:
                pass
    finally:
        CURRENT_IDEA = None


# ------------------------------------------------------------------
# (1) 리서치 — perplexity/sonar-pro 웹검색
# ------------------------------------------------------------------
async def _parse_idea(idea_text: str) -> dict | None:
    """0.5단계: 아이디어 텍스트에서 thesis + constraints 분리.

    갓성비 모델(kimi) 1차 + 빈 응답 시 2차 재시도 + 3차 haiku 폴백 (chat_with_retry).
    파싱 실패는 graceful — 호출자가 제약 없이 계속 진행.
    """
    loop = asyncio.get_running_loop()

    def _blocking() -> dict | None:
        from src import summarizer
        try:
            client = summarizer.get_client()
        except Exception:
            log.exception("OpenRouter client init 실패 (parse)")
            return None
        sys_prompt = idea_prompts.load("idea_parse")
        user_msg = f"사용자 투자 아이디어:\n{idea_text}\n\n시스템 프롬프트 형식대로 JSON 출력."
        try:
            content = summarizer.chat_with_retry(
                client,
                model=_summary_model(),       # 1차/2차: kimi
                fallback_model=_narrow_model(),  # 3차: haiku (kimi 빈 응답 폴백)
                max_tokens=1000,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                context="idea_parse",
            )
        except summarizer.OpenRouterCreditExhausted:
            raise
        except Exception:
            log.exception("아이디어 파싱 LLM 호출 실패 (chat_with_retry)")
            return None
        log.info("parse LLM 응답: %d chars — preview=%r", len(content), content[:300])
        if not content:
            return None
        parsed = _parse_json(content)
        if parsed is None:
            log.warning("parse JSON 파싱 실패 — content 전문: %r", content[:1500])
        return parsed

    return await loop.run_in_executor(None, _blocking)


async def _send_parse_summary(bot: Bot, chat_id: str, parsed: dict) -> None:
    """0.5단계 결과: 추출된 thesis + constraints를 사용자에게 보여줌."""
    thesis = parsed.get("thesis", "")
    summary = parsed.get("constraints_summary", "")
    constraints = parsed.get("constraints") or {}
    msg = "🧭 아이디어 파싱 결과\n\n"
    if thesis:
        msg += f"📌 핵심 논리: {thesis}\n"
    if summary:
        msg += f"🎯 후보 발굴 범위: {summary}\n"
    # 디테일 — 사용자가 검증 가능하게
    detail_lines = []
    mcap_max = constraints.get("market_cap_max_krw")
    mcap_min = constraints.get("market_cap_min_krw")
    if mcap_max:
        detail_lines.append(f"  • 시총 상한: {_fmt_krw(mcap_max)}")
    if mcap_min:
        detail_lines.append(f"  • 시총 하한: {_fmt_krw(mcap_min)}")
    if constraints.get("industry_filter"):
        detail_lines.append(f"  • 산업 필터: {', '.join(constraints['industry_filter'])}")
    if constraints.get("exchange"):
        detail_lines.append(f"  • 거래소: {constraints['exchange']}")
    if constraints.get("exclude_keywords"):
        detail_lines.append(f"  • 제외: {', '.join(constraints['exclude_keywords'])}")
    if detail_lines:
        msg += "\n적용 제약:\n" + "\n".join(detail_lines)
    await send_text_chunked(bot, chat_id, msg)


def _fmt_krw(v: int | float | None) -> str:
    if v is None:
        return "-"
    try:
        v = int(v)
    except (TypeError, ValueError):
        return str(v)
    if v >= 1_000_000_000_000:
        return f"{v / 1_000_000_000_000:.1f}조"
    if v >= 100_000_000:
        return f"{v / 100_000_000:.0f}억"
    return f"{v:,}원"


async def _research_idea(idea_text: str, parsed: dict | None = None) -> dict | None:
    loop = asyncio.get_running_loop()

    def _blocking() -> dict | None:
        from src import summarizer
        try:
            client = summarizer.get_client()
        except Exception:
            log.exception("OpenRouter client init 실패")
            return None
        model = os.getenv(RESEARCH_MODEL_ENV) or DEFAULT_RESEARCH_MODEL
        sys_prompt = idea_prompts.load("idea_research")

        # parsed에서 thesis + constraints 추출. 없으면 원문만.
        thesis = (parsed or {}).get("thesis") or idea_text
        constraints = (parsed or {}).get("constraints") or {}
        constraints_block = ""
        if any(constraints.get(k) for k in (
            "market_cap_max_krw", "market_cap_min_krw",
            "industry_filter", "exchange", "exclude_keywords",
        )):
            constraints_block = (
                "\n\n<constraints>\n"
                f"{json.dumps(constraints, ensure_ascii=False, indent=2)}\n"
                "</constraints>\n"
                "위 제약을 반드시 엄격하게 적용하세요. 제약 위반 종목은 candidates에 절대 포함하지 마세요."
            )

        user_msg = (
            f"투자 아이디어 원문:\n{idea_text}\n\n"
            f"파싱된 thesis: {thesis}{constraints_block}\n\n"
            "위 자료를 시스템 프롬프트의 형식대로 JSON으로 출력해주세요."
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=12000,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
        except Exception as e:
            _maybe_raise_credit(e)
            log.exception("리서치 LLM 호출 실패")
            return None
        try:
            content = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.exception("리서치 응답 파싱 실패")
            return None
        log.info(
            "research LLM 응답: %d chars (model=%s) — preview=%r",
            len(content), model, content[:300],
        )
        parsed_resp = _parse_json(content)
        if parsed_resp is None:
            log.warning("research JSON 파싱 실패 — content 전문: %r", content[:3000])
        return parsed_resp

    return await loop.run_in_executor(None, _blocking)


async def _send_research_summary(bot: Bot, chat_id: str, research: dict) -> None:
    """1단계 결과 발송. 마크다운 파싱 실패 방지를 위해 plain text로 발송."""
    logic = research.get("logic_gradient_text", "(논리 검증 텍스트 없음)")
    industries = research.get("industries") or []
    candidates = research.get("candidates") or []
    msg = f"📐 논리의 기울기 검증\n\n{logic}\n\n"
    msg += "🏭 수혜 산업\n"
    for ind in industries:
        msg += f"  • {ind.get('name','?')} — {ind.get('reasoning','')[:120]}\n"
    msg += f"\n📋 후보 종목 ({len(candidates)}개) — 시총 상위 순\n"
    for c in candidates[:30]:
        rank = c.get("mcap_rank", "?")
        msg += f"  {rank}. {c.get('name','?')} ({c.get('ticker6','??????')}) — {c.get('industry','')}\n"
    # parse_mode 없이 plain text — 사용자 입력에 *,_,[,] 등 markdown 특수문자 있어도 안전
    await send_text_chunked(bot, chat_id, msg)


# ------------------------------------------------------------------
# (1.5) 중요도 평가 — 단호한 비판적 검증
# ------------------------------------------------------------------
async def _evaluate_importance(idea_text: str, research: dict) -> dict | None:
    """research 결과를 받아 '이 현상이 정말 중요한가'를 비판적으로 평가.

    출력: importance_score, verdict, key_arguments_for/against,
    investment_implication, proceed_with_top5.

    실패해도 None 반환 — 파이프라인은 계속 진행 (graceful degradation).
    """
    loop = asyncio.get_running_loop()

    def _blocking() -> dict | None:
        from src import summarizer
        try:
            client = summarizer.get_client()
        except Exception:
            log.exception("OpenRouter client init 실패 (importance)")
            return None
        sys_prompt = idea_prompts.load("idea_importance")

        # 작은 입력만 — research 핵심 필드만 발췌 (토큰 절감)
        compact = {
            "logic_gradient_text": research.get("logic_gradient_text", ""),
            "industries": [
                {"name": i.get("name"), "reasoning": i.get("reasoning")}
                for i in (research.get("industries") or [])
            ],
            "top_candidates": [
                {"name": c.get("name"), "industry": c.get("industry")}
                for c in (research.get("candidates") or [])[:10]
            ],
        }
        user_msg = (
            f"# 사용자 투자 아이디어\n{idea_text}\n\n"
            f"# 1단계 리서치 (요약)\n"
            f"{json.dumps(compact, ensure_ascii=False, indent=2)}\n\n"
            "위 자료를 비판적으로 검증해 시스템 프롬프트 형식대로 JSON 출력해주세요."
        )
        try:
            resp = client.chat.completions.create(
                model=_synthesis_model(),  # 지능 필요 — synthesis와 같은 등급
                max_tokens=2000,
                temperature=0.4,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
        except summarizer.APIStatusError as e:
            if summarizer._is_credit_error(e):
                raise summarizer.OpenRouterCreditExhausted(str(e)) from e
            log.exception("importance LLM API 에러")
            return None
        except Exception as e:
            _maybe_raise_credit(e)
            log.exception("importance LLM 호출 실패")
            return None
        try:
            content = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.exception("importance 응답 파싱 실패")
            return None
        log.info(
            "importance LLM 응답: %d chars (model=%s) — preview=%r",
            len(content), _synthesis_model(), content[:300],
        )
        parsed = _parse_json(content)
        if parsed is None:
            log.warning("importance JSON 파싱 실패 — content: %r", content[:2000])
        return parsed

    return await loop.run_in_executor(None, _blocking)


async def _send_importance_summary(bot: Bot, chat_id: str, imp: dict) -> None:
    """1.5단계 중요도 평가 결과 발송."""
    score = imp.get("importance_score", "?")
    verdict = imp.get("verdict", "(평가 없음)")
    fors = imp.get("key_arguments_for") or []
    againsts = imp.get("key_arguments_against") or []
    implication = imp.get("investment_implication", "")

    # 점수에 따른 시각적 표시
    try:
        s = int(score)
    except Exception:
        s = 5
    badge = "🟢" if s >= 7 else ("🟡" if s >= 5 else "🔴")

    msg = f"⚖️ 중요도 평가 (Step 1.5)\n\n"
    msg += f"{badge} 점수: {score}/10 — {verdict}\n\n"
    msg += "✅ 중요한 이유\n"
    for f in fors[:3]:
        msg += f"  • {f}\n"
    msg += "\n⚠️ 과대평가 우려\n"
    for a in againsts[:3]:
        msg += f"  • {a}\n"
    if implication:
        msg += f"\n💭 투자 시사점\n{implication}\n"
    if s < 5:
        msg += "\n⚡ 점수 < 5 — 이 아이디어는 제한적일 수 있음. 그래도 Top 5 분석은 계속 진행합니다.\n"
    await send_text_chunked(bot, chat_id, msg)


# ------------------------------------------------------------------
# (2) 산업 리포트 수집
# ------------------------------------------------------------------
async def _collect_industry_reports(
    industries: list[dict], target_dir: Path,
) -> tuple[list[Path], dict[str, str]]:
    """각 산업명을 wisereport에서 코드로 변환 후 인기 N건 다운로드 → 텍스트 추출.

    실패한 산업은 일반 industry top reports로 폴백 시도.
    반환:
      - PDF 경로 목록
      - {파일명: 텍스트} dict (LLM 컨텍스트로 사용)
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    def _blocking() -> tuple[list[Path], dict[str, str]]:
        from src.wisereport import WisereportClient
        from src import summarizer

        all_paths: list[Path] = []
        text_by_name: dict[str, str] = {}
        seen_rpt_ids: set[str] = set()  # 산업이 같은 코드로 매핑돼도 중복 다운로드 방지
        general_pool_used = False  # 일반 industry top reports는 한 번만

        try:
            with WisereportClient(
                user_id=os.environ["WISEREPORT_ID"],
                password=os.environ["WISEREPORT_PW"],
                download_root=target_dir,
                headless=True,
                ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
                state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
            ) as cli:
                cli.ensure_logged_in()

                for ind in industries:
                    name = ind.get("name", "")
                    if not name:
                        continue
                    sub_dir = target_dir / safe_dirname(name)
                    code: str | None = None
                    try:
                        code = cli.lookup_industry_code(name)
                    except Exception:
                        log.exception("산업 코드 룩업 예외: %s", name)

                    # 신뢰할 수 있는 코드만 사용. wisereport의 fallback 정규식은 G10 같은
                    # 섹터(2자리) 레벨로 잘못 떨어지는 경우가 있음. 4자 이상이면 대체로 sub-industry.
                    if code and not re.match(r"^[A-Z]?\d{4,}$", code):
                        log.info("산업 코드 '%s' (산업: %s)는 너무 광범위 — 무시하고 일반 풀 사용", code, name)
                        code = None

                    items = []
                    try:
                        if code:
                            items = cli.list_top_reports(
                                category="industry", sort_by="popular",
                                limit=INDUSTRY_REPORTS_PER_INDUSTRY,
                                days_back=180, industry_gics=code,  # 6개월 cap
                            )
                        if not items and not general_pool_used:
                            log.info("산업 '%s' 매칭 실패 → 일반 industry top reports 1회 사용", name)
                            items = cli.list_top_reports(
                                category="industry", sort_by="popular",
                                limit=INDUSTRY_REPORTS_PER_INDUSTRY * 2,
                                days_back=180,
                            )
                            general_pool_used = True
                        # 인기순이지만 sch_dt 최신 우선 보조 정렬 (같은 visit_cnt 그룹 안에서 최신 우대)
                        items.sort(key=lambda it: it.sch_dt, reverse=True)
                    except Exception:
                        log.exception("산업 리포트 목록 조회 실패: %s", name)
                        continue

                    # 이미 다운로드한 rpt_id 제외
                    items = [it for it in items if it.rpt_id not in seen_rpt_ids]
                    if not items:
                        log.info("산업 '%s' — 신규 리포트 없음 (모두 중복 또는 매칭 실패)", name)
                        continue

                    try:
                        saved = cli.download_reports(items, sub_dir)
                    except Exception:
                        log.exception("산업 리포트 다운로드 실패: %s", name)
                        continue
                    all_paths.extend(saved)
                    seen_rpt_ids.update(it.rpt_id for it in items)

                    for p in saved:
                        try:
                            t = summarizer._extract_pdf_text(p, max_chars=INDUSTRY_TEXT_MAX)
                            if t.strip():
                                text_by_name[p.name] = t
                        except Exception:
                            log.exception("산업 PDF 텍스트 추출 실패: %s", p)
        except Exception:
            log.exception("wisereport 산업 단계 자체 실패")

        return all_paths, text_by_name

    async with PIPELINE_LOCK:
        return await loop.run_in_executor(None, _blocking)


# ------------------------------------------------------------------
# (3) 30 → 10 narrow
# ------------------------------------------------------------------
async def _narrow_candidates(
    idea_text: str, research: dict, industry_texts: dict[str, str],
    candidates: list[dict],
) -> dict | None:
    loop = asyncio.get_running_loop()

    def _blocking() -> dict | None:
        from src import summarizer
        try:
            client = summarizer.get_client()
        except Exception:
            log.exception("OpenRouter client init 실패")
            return None
        sys_prompt = idea_prompts.load("idea_narrow")

        ind_block = "\n\n".join(
            f"=== 산업 리포트: {fn} ===\n{txt}"
            for fn, txt in industry_texts.items()
        ) or "(산업 리포트 텍스트 없음)"

        user_msg = (
            f"# 사용자 투자 아이디어\n{idea_text}\n\n"
            f"# 1단계 리서치 (논리의 기울기 + 산업 + 30 후보)\n"
            f"{json.dumps(research, ensure_ascii=False, indent=2)[:15_000]}\n\n"
            f"# 산업 리포트 텍스트\n{ind_block[:60_000]}\n\n"
            "위 자료를 종합해 시스템 프롬프트 형식대로 top10 JSON을 출력해주세요."
        )
        # 빈 응답 재시도 (kimi가 가끔 idle timeout으로 0자 반환).
        # 두 번째 시도는 temperature 약간 올려 동일 응답 회피.
        content = ""
        for attempt in (1, 2):
            try:
                resp = client.chat.completions.create(
                    model=_narrow_model(),
                    max_tokens=12000,
                    temperature=0.3 if attempt == 1 else 0.5,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                )
            except summarizer.APIStatusError as e:
                if summarizer._is_credit_error(e):
                    raise summarizer.OpenRouterCreditExhausted(str(e)) from e
                log.exception("narrow LLM API 에러 (attempt %d)", attempt)
                return None
            except Exception as e:
                _maybe_raise_credit(e)
                log.exception("narrow LLM 호출 실패 (attempt %d)", attempt)
                if attempt == 2:
                    return None
                continue
            try:
                content = (resp.choices[0].message.content or "").strip()
            except Exception:
                log.exception("narrow 응답 추출 실패 (attempt %d)", attempt)
                if attempt == 2:
                    return None
                continue
            if content:
                break
            log.warning("narrow LLM 빈 응답 (attempt %d) — 재시도", attempt)
        log.info(
            "narrow LLM 응답: %d chars (model=%s) — preview=%r",
            len(content), _narrow_model(), content[:300],
        )
        parsed = _parse_json(content)
        if parsed is None:
            log.warning("narrow JSON 파싱 실패 — content 전문: %r", content[:3000])
            return None
        if not parsed.get("top10"):
            log.warning("narrow 응답에 top10 없음 — keys=%s", list(parsed.keys()))
        return parsed

    return await loop.run_in_executor(None, _blocking)


async def _send_narrow_summary(bot: Bot, chat_id: str, narrow: dict) -> None:
    """3단계 결과 발송 (plain text)."""
    summary = narrow.get("narrowing_summary", "")
    top10 = narrow.get("top10") or []
    msg = "🎯 Narrow 결과 (30 → 10)\n\n"
    if summary:
        msg += summary + "\n\n"
    msg += "Top 10 (영업레버리지 점수순)\n"
    for i, c in enumerate(top10, 1):
        msg += (
            f"{i}. {c.get('name','?')} ({c.get('ticker6','??????')}) — "
            f"점수 {c.get('operating_leverage_score','?')}/10 — "
            f"{c.get('industry','')}\n"
        )
    await send_text_chunked(bot, chat_id, msg)


async def _send_scatter_chart(
    bot: Bot, chat_id: str, idea_text: str, all30_scored: list[dict],
) -> None:
    """30종목 4축 산점도 PNG 발송. 차트 생성/발송 어느 단계 실패해도 봇은 계속."""
    loop = asyncio.get_running_loop()
    try:
        from src import idea_chart
        png_bytes = await loop.run_in_executor(
            None, idea_chart.build, idea_text, all30_scored,
        )
    except Exception:
        log.exception("산점도 생성 단계 실패 — 차트 스킵")
        return
    if not png_bytes:
        log.info("산점도 PNG 비어있음 — 스킵 (all30_scored=%d개)", len(all30_scored))
        return
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=png_bytes,
            caption=(
                "📊 30 후보 4축 산점도\n"
                "X=매출가속, Y=고정비비중, 점크기=마진민감도, 색=가동률여유\n"
                "오른쪽 위 + 큰 점 + 진한 색 = 영업레버리지 강한 zone"
            ),
        )
    except Exception:
        log.exception("산점도 텔레그램 발송 실패 — 무시하고 계속")


async def _fix_tickers(top10: list[dict]) -> list[dict]:
    """후보의 (name, ticker6) 페어를 DART corp_map과 대조해 검증·교정.

    동작:
      1. ticker6 있음 → DART에서 ticker → 등록 회사명 조회
         - LLM이 준 name과 정규화 비교해 일치 → OK
         - 불일치 (예: HD현대오일뱅크는 비상장, LLM이 002380=KCC를 잘못 매핑) →
           LLM name으로 재lookup해서 새 ticker 교체. 못 찾으면 ticker 비움.
      2. ticker6 없음 → name으로 lookup (기존 동작).
      3. 그래도 ticker 못 찾으면 비워둠 → 후속 _collect_company_reports에서 스킵.
    """
    loop = asyncio.get_running_loop()
    try:
        from src.deepdive import dart_client
    except Exception:
        log.exception("dart_client import 실패 — ticker 룩업 스킵")
        return top10

    # corp_map 빌드 (한 번)
    try:
        await loop.run_in_executor(None, dart_client._load_corp_map)
    except Exception:
        log.exception("DART corp_map 로딩 실패")

    def _norm(s: str) -> str:
        # 회사 suffix(공업/산업/홀딩스/지주)도 약식으로 제거 — 명칭 줄임 매칭 위해
        s = re.sub(r"[\s()㈜（）\-]+", "", s or "").lower()
        return s

    def _names_match(llm_name: str, dart_name: str) -> bool:
        """LLM이 준 회사명과 DART 등록명이 사실상 같은 회사를 가리키는지 fuzzy 판단.

        - 정규화 후 정확 일치 → True
        - 정규화 후 한쪽이 다른 쪽의 substring → True (예: '한국석유' ↔ '한국석유공업')
        - 빈 문자열 → False
        """
        nl = _norm(llm_name)
        nd = _norm(dart_name)
        if not nl or not nd:
            return False
        if nl == nd:
            return True
        if nl in nd or nd in nl:
            return True
        return False

    out: list[dict] = []
    for c in top10:
        ticker = (c.get("ticker6") or "").strip()
        name = (c.get("name") or "").strip()

        if re.match(r"^\d{6}$", ticker):
            # ticker 있음 — 회사명 일치 검증 (substring 허용)
            dart_name = dart_client._CORP_NAME_CACHE.get(ticker, "")
            if _names_match(name, dart_name):
                out.append(c)
                continue
            # 불일치 → name으로 재lookup
            log.warning(
                "ticker mismatch: name='%s' ticker=%s but DART에는 '%s' — name으로 재lookup",
                name, ticker, dart_name,
            )
            new_ticker = None
            if name:
                try:
                    new_ticker = await loop.run_in_executor(None, dart_client.lookup_ticker_by_name, name)
                except Exception:
                    log.exception("재lookup 실패: %s", name)
            if new_ticker and new_ticker != ticker:
                log.info("ticker 교체: %s '%s' → %s '%s'", ticker, name, new_ticker, dart_client._CORP_NAME_CACHE.get(new_ticker, ""))
                c["ticker6"] = new_ticker
            else:
                # 비상장이거나 매칭 못 함 — ticker 비우면 후속 단계에서 스킵
                log.warning("ticker 매칭 실패 (비상장 가능성) — '%s' candidate ticker6 비움", name)
                c["ticker6"] = ""
            out.append(c)
            continue

        # ticker 비어있음 — name으로 lookup
        if not name:
            out.append(c)
            continue
        try:
            t = await loop.run_in_executor(None, dart_client.lookup_ticker_by_name, name)
        except Exception:
            log.exception("ticker 룩업 실패: %s", name)
            t = None
        if t:
            c["ticker6"] = t
        out.append(c)
    return out


# ------------------------------------------------------------------
# (4) 종목 리포트 수집
# ------------------------------------------------------------------
async def _collect_company_reports(
    top10: list[dict], target_dir: Path,
) -> tuple[dict[str, list[Path]], dict[str, dict[str, str]]]:
    """각 ticker별로 wisereport 종목 리포트 N건 다운로드 + 텍스트 추출.

    반환:
      - {ticker: [PDF paths]}
      - {ticker: {filename: text}}
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    def _blocking() -> tuple[dict[str, list[Path]], dict[str, dict[str, str]]]:
        from src.wisereport import WisereportClient
        from src import summarizer

        paths_by_ticker: dict[str, list[Path]] = {}
        texts_by_ticker: dict[str, dict[str, str]] = {}

        try:
            with WisereportClient(
                user_id=os.environ["WISEREPORT_ID"],
                password=os.environ["WISEREPORT_PW"],
                download_root=target_dir,
                headless=True,
                ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
                state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
            ) as cli:
                cli.ensure_logged_in()
                for c in top10:
                    ticker = (c.get("ticker6") or "").strip()
                    if not re.match(r"^\d{6}$", ticker):
                        log.info(
                            "ticker 없음 → 종목 리포트 스킵: name='%s' (비상장 또는 매칭 실패)",
                            c.get("name", "?"),
                        )
                        continue
                    sub_dir = target_dir / ticker
                    try:
                        items = cli.list_reports(
                            ticker=ticker, sort_by="latest",
                            limit=COMPANY_REPORTS_PER_TICKER,
                            days_back=180,  # 6개월 이내만 — 옛날 리포트 차단
                        )
                    except Exception:
                        log.exception("종목 리포트 목록 실패: %s", ticker)
                        continue
                    if not items:
                        continue
                    try:
                        saved = cli.download_reports(items, sub_dir)
                    except Exception:
                        log.exception("종목 리포트 다운로드 실패: %s", ticker)
                        continue
                    if not saved:
                        continue
                    paths_by_ticker[ticker] = saved
                    texts: dict[str, str] = {}
                    for p in saved:
                        try:
                            t = summarizer._extract_pdf_text(p, max_chars=COMPANY_TEXT_MAX)
                            if t.strip():
                                texts[p.name] = t
                        except Exception:
                            log.exception("종목 PDF 텍스트 추출 실패: %s", p)
                    texts_by_ticker[ticker] = texts
        except Exception:
            log.exception("wisereport 종목 단계 자체 실패")

        return paths_by_ticker, texts_by_ticker

    async with PIPELINE_LOCK:
        return await loop.run_in_executor(None, _blocking)


# ------------------------------------------------------------------
# (5) 최종 Top 5 synthesis
# ------------------------------------------------------------------
async def _synthesize_top5(
    idea_text: str, research: dict, importance: dict,
    industry_pdfs: list[Path], industry_texts: dict[str, str],
    top10: list[dict],
    company_pdfs_by_ticker: dict[str, list[Path]],
    company_texts_by_ticker: dict[str, dict[str, str]],
) -> dict | None:
    loop = asyncio.get_running_loop()

    def _blocking() -> dict | None:
        from src import summarizer
        try:
            client = summarizer.get_client()
        except Exception:
            log.exception("OpenRouter client init 실패")
            return None
        sys_prompt = idea_prompts.load("idea_synthesis")

        # 컨텍스트 빌드
        ind_block = "\n\n".join(
            f"=== 산업 리포트 파일: {fn} ===\n{txt}"
            for fn, txt in industry_texts.items()
        ) or "(산업 리포트 없음)"

        company_blocks: list[str] = []
        for c in top10:
            ticker = c.get("ticker6", "")
            name = c.get("name", "")
            texts = company_texts_by_ticker.get(ticker, {})
            if not texts:
                company_blocks.append(
                    f"=== {name} ({ticker}) — 종목 리포트 없음 ===\n"
                    f"산업 리포트와 1단계 리서치 정보로 평가."
                )
                continue
            for fn, txt in texts.items():
                company_blocks.append(
                    f"=== {name} ({ticker}) 리포트 파일: {fn} ===\n{txt}"
                )
        company_block = "\n\n".join(company_blocks) or "(종목 리포트 없음)"

        # 1.5단계 중요도 평가도 컨텍스트로 주입 (verdict, score, args)
        importance_ctx = json.dumps(importance or {}, ensure_ascii=False, indent=2)[:3000]

        user_msg = (
            f"# 사용자 투자 아이디어\n{idea_text}\n\n"
            f"# 1단계 리서치 — 논리의 기울기\n"
            f"{json.dumps(research.get('logic_gradient_text',''), ensure_ascii=False)[:3000]}\n\n"
            f"# 1.5단계 중요도 평가 (이 평가를 진지하게 받아 thesis에 반영)\n"
            f"{importance_ctx}\n\n"
            f"# top10 후보 (3단계 narrow 결과)\n"
            f"{json.dumps(top10, ensure_ascii=False, indent=2)[:8_000]}\n\n"
            f"# 산업 리포트 텍스트\n{ind_block[:50_000]}\n\n"
            f"# 종목 리포트 텍스트\n{company_block[:60_000]}\n\n"
            "시스템 프롬프트 형식대로 Top 5 JSON을 출력해주세요. "
            "각 종목마다 사업부 식별 → 영업레버리지 폭 → 기울기 → thesis 순으로 단계적으로 추론하고 "
            "최종 thesis에 통합해주세요. "
            "referenced_reports에는 해당 종목의 own 종목 리포트 파일명만 정확히 사용해주세요."
        )
        # max_tokens=12000 — Top 5 × 1500자 thesis + key_numbers + methodology
        #   (이전 8000은 Claude Sonnet에서 마지막 종목의 referenced_reports 직전에 truncate됨).
        # 빈 응답 재시도 1회.
        content = ""
        for attempt in (1, 2):
            try:
                resp = client.chat.completions.create(
                    model=_synthesis_model(),
                    max_tokens=16000,
                    temperature=0.4 if attempt == 1 else 0.6,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                )
            except summarizer.APIStatusError as e:
                if summarizer._is_credit_error(e):
                    raise summarizer.OpenRouterCreditExhausted(str(e)) from e
                log.exception("synthesis LLM API 에러 (attempt %d)", attempt)
                return None
            except Exception as e:
                _maybe_raise_credit(e)
                log.exception("synthesis LLM 호출 실패 (attempt %d)", attempt)
                if attempt == 2:
                    return None
                continue
            try:
                content = (resp.choices[0].message.content or "").strip()
            except Exception:
                log.exception("synthesis 응답 추출 실패 (attempt %d)", attempt)
                if attempt == 2:
                    return None
                continue
            if content:
                break
            log.warning("synthesis LLM 빈 응답 (attempt %d) — 재시도", attempt)
        log.info(
            "synthesis LLM 응답: %d chars (model=%s) — preview=%r",
            len(content), _synthesis_model(), content[:300],
        )
        parsed = _parse_json(content)
        if parsed is None:
            log.warning("synthesis JSON 파싱 실패 — content 전문: %r", content[:5000])
            return None
        if not parsed.get("top5"):
            log.warning("synthesis 응답에 top5 없음 — keys=%s", list(parsed.keys()))
        return parsed

    return await loop.run_in_executor(None, _blocking)


# ------------------------------------------------------------------
# (6) 텔레그램 발송
# ------------------------------------------------------------------
async def _send_top1_quarterly_chart(
    bot: Bot,
    chat_id: str,
    top1_pick: dict,
    company_texts_by_ticker: dict[str, dict[str, str]],
) -> None:
    """Top 1 종목의 분기 매출/영업이익/순이익 + (옵션) forward consensus 차트.

    - DART에서 corp_code → 분기 재무 fetch + 잠정실적 보강.
    - wisereport 종목 리포트 텍스트 → forward consensus 추출 (옵션).
    - matplotlib 차트 PNG → bot.send_photo.

    실패해도 다른 결과 영향 없음 (caller에서 try/except).
    """
    ticker = (top1_pick.get("ticker6") or "").strip()
    name = top1_pick.get("name", "?")
    if not re.match(r"^\d{6}$", ticker):
        log.info("Top 1 ticker 없음 — 분기 차트 스킵: %s", name)
        return

    loop = asyncio.get_running_loop()

    # corp_code lookup
    try:
        from src.deepdive import dart_client
    except Exception:
        log.exception("dart_client import 실패 — 분기 차트 스킵")
        return
    pair = await loop.run_in_executor(None, dart_client.get_corp_code, ticker)
    if not pair:
        log.info("Top 1 corp_code 없음: %s (%s) — 분기 차트 스킵", name, ticker)
        return
    corp_code, corp_name = pair

    # 분기 재무 fetch
    try:
        fin = await loop.run_in_executor(None, dart_client.fetch_quarterly_financials, corp_code, 3)
    except Exception:
        log.exception("Top 1 분기 재무 fetch 실패")
        return
    if not fin or not fin.revenue_qoq:
        log.info("Top 1 분기 재무 데이터 없음 — 차트 스킵")
        return

    # 잠정실적 보강 (실패해도 무시)
    try:
        from pathlib import Path as _P
        prelim_dir = download_root_for("idea") / "_top1_preliminary" / ticker
        preliminary = await loop.run_in_executor(
            None, dart_client.fetch_latest_preliminary_quarter, corp_code, prelim_dir,
        )
        if preliminary:
            yq, rev, op, net = preliminary
            if yq not in fin.revenue_qoq:
                fin.revenue_qoq[yq] = rev
                fin.op_profit_qoq[yq] = op
                fin.net_profit_qoq[yq] = net
                log.info("Top 1 잠정실적 보강: %s", yq)
    except Exception:
        log.info("Top 1 잠정실적 보강 실패 — 무시")

    # forward consensus — top 1 ticker의 wisereport 종목 리포트 텍스트로부터 LLM 추출
    forward = None
    try:
        from src.deepdive import forward_consensus
        from src.deepdive.wisereport_context import WisereportContext
        # WisereportContext 형태로 wrap (forward_consensus.from_wisereport 시그니처 맞추기)
        own_texts = list((company_texts_by_ticker.get(ticker) or {}).values())
        own_titles = list((company_texts_by_ticker.get(ticker) or {}).keys())
        if own_texts:
            wctx = WisereportContext(
                company_texts=own_texts, company_titles=own_titles,
                industry_texts=[], industry_titles=[],
            )
            forward = await loop.run_in_executor(None, forward_consensus.from_wisereport, wctx)
    except Exception:
        log.info("Top 1 forward consensus 실패 — 무시")

    # 차트 build + 발송
    try:
        from src.deepdive import chart
        png_bytes = await loop.run_in_executor(
            None, chart.build,
            corp_name,
            fin.revenue_qoq, fin.op_profit_qoq, fin.net_profit_qoq,
            None, forward or None,
        )
    except Exception:
        log.exception("Top 1 chart.build 실패")
        return
    if not png_bytes:
        return
    try:
        fwd_info = (
            f" + Forward {len(forward)}분기 (wisereport 컨센서스)" if forward
            else " (Forward 미수집)"
        )
        await bot.send_photo(
            chat_id=chat_id,
            photo=png_bytes,
            caption=f"📈 Top 1 {corp_name}({ticker}) 분기별 재무{fwd_info}",
        )
    except Exception:
        log.exception("Top 1 분기 차트 발송 실패")


async def _send_results(
    bot: Bot, chat_id: str,
    synthesis: dict,
    industry_pdfs: list[Path],
    company_pdfs_by_ticker: dict[str, list[Path]],
    company_texts_by_ticker: dict[str, dict[str, str]] | None = None,
) -> None:
    """Top 5 결과 발송.

    참고 리포트 처리 원칙 (종목별 ↔ 산업별 분리):
      - 각 Top N 종목 블록에는 그 종목의 own 종목 리포트만 첨부.
      - 산업 리포트는 마지막에 한 번 일괄 첨부 (모든 픽 공통 컨텍스트).
      - LLM이 referenced_reports에 다른 회사 리포트나 산업 리포트를 끼워
        넣어도 own 리포트가 아니면 본문에서 제외하고 PDF로도 발송 안 함.
    """
    methodology = synthesis.get("ranking_methodology", "")
    top5 = synthesis.get("top5") or []

    intro = (
        "🏆 영업레버리지 Top 5 — 최종 선정\n\n"
        f"랭킹 근거:\n{methodology}\n"
    )
    await send_text_chunked(bot, chat_id, intro)

    sent_pdf_names: set[str] = set()

    for pick in top5:
        rank = pick.get("rank", "?")
        name = pick.get("name", "?")
        ticker = pick.get("ticker6", "??????")
        industry = pick.get("industry", "")
        business_unit = pick.get("business_unit", "")
        magnitude = pick.get("magnitude", "")
        gradient_timing = pick.get("gradient_timing", "")
        thesis = pick.get("operating_leverage_thesis", "(thesis 없음)")
        kn = pick.get("key_numbers") or {}
        refs = pick.get("referenced_reports") or []

        # 이 종목 own 리포트 파일명 집합
        own_pdfs = company_pdfs_by_ticker.get(ticker, [])
        own_pdf_names = {p.name for p in own_pdfs}

        # LLM이 인용한 refs 중 own 리포트만 채택. 그 외(산업/타종목)는 잘못 인용된
        # 것으로 간주하고 표시·발송에서 제외.
        valid_refs = [r for r in refs if (r or "").strip() in own_pdf_names]
        invalid_refs = [r for r in refs if (r or "").strip() and (r or "").strip() not in own_pdf_names]
        if invalid_refs:
            log.info(
                "Top%s %s (%s) 잘못된 ref 제외: %s",
                rank, name, ticker, invalid_refs[:5],
            )

        # 가격 정보 (Naver Finance) — fetch 실패 시 빈 문자열
        try:
            from src import price_fetcher
            quote = price_fetcher.fetch_quote(ticker) if re.match(r"^\d{6}$", ticker) else None
            price_brief = price_fetcher.format_quote_brief(quote)
        except Exception:
            log.info("가격 fetch 실패 (Top %s %s) — 무시", rank, ticker)
            price_brief = ""

        header = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🥇 Top {rank}. {name} ({ticker}) — {industry}\n"
        )
        if price_brief:
            header += f"   💰 {price_brief}\n"
        header += "━━━━━━━━━━━━━━━━━━━━"
        await send_text_chunked(bot, chat_id, header)

        # 5단계 추론 요약 헤더 (사업부/폭/기울기) — thesis 위에 짧게 노출
        summary_header = ""
        if business_unit:
            summary_header += f"🏢 사업부: {business_unit}\n"
        if magnitude:
            summary_header += f"📐 폭(magnitude): {magnitude}\n"
        if gradient_timing:
            summary_header += f"⏱️ 기울기/시점: {gradient_timing}\n"
        if summary_header:
            summary_header += "\n"

        body = summary_header + thesis
        if kn:
            body += "\n\n📊 Key Numbers\n"
            for k, v in kn.items():
                body += f"  • {k}: {v}\n"
        if valid_refs:
            body += "\n📎 참고 종목 리포트: " + ", ".join(valid_refs)
        elif own_pdfs:
            # LLM이 own 리포트를 명시 안 했어도 다운받은 own 리포트 모두 첨부
            body += "\n📎 참고 종목 리포트: " + ", ".join(p.name for p in own_pdfs)
        else:
            body += "\n📎 참고 종목 리포트: (해당 종목 리포트 없음 — 산업 리포트 참조)"
        await send_text_chunked(bot, chat_id, body)

        # PDF 첨부: own 종목 리포트만 (산업 리포트는 종목별로 첨부 안 함).
        for p in own_pdfs:
            if p.name in sent_pdf_names:
                continue
            await send_pdf(bot, chat_id, p, caption=f"[Top {rank} {name}] {p.name}")
            sent_pdf_names.add(p.name)

    # ---- Top 1 분기 차트 — DART 분기 재무 + (옵션) wisereport forward consensus
    if top5:
        try:
            await _send_top1_quarterly_chart(bot, chat_id, top5[0], company_texts_by_ticker or {})
        except Exception:
            log.exception("Top 1 분기 차트 단계 실패 — 무시 (다른 결과는 정상)")

    # ---- 산업 리포트는 마지막에 한 번만 일괄 첨부 (모든 픽의 공통 컨텍스트)
    if industry_pdfs:
        await send_text_chunked(
            bot, chat_id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🏭 공통 산업 리포트 (모든 픽의 매크로 컨텍스트)\n"
            "━━━━━━━━━━━━━━━━━━━━",
        )
        for p in industry_pdfs:
            if p.name in sent_pdf_names:
                continue
            await send_pdf(bot, chat_id, p, caption=f"[산업] {p.name}")
            sent_pdf_names.add(p.name)

    # Top 5 명세를 한 줄 요약으로 로그 — 검증·디버깅에 핵심.
    top5_brief = " / ".join(
        f"#{p.get('rank','?')} {p.get('name','?')}({p.get('ticker6','?')}) — {(p.get('business_unit') or '')[:25]}"
        for p in top5
    )
    log.info(
        "[send_results 완료] top5=%d개, PDF 첨부 %d건 — %s",
        len(top5), len(sent_pdf_names), top5_brief,
    )


# ------------------------------------------------------------------
# 헬퍼
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# 모델 티어 매트릭스 (OPENROUTER_MODEL = 갓성비 cheap, IDEA_NARROW_MODEL = mid,
# IDEA_SYNTHESIS_MODEL = premium)
#
#   _summary_model()   → OPENROUTER_MODEL (kimi 등) — parse, 단순 JSON 추출
#                        같은 티어를 PDF 요약·DART 파싱·deepdive 요약도 사용.
#   _research_model()  → IDEA_RESEARCH_MODEL (perplexity/sonar-pro) — 웹검색
#   _narrow_model()    → IDEA_NARROW_MODEL (haiku 등) — 30→10 점수화 (큰 출력)
#   _synthesis_model() → IDEA_SYNTHESIS_MODEL (sonnet 등) — 1.5+5 단계 진짜 지능
# ------------------------------------------------------------------
def _summary_model() -> str:
    """0.5단계 parse 등 단순 추출용 — 가장 저렴한 OPENROUTER_MODEL 사용.

    PDF 요약·DART 파싱·deepdive 요약과 같은 티어. kimi-k2.6 등 갓성비 모델 권장.
    """
    return os.getenv("OPENROUTER_MODEL") or "moonshotai/kimi-k2.6"


def _synthesis_model() -> str:
    """1.5단계 중요도 평가 + 5단계 최종 Top 5 합성 — 가장 지능 필요. 기본 claude-sonnet."""
    explicit = os.getenv(SYNTHESIS_MODEL_ENV)
    if explicit:
        return explicit
    return os.getenv("OPENROUTER_MODEL") or "anthropic/claude-sonnet-4.5"


def _narrow_model() -> str:
    """3단계 30→10 narrowing — 정형화된 스코어링이라 평소 모델로 충분.

    명시값(IDEA_NARROW_MODEL) 없으면 OPENROUTER_MODEL(평소 모델) 사용.
    """
    explicit = os.getenv(NARROW_MODEL_ENV)
    if explicit:
        return explicit
    return os.getenv("OPENROUTER_MODEL") or "anthropic/claude-sonnet-4.5"


def _maybe_raise_credit(e: Exception) -> None:
    """OpenRouter 키 한도/결제 오류면 OpenRouterCreditExhausted 재라이즈.

    그렇지 않으면 no-op. 모든 LLM 호출의 except 블록에서 가장 먼저 호출하면
    credit error를 silent 실패 대신 사용자에게 명확히 전달.
    """
    from src import summarizer
    if isinstance(e, summarizer.APIStatusError) and summarizer._is_credit_error(e):
        raise summarizer.OpenRouterCreditExhausted(str(e)) from e


def _parse_json(content: str) -> dict | None:
    """LLM 응답에서 JSON 객체 추출 (tolerant).

    처리 단계:
      1. 코드 펜스 ```json ... ``` 제거
      2. 첫 { 부터 마지막 } 까지 잘라냄
      3. trailing comma 정리 (`,]` → `]`, `,}` → `}`)
      4. JS 라인 코멘트 / 블록 코멘트 제거
      5. 그래도 실패 시 progressive truncate — 끝에서부터 한 글자씩 빼며 재시도
         (Claude가 마지막 객체에서 truncate된 경우 직전 } 까지 살리기)

    파싱 실패 시 None.
    """
    if not content:
        log.warning("_parse_json: 빈 content")
        return None
    # 코드 펜스 제거
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        content_inner = fence.group(1)
    else:
        content_inner = content
    # 첫 { 부터 마지막 } 까지
    start = content_inner.find("{")
    end = content_inner.rfind("}")
    if start < 0 or end <= start:
        log.warning("JSON 블록 못 찾음 (head=%r tail=%r)", content_inner[:200], content_inner[-200:])
        return None
    blob = content_inner[start:end + 1]

    # 1차: 그대로 시도
    obj = _try_loads(blob)
    if obj is not None:
        return obj if isinstance(obj, dict) else None

    # 2차: 정리(trailing comma + 코멘트) 후 시도
    cleaned = _clean_json_loose(blob)
    obj = _try_loads(cleaned)
    if obj is not None:
        log.info("JSON 파싱 — cleanup 후 성공")
        return obj if isinstance(obj, dict) else None

    # 3차: depth/string-tracking 으로 완전히 닫힌 마지막 top-level 위치까지 잘라냄
    #      (Claude가 mid-string 으로 truncate된 경우에 대응. 단순 } 카운팅으론
    #       문자열 안의 }와 진짜 }를 구별 못함.)
    safe_blob = _truncate_to_balanced_json(content_inner, start)
    if safe_blob is not None:
        cleaned = _clean_json_loose(safe_blob)
        obj = _try_loads(cleaned)
        if obj is not None:
            log.info("JSON 파싱 — balanced truncate 성공 (len=%d)", len(safe_blob))
            return obj if isinstance(obj, dict) else None

    # 4차: depth-aware partial recovery — 마지막 '완성된 top5 항목까지'라도 살리기.
    #      중첩 array/object 가 닫히지 않은 채로 끝났으면, 강제로 ] 와 } 닫아서
    #      직전까지의 항목만이라도 재구성.
    forced = _force_close_open_brackets(content_inner, start, end)
    if forced is not None:
        cleaned = _clean_json_loose(forced)
        obj = _try_loads(cleaned)
        if obj is not None:
            log.info("JSON 파싱 — force-close 성공 (len=%d)", len(forced))
            return obj if isinstance(obj, dict) else None

    log.warning("JSON 파싱 최종 실패 — head=%r tail=%r", blob[:200], blob[-200:])
    return None


def _truncate_to_balanced_json(s: str, start: int) -> str | None:
    """문자열·이스케이프를 추적하며 depth가 0으로 돌아온 마지막 위치까지 잘라냄.

    LLM이 출력 중간(예: 문자열 안)에서 끊긴 경우, 가장 가까운 안전한 cutoff를 찾는다.
    반환: s[start:cutoff+1] 형태의 부분 문자열 (성공 시) 또는 None.
    """
    if start < 0 or start >= len(s):
        return None
    if s[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    last_complete = -1  # depth가 0으로 돌아온 가장 최근 위치
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{" or c == "[":
            depth += 1
        elif c == "}" or c == "]":
            depth -= 1
            if depth == 0:
                last_complete = i
    if last_complete < 0:
        return None
    return s[start:last_complete + 1]


def _force_close_open_brackets(s: str, start: int, end: int) -> str | None:
    """LLM이 array 중간에서 끊긴 경우 마지막 완전한 *항목* 까지 보존하고 강제 닫음.

    동작:
      1. start 부터 한 글자씩 진행. 문자열·이스케이프 추적.
      2. push/pop마다 stack 변화 기록.
      3. **모든 depth에서** "직전이 닫힌 위치" safe_positions[depth] 를 추적.
      4. **문자열 안에서 EOF 만나면** 그 문자열 시작 직전의 마지막 안전 위치까지 백트랙
         (perplexity 등이 specialty_note 같은 string field 안에서 truncate되는 케이스 대응).
      5. 끝까지 가서 닫히지 않은 ']' 와 '}' 가 있으면, **현재 남은 stack depth와 일치하는**
         safe_positions[len(stack)] 위치까지 잘라내고 stack 역순으로 닫기.

    예: top5 배열에 5개 항목 중 5번째가 닫혔지만 array ']'와 root '}'가 missing인 경우
        → stack=['{', '['] 길이 2. safe_positions[2] = 5번째 항목 '}' 위치.
        → 그 위치까지 잘라낸 후 ']}' append → 5개 항목 보존된 valid JSON.
    """
    if start < 0 or start >= len(s) or s[start] != "{":
        return None
    in_string = False
    escape = False
    stack: list[str] = []
    safe_positions: dict[int, int] = {}  # depth-after-pop → 그 depth에서 마지막 close 위치
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c in "{[":
            stack.append(c)
        elif c in "}]":
            if not stack:
                return None  # 비정상 — 매칭 brackets 깨짐
            stack.pop()
            safe_positions[len(stack)] = i
    if not stack:
        return s[start:end + 1] if end >= start else None
    # safe_positions[len(stack)]이 None인 경우 = 현재 top stack 안에서 한 번도 자식이 닫히지 않음.
    # (예: candidate object 안의 string field에서 truncate되어 자식이 0개 닫힘)
    # stack을 가상으로 pop하면서 가능한 safe position 탐색 — 가장 데이터 많이 보존되는 거 우선.
    truncated_blob = None
    closing = ""
    for pop_count in range(0, len(stack) + 1):
        target_depth = len(stack) - pop_count
        safe = safe_positions.get(target_depth)
        if safe is None or safe <= start:
            continue
        truncated_blob = s[start:safe + 1]
        items_to_close = stack[:target_depth]
        closing = "".join("]" if ch == "[" else "}" for ch in reversed(items_to_close))
        break
    if truncated_blob is None:
        return None
    # trailing comma 가능성 → cleanup이 처리
    return truncated_blob + closing


def _try_loads(s: str):
    """json.loads 성공 시 객체, 실패 시 None."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None
    except Exception:
        return None


def _clean_json_loose(s: str) -> str:
    """LLM JSON에서 흔한 비표준 표기 제거.

    - trailing comma: `,\s*]` → `]`, `,\s*}` → `}`
    - JS 라인 코멘트: `// ...` 줄
    - JS 블록 코멘트: `/* ... */`
    """
    # 라인 코멘트 (행 끝까지)
    s = re.sub(r"//[^\n\r]*", "", s)
    # 블록 코멘트
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    # trailing comma
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


# ------------------------------------------------------------------
# Application 빌더 — orchestrator가 호출
# ------------------------------------------------------------------
async def _self_test(app: Application) -> None:
    """IDEA_TEST_PROMPT env가 설정돼 있으면 부팅 후 한 번 파이프라인 자동 실행.

    검증용. 실제 사용자 인터랙션 없이도 idea_bot 동작을 끝까지 검증할 수 있게 한다.
    chat_id는 IDEA_TEST_CHAT_ID > IDEA_CHAT_ID > IDEA_ALLOWED_CHAT_IDS의 첫 번째.
    """
    test_prompt = (os.getenv("IDEA_TEST_PROMPT") or "").strip()
    if not test_prompt:
        return
    chat_id = (
        os.getenv("IDEA_TEST_CHAT_ID")
        or os.getenv("IDEA_CHAT_ID")
        or (os.getenv("IDEA_ALLOWED_CHAT_IDS", "").split(",") + [""])[0].strip()
    )
    if not chat_id:
        log.warning("[self-test] IDEA_TEST_PROMPT set but no chat_id — 스킵")
        return

    log.info("=" * 60)
    log.info("[self-test] 자동 검증 시작 — chat_id=%s prompt=%r", chat_id, test_prompt)
    log.info("=" * 60)

    # 가짜 Update + Context — _run_pipeline는 이 안의 effective_chat.id, message.text, context.bot만 사용
    class _FakeChat:
        def __init__(self, cid: str) -> None:
            self.id = int(cid)

    class _FakeMessage:
        def __init__(self, cid: str, text: str) -> None:
            self.text = text
            self.chat = _FakeChat(cid)

        async def reply_text(self, *a, **kw) -> None:
            try:
                await app.bot.send_message(chat_id=self.chat.id, text=str(a[0]) if a else (kw.get("text") or ""))
            except Exception:
                log.exception("[self-test] reply_text 실패")

    class _FakeUpdate:
        def __init__(self, cid: str, text: str) -> None:
            self.effective_chat = _FakeChat(cid)
            self.message = _FakeMessage(cid, text)

    class _FakeContext:
        def __init__(self) -> None:
            self.bot = app.bot
            self.args: list[str] = []

    # 모든 봇 폴링이 안정화된 후 실행 (구 컨테이너 Conflict 메시지 안 끼게)
    await asyncio.sleep(15)
    try:
        await _run_pipeline(_FakeUpdate(chat_id, test_prompt), _FakeContext(), test_prompt)
    except Exception:
        log.exception("[self-test] 파이프라인 최상위 예외")
    log.info("=" * 60)
    log.info("[self-test] 종료")
    log.info("=" * 60)


def build_idea_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(CommandHandler("idea", _cmd_idea))
    app.add_handler(CommandHandler("history", _cmd_history))
    app.add_handler(CommandHandler("show", _cmd_show))
    app.add_handler(CommandHandler("dive", _cmd_dive))
    app.add_handler(CommandHandler("refine", _cmd_refine))
    app.add_handler(CommandHandler("contrarian", _cmd_contrarian))
    app.add_handler(CommandHandler("compare", _cmd_compare))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    # self-test 자동 실행 — orchestrator가 build_idea_app을 async _run_forever 안에서
    # 호출하므로 running loop이 이미 있음. PTB v22의 post_init 훅은 run_polling()
    # 에서만 호출되고 orchestrator는 lifecycle을 수동 관리하므로 그 훅이 안 fire됨.
    # 직접 create_task로 띄우면 _self_test가 sleep 후 자기 시점에 동작.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_self_test(app))
    except RuntimeError:
        # async context 밖 (예: CLI 단독 실행) — self-test 비활성
        pass
    return app
