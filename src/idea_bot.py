"""IdeaBot — 투자 아이디어 → 영업레버리지 Top 5 종목 추천.

사용 흐름:
  사용자 입력(텍스트 or /idea <text>)
    → (1) perplexity/sonar-pro 웹검색: 논리의 기울기 검증 + 산업 2-3 + 후보 30
    → (2) wisereport 산업 리포트 수집 (PIPELINE_LOCK)
    → (3) LLM narrow: 30 → 10 + all30_scored (영업레버리지 4축 점수)
        → 30종목 4축 산점도 PNG 발송
    → (4) wisereport 종목 리포트 수집 (PIPELINE_LOCK)
    → (5) LLM 최종 synthesis: Top 5 + 영업레버리지 thesis (JSON)
    → (6) 텔레그램 발송: 텍스트 메시지 + Top 5 참고 PDF + 공통 산업 PDF

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
    "*사용법:*\n"
    "  - 슬래시 없이 아이디어를 그냥 보내거나\n"
    "  - `/idea <아이디어 텍스트>`\n\n"
    "*제약 표현 지원* (자연어 그대로):\n"
    "  - `1조 이하 소부장 중 AI 데이터센터 수혜`\n"
    "  - `중소형 K-방산`\n"
    "  - `코스닥만, 5천억 이하 반도체 장비`\n\n"
    "*동작:*\n"
    "  🧭 0.5단계: 아이디어 파싱 (thesis + 시총/산업 제약 추출)\n"
    "  🌐 1단계: 웹 검색 + 30 후보 발굴 (제약 적용)\n"
    "  ⚖️ 1.5단계: 현상 중요도 비판적 평가\n"
    "  📊 2단계: 산업 리포트 다운로드\n"
    "  🎯 3단계: 영업레버리지 4축 점수로 30→10 + 산점도\n"
    "  📈 4단계: Top10 종목 리포트 다운로드\n"
    "  🧠 5단계: 사업부→폭→기울기 단계적 사고로 Top 5\n"
    "  🏆 6단계: Top 5 thesis + 종목 PDF + 산업 PDF\n\n"
    "_⏱️ 약 15-25분 소요_\n"
    "_프롬프트 수정: GitHub `prompts/idea_*.txt` 편집 후 push_"
)


# ------------------------------------------------------------------
# 핸들러
# ------------------------------------------------------------------
async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        if update.effective_chat:
            await update.message.reply_text(
                f"이 봇은 인가된 사용자만 사용 가능합니다.\nchat_id: `{update.effective_chat.id}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def _cmd_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
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
        return
    text = (update.message.text or "").strip()
    if not text or len(text) > 500:
        await update.message.reply_text(
            "아이디어를 1-500자 이내로 입력해주세요. /help 참고.",
        )
        return
    asyncio.create_task(_run_pipeline(update, context, text))


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
        )

        ended = datetime.now(KST).strftime("%H:%M")
        await send_text_chunked(
            bot, chat_id, f"✅ 완료 ({started} → {ended})",
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

    저렴한 모델(narrow와 동일) 사용. 실패 시 None — 호출자는 제약 없이 진행.
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
            resp = client.chat.completions.create(
                model=_narrow_model(),  # 저렴한 모델 OK — 단순 추출
                max_tokens=1000,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
        except Exception as e:
            _maybe_raise_credit(e)
            log.exception("아이디어 파싱 LLM 호출 실패")
            return None
        try:
            content = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.exception("파싱 응답 추출 실패")
            return None
        log.info("parse LLM 응답: %d chars — preview=%r", len(content), content[:300])
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
                max_tokens=6000,
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
                                days_back=60, industry_gics=code,
                            )
                        if not items and not general_pool_used:
                            # 폴백: 전체 industry 인기 (한 번만)
                            log.info("산업 '%s' 매칭 실패 → 일반 industry top reports 1회 사용", name)
                            items = cli.list_top_reports(
                                category="industry", sort_by="popular",
                                limit=INDUSTRY_REPORTS_PER_INDUSTRY * 2,  # 더 넉넉히
                                days_back=30,
                            )
                            general_pool_used = True
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
        try:
            resp = client.chat.completions.create(
                model=_narrow_model(),
                max_tokens=4500,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
        except summarizer.APIStatusError as e:
            if summarizer._is_credit_error(e):
                raise summarizer.OpenRouterCreditExhausted(str(e)) from e
            log.exception("narrow LLM API 에러")
            return None
        except Exception as e:
            _maybe_raise_credit(e)
            log.exception("narrow LLM 호출 실패")
            return None
        try:
            content = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.exception("narrow 응답 파싱 실패")
            return None
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
    """ticker6가 비어있으면 DART 회사명 룩업으로 채움."""
    loop = asyncio.get_running_loop()
    try:
        from src.deepdive import dart_client
    except Exception:
        log.exception("dart_client import 실패 — ticker 룩업 스킵")
        return top10

    out: list[dict] = []
    for c in top10:
        ticker = (c.get("ticker6") or "").strip()
        if re.match(r"^\d{6}$", ticker):
            out.append(c)
            continue
        name = (c.get("name") or "").strip()
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
                        continue
                    sub_dir = target_dir / ticker
                    try:
                        items = cli.list_reports(
                            ticker=ticker, sort_by="latest",
                            limit=COMPANY_REPORTS_PER_TICKER,
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
        try:
            resp = client.chat.completions.create(
                model=_synthesis_model(),
                max_tokens=8000,
                temperature=0.4,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
        except summarizer.APIStatusError as e:
            if summarizer._is_credit_error(e):
                raise summarizer.OpenRouterCreditExhausted(str(e)) from e
            log.exception("synthesis LLM API 에러")
            return None
        except Exception as e:
            _maybe_raise_credit(e)
            log.exception("synthesis LLM 호출 실패")
            return None
        try:
            content = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.exception("synthesis 응답 파싱 실패")
            return None
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
async def _send_results(
    bot: Bot, chat_id: str,
    synthesis: dict,
    industry_pdfs: list[Path],
    company_pdfs_by_ticker: dict[str, list[Path]],
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

        header = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🥇 Top {rank}. {name} ({ticker}) — {industry}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
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


# ------------------------------------------------------------------
# 헬퍼
# ------------------------------------------------------------------
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
    """LLM 응답에서 JSON 객체 추출. 코드 펜스 제거 + 첫 {...} 매칭.

    파싱 실패 시 None. 단, 부분적으로 잘린 JSON은 마지막 닫힘 위치까지 잘라서 재시도.
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
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError as e:
        log.warning("JSON 파싱 실패 (%s) — head=%r tail=%r", e, blob[:200], blob[-200:])
        return None
    if not isinstance(obj, dict):
        log.warning("JSON이 dict 아님: %s", type(obj))
        return None
    return obj


# ------------------------------------------------------------------
# Application 빌더 — orchestrator가 호출
# ------------------------------------------------------------------
def build_idea_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(CommandHandler("idea", _cmd_idea))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    return app
