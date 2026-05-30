"""EarningsBot — 미국 기업 어닝콜 전문 + 비교 PDF 보고서.

사용 흐름:
  사용자 입력
    → (0) parse: 회사 명시 vs 자연어 조건 vs 커스텀 분석만 (summary tier)
    → (1) criteria → tickers 확장 (research tier, 자연어 조건일 때만)
    → (2) 종목별 어닝콜 전문 fetch (research tier, perplexity 웹검색)
        → 텔레그램에 전문 요약 발송 (종목당 1청크씩)
    → (3) 종목별 SEC EDGAR 6년치 재무 fetch (CapEx/OCF/FCF/Revenue)
    → (4) Executive Summary 합성 (synthesis tier, 한국어 산업 분위기)
    → (5) 커스텀 질문이 있으면 답변 합성 (synthesis tier)
    → (6) PDF 빌드 + 텔레그램 발송

명령:
  /earnings <텍스트>  — 분석 시작
  슬래시 없이 그냥 입력해도 동작
  /help

모델 티어 (CLAUDE.md 규약):
  - summary    (OPENROUTER_MODEL)         — parse, transcript JSON parse
  - research   (IDEA_RESEARCH_MODEL)      — 어닝콜 웹검색, criteria 확장
  - synthesis  (IDEA_SYNTHESIS_MODEL)     — Executive Summary, 커스텀 분석

격리 (BOTS.md):
  - wisereport 미사용 (PIPELINE_LOCK 잡지 않음)
  - 외부 호출 모두 try/except로 봇 프로세스 보호
  - 신규 모듈, 기존 봇 파일 수정 안 함 (orchestrator만 1엔트리 추가)
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

from src.bot_helpers import (
    deny_message,
    download_root_for,
    is_authorized,
    safe_dirname,
    send_pdf,
    send_text_chunked,
)

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ALLOWED_ENV = "EARNINGS_ALLOWED_CHAT_IDS"
# 전용 봇이지만 EARNINGS_ALLOWED_CHAT_IDS 미설정 시 종목봇의 ALLOWED_CHAT_IDS 로 폴백
# — 별도 인가 설정 없이도 기존 사용자가 그대로 사용. (전체 공개는 둘 중 하나를 '*'로.)
FALLBACK_ALLOWED_ENV = "ALLOWED_CHAT_IDS"


def _is_authorized(update: Update) -> bool:
    """EARNINGS_ALLOWED_CHAT_IDS 우선, 없으면 ALLOWED_CHAT_IDS 폴백."""
    if os.getenv(ALLOWED_ENV, "").strip():
        return is_authorized(update, ALLOWED_ENV)
    return is_authorized(update, FALLBACK_ALLOWED_ENV)

HELP_TEXT = (
    "📞 *어닝콜 분석 봇*\n\n"
    "*기본 사용:*\n"
    "  • 그냥 텍스트를 입력하면 바로 분석 시작\n"
    "  • 또는 `/earnings <텍스트>`\n\n"
    "*입력 예시:*\n"
    "  • 회사 직접 지정 — `/earnings AAPL MSFT GOOGL NVDA의 2026 1Q 어닝콜`\n"
    "  • 자연어 조건 — `/earnings 빅테크 2026 1Q 실적발표`\n"
    "  • Capex 기준 — `/earnings capex 순 상위 6개`\n"
    "  • 커스텀 질문 — `/earnings MSFT GOOGL AMZN META 어닝콜에서 시장에 서프라이즈가 될 수 있는 요인 알려줘`\n\n"
    "*동작 단계:*\n"
    "  🧭 0단계: 입력 파싱 (회사 vs 조건 vs 분석 질문)\n"
    "  🌐 1단계: (조건 입력 시) 조건 → 티커 확장\n"
    "  📞 2단계: 종목별 어닝콜 전문 fetch → 텔레그램 즉시 발송\n"
    "  📊 3단계: SEC EDGAR로 6년치 CapEx/OCF/FCF/Revenue\n"
    "  📈 4단계: 비교 차트 6종 + 한국어 Executive Summary\n"
    "  🧠 5단계: 커스텀 질문 답변 합성 (있는 경우)\n"
    "  📄 6단계: PDF 보고서 발송\n\n"
    "*제한:*\n"
    "  • 미국 상장사만 (SEC EDGAR 기반). 한국 종목은 분석 불가.\n"
    "  • 최대 8개 기업까지 동시 분석 (4-6개 권장).\n"
    "  • 어닝콜 전문은 perplexity 웹검색 의존 — 발표 직후엔 미수록 가능.\n\n"
    "_⏱️ 약 5-10분 소요_"
)


# 진행 상태 (단일 사용자 전제 — 동시 다중 요청은 큐잉 없이 그냥 병렬)
_CURRENT_LOCK = asyncio.Lock()


# ------------------------------------------------------------------
# 핸들러
# ------------------------------------------------------------------
async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def _cmd_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    args = " ".join(context.args or []).strip()
    if not args:
        await update.message.reply_text(
            "사용법: `/earnings <텍스트>` 또는 슬래시 없이 그냥 입력",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    asyncio.create_task(_run_pipeline(update, context, args))


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("입력이 비어 있습니다. 사용법은 /help")
        return
    if len(text) > 1000:
        await update.message.reply_text(
            f"입력이 너무 깁니다 ({len(text)}자, 최대 1000자).\n"
            "회사·조건만 간결히 적어 주세요. 예: `AAPL MSFT GOOGL 2026 1Q`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    asyncio.create_task(_run_pipeline(update, context, text))


# ------------------------------------------------------------------
# 파이프라인
# ------------------------------------------------------------------
async def _run_pipeline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_text: str,
) -> None:
    """전체 어닝콜 분석 파이프라인. graceful — 외부 실패는 메시지로 알리고 계속."""
    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)

    await _safe_reply(update, "📞 어닝콜 분석 시작 — 0단계: 입력 파싱 …")

    # 0) 파싱
    parsed = await _step_parse(user_text)
    if parsed is None:
        await _safe_send(
            bot, chat_id,
            "⚠️ 입력을 이해하지 못했습니다.\n"
            "첫 줄에 회사명 또는 티커를 적어 주세요 (예: AAPL MSFT GOOGL).\n"
            "사용법: /help",
        )
        return

    mode = parsed.get("mode") or "custom_only"
    fiscal_period = parsed.get("fiscal_period")
    custom_question = (parsed.get("custom_question") or "").strip()
    fiscal_year = parsed.get("fiscal_year")
    fiscal_quarter = parsed.get("fiscal_quarter")

    tickers: list[str] = []
    if mode == "tickers":
        tickers = [t.upper() for t in (parsed.get("tickers") or []) if t]
        await _safe_send(
            bot, chat_id,
            f"🧭 회사 직접 지정 모드 — {', '.join(tickers) or '비어 있음'}\n"
            f"  분기: {fiscal_period or '최근 분기'}\n"
            + (f"  커스텀 질문: {custom_question}\n" if custom_question else ""),
        )
    elif mode == "criteria":
        criteria = (parsed.get("criteria") or user_text).strip()
        await _safe_send(bot, chat_id, f"🌐 1단계: 조건 → 티커 확장 (perplexity) …\n  조건: {criteria}")
        expansion = await _step_expand_criteria(criteria)
        if expansion is None:
            await _safe_send(bot, chat_id, "⚠️ 조건 → 티커 확장 실패. 회사를 직접 적어 주세요. (예: AAPL MSFT)")
            return
        rows = expansion.get("tickers") or []
        tickers = [(r.get("ticker") or "").upper() for r in rows if r.get("ticker")]
        if not tickers:
            await _safe_send(
                bot, chat_id,
                "⚠️ 조건에 맞는 미국 상장사를 찾지 못했습니다.\n"
                "(어닝콜봇은 SEC EDGAR 기반 — 미국 상장사만 분석합니다.)\n"
                + (f"\n💡 {expansion.get('exclusion_notes')}" if expansion.get("exclusion_notes") else ""),
            )
            return
        # 최대 8개
        tickers = tickers[:8]
        # 사용자에게 확장 결과 보여주기
        interp = expansion.get("interpreted_criteria") or criteria
        period_guess = expansion.get("target_fiscal_period")
        if period_guess and not fiscal_period:
            fiscal_period = period_guess
        lines = [f"  · 해석: {interp}"]
        for r in rows[:8]:
            reason = r.get("reason_for_inclusion") or ""
            lines.append(f"  · {r.get('ticker')} — {r.get('company_name', '')}: {reason}")
        await _safe_send(bot, chat_id, "🌐 확장 결과:\n" + "\n".join(lines))
    else:  # custom_only — 회사 미상
        await _safe_send(
            bot, chat_id,
            "⚠️ 분석 대상을 찾지 못했습니다.\n\n"
            "입력 예시:\n"
            "  • 회사 지정: AAPL MSFT GOOGL 2026 1Q\n"
            "  • 조건: 빅테크 2026 1Q / capex 상위 5\n"
            "  • 커스텀: MSFT GOOGL 어디가 경쟁 우위?\n\n"
            "사용법: /help",
        )
        return

    if not tickers:
        await _safe_send(bot, chat_id, "⚠️ 분석 대상 티커가 없습니다.")
        return

    # 분기 정수 정규화 — parse가 못 줬으면 fiscal_period 문자열에서 추출
    if not fiscal_year or not fiscal_quarter:
        from src.earnings.transcript_source import resolve_year_quarter
        ry, rq = resolve_year_quarter(fiscal_period)
        fiscal_year = fiscal_year or ry
        fiscal_quarter = fiscal_quarter or rq
    fiscal_label = fiscal_period or (
        f"Q{fiscal_quarter} {fiscal_year}" if (fiscal_year and fiscal_quarter) else "the most recent reported quarter"
    )

    # 3) SEC EDGAR 재무 (검증·차트·페이로드 모두에서 쓰이므로 먼저 수집)
    await _safe_send(bot, chat_id, "📊 SEC EDGAR로 6년치 + 분기 재무 수집 중 …")
    financials = await _step_fetch_financials(tickers)
    if not financials:
        await _safe_send(bot, chat_id, "⚠️ SEC EDGAR 데이터를 못 가져왔습니다 — 차트·검증 제한됩니다.")

    # 2+3) 종목별: 진짜 전문 확보 → 심층추출 → 숫자 교차검증
    transcripts: dict[str, dict] = {}
    verify_by_ticker: dict[str, list] = {}
    await _safe_send(
        bot, chat_id,
        f"📞 어닝콜 전문 확보 + 심층추출 시작 ({len(tickers)}개, {fiscal_label})\n"
        f"  진짜 전문에 grounding → 종목별 deep read (종목당 1-2분)",
    )
    for idx, t in enumerate(tickers, 1):
        await _safe_send(bot, chat_id, f"  ⏳ ({idx}/{len(tickers)}) {t} 전문 확보 + 심층 분석 중 …")
        tr = await _step_deep_extract(t, fiscal_year, fiscal_quarter, fiscal_label)
        if tr is None:
            await _safe_send(bot, chat_id, f"  ⚠️ ({idx}/{len(tickers)}) {t} 전문 확보 실패 — 스킵")
            continue
        # 숫자 교차검증 (SEC와 대조)
        vres = await _step_verify(tr, financials.get(t), fiscal_year, fiscal_quarter)
        verify_by_ticker[t] = vres
        tr["_verify"] = [v.message for v in vres]
        transcripts[t] = tr
        # 즉시 텔레그램 발송 (전문 심층요약 + 검증결과)
        from src.earnings.transcripts import format_transcript_text
        from src.earnings.verify import format_results
        body = format_transcript_text(tr)
        vtext = format_results(vres)
        if vtext:
            body += "\n\n" + vtext
        await send_text_chunked(bot, chat_id, body)

    if not transcripts:
        await _safe_send(bot, chat_id, "⚠️ 모든 종목의 전문 확보 실패. 잠시 후 재시도 권장.")
        return

    grounded_n = sum(1 for tr in transcripts.values() if tr.get("grounded"))
    await _safe_send(
        bot, chat_id,
        f"  ✅ {len(transcripts)}개 분석 완료 (전문 grounding {grounded_n}/{len(transcripts)})",
    )

    # 6) 비교 합성 (Opus, 딥리서치급)
    await _safe_send(bot, chat_id, "🧠 비교 합성 중 (Opus, 인용·시나리오 기반 8000자+)…")
    industry_summary = await _step_synthesize_industry(
        transcripts, financials, fiscal_label, verify_by_ticker,
    )
    if industry_summary:
        # 합성 결과엔 [TICKER] 인용·표·# 헤더가 있어 텔레그램 Markdown 파싱이 깨짐 → 평문 발송
        await send_text_chunked(bot, chat_id, industry_summary)
    else:
        industry_summary = "(비교 합성 실패 — PDF에는 차트·전문만 포함)"

    # 7) 커스텀 분석 (있을 때, Opus)
    custom_answer = ""
    if custom_question:
        await _safe_send(bot, chat_id, f"🎯 커스텀 질문 심층 답변 중 (Opus) — {custom_question[:80]}")
        custom_answer = await _step_synthesize_custom(
            custom_question, transcripts, financials, fiscal_label, verify_by_ticker,
        ) or ""
        if custom_answer:
            await send_text_chunked(bot, chat_id, "🎯 커스텀 분석\n\n" + custom_answer)

    # 8) PDF 빌드 + 발송
    await _safe_send(bot, chat_id, "📄 PDF 보고서 생성 중 …")
    pdf_path = await _step_build_pdf(
        tickers=list(transcripts.keys()),
        fiscal_period=fiscal_label,
        transcripts=transcripts,
        financials=financials,
        industry_summary=industry_summary,
        custom_question=custom_question,
        custom_answer=custom_answer,
        verify_by_ticker=verify_by_ticker,
    )
    if pdf_path and pdf_path.exists():
        await send_pdf(
            bot, chat_id, pdf_path,
            caption=f"📄 어닝콜 비교 분석 — {', '.join(tickers[:6])} · {fiscal_label}",
        )
        log.info("[earnings: send_results 완료] PDF=%s tickers=%s grounded=%d/%d",
                 pdf_path.name, tickers, grounded_n, len(transcripts))
    else:
        await _safe_send(bot, chat_id, "⚠️ PDF 생성 실패 — 텔레그램 텍스트만 확인해 주세요.")
        log.warning("[earnings: PDF 실패] tickers=%s", tickers)


# ------------------------------------------------------------------
# 단계별 헬퍼
# ------------------------------------------------------------------
async def _step_parse(user_text: str) -> dict | None:
    """0단계: 사용자 입력 → 모드/티커/분기/커스텀 질문 JSON."""
    from src import summarizer
    from src.idea_bot import _parse_json

    system = _load_prompt("earnings_parse")
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_summary_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=0.1,
            max_tokens=1500,
            context="earnings-parse",
        )

    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("parse LLM 호출 실패")
        return None
    if not content:
        return None
    return _parse_json(content)


async def _step_expand_criteria(criteria: str) -> dict | None:
    """1단계: 자연어 조건 → 미국 티커 리스트."""
    from src import summarizer
    from src.earnings import transcripts as trans
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return trans.expand_criteria_to_tickers(client, criteria)

    try:
        return await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("criteria 확장 실패")
        return None


async def _step_deep_extract(
    ticker: str, year: int | None, quarter: int | None, fiscal_label: str
) -> dict | None:
    """2+3단계: 진짜 전문 확보 → sonnet 심층추출."""
    from src import summarizer
    from src.earnings import transcripts as trans
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return trans.fetch_and_extract(
            client, ticker, year, quarter,
            extract_model=_extract_model(), fiscal_label=fiscal_label,
        )

    try:
        return await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("deep extract 실패 (%s)", ticker)
        return None


async def _step_verify(extract: dict, financials, year: int | None, quarter: int | None) -> list:
    """검증 단계: 추출 숫자 ↔ SEC 교차검증. list[VerifyResult]."""
    from src.earnings import verify
    loop = asyncio.get_running_loop()

    def _call():
        return verify.cross_check(extract, financials, year, quarter)

    try:
        return await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("검증 실패 (%s)", extract.get("ticker"))
        return []


async def _step_fetch_financials(tickers: list[str]) -> dict[str, Any]:
    """SEC EDGAR 재무 (FY 6년 + 분기). {ticker: CompanyFinancials}.

    종목별 병렬 fetch — sec_edgar._throttle (전역 스레드락, ≈6.6 req/s) 가 SEC 정책 보장.
    """
    from src.earnings import sec_edgar
    loop = asyncio.get_running_loop()
    results = await asyncio.gather(
        *[loop.run_in_executor(None, sec_edgar.fetch_company_financials, t, 6) for t in tickers],
        return_exceptions=True,
    )
    out: dict[str, Any] = {}
    for t, fin in zip(tickers, results):
        if isinstance(fin, Exception):
            log.warning("SEC fetch 실패 (%s): %s", t, fin)
            continue
        if fin is not None:
            out[t] = fin
    return out


async def _step_synthesize_industry(
    transcripts: dict[str, dict],
    financials: dict[str, Any],
    fiscal_period: str,
    verify_by_ticker: dict[str, list],
) -> str | None:
    """비교 합성 (Opus, 딥리서치급)."""
    from src import summarizer
    system = _load_prompt("earnings_synthesis")
    user_payload = _build_synthesis_payload(transcripts, financials, fiscal_period, verify_by_ticker)
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_synthesis_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.3,
            max_tokens=12000,
            context="earnings-synthesis",
        )

    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("비교 합성 실패")
        return None
    return content or None


async def _step_synthesize_custom(
    question: str,
    transcripts: dict[str, dict],
    financials: dict[str, Any],
    fiscal_period: str,
    verify_by_ticker: dict[str, list],
) -> str | None:
    """커스텀 분석 답변 합성 (Opus)."""
    from src import summarizer
    system = _load_prompt("earnings_custom")
    user_payload = (
        f"# 사용자 질문\n{question}\n\n"
        + _build_synthesis_payload(transcripts, financials, fiscal_period, verify_by_ticker)
    )
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_synthesis_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.4,
            max_tokens=10000,
            context="earnings-custom",
        )

    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("커스텀 분석 합성 실패")
        return None
    return content or None


async def _step_build_pdf(
    *,
    tickers: list[str],
    fiscal_period: str,
    transcripts: dict[str, dict],
    financials: dict[str, Any],
    industry_summary: str,
    custom_question: str,
    custom_answer: str,
    verify_by_ticker: dict[str, list],
) -> Path | None:
    """PDF 빌드 (blocking → run_in_executor)."""
    from src.earnings import pdf_report
    from src.earnings import verify as verify_mod
    loop = asyncio.get_running_loop()

    out_dir = download_root_for("earnings")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    safe_tag = safe_dirname("_".join(tickers[:4]) or "earnings")
    out_path = out_dir / f"earnings_{safe_tag}_{stamp}.pdf"

    # 검증 결과를 텍스트로 평탄화 (PDF에 표시)
    verify_lines: list[str] = []
    for t in tickers:
        res = verify_by_ticker.get(t) or []
        for r in res:
            verify_lines.append(r.message)

    def _build():
        return pdf_report.build_pdf(
            out_path,
            tickers=tickers,
            fiscal_period=fiscal_period,
            transcripts=transcripts,
            financials_by_ticker=financials,
            industry_summary_kr=industry_summary,
            custom_question=custom_question,
            custom_answer_kr=custom_answer,
            verify_lines=verify_lines,
        )

    try:
        return await loop.run_in_executor(None, _build)
    except Exception:
        log.exception("PDF 빌드 최상위 실패")
        return None


# ------------------------------------------------------------------
# 합성용 페이로드 빌더
# ------------------------------------------------------------------
def _build_synthesis_payload(
    transcripts: dict[str, dict],
    financials: dict[str, Any],
    fiscal_period: str,
    verify_by_ticker: dict[str, list] | None = None,
) -> str:
    """LLM에 넘길 유저 메시지. 종목별 심층추출 + 재무 6년치 + 검증결과."""
    from src.earnings.sec_edgar import fmt_usd
    verify_by_ticker = verify_by_ticker or {}
    parts: list[str] = []
    parts.append(f"# 분기: {fiscal_period}")
    parts.append(f"# 분석 기업: {', '.join(transcripts.keys())}")
    grounded_n = sum(1 for tr in transcripts.values() if tr.get("grounded"))
    parts.append(f"# 전문 grounding: {grounded_n}/{len(transcripts)} (grounded=True인 종목만 verbatim 신뢰)")
    parts.append("")

    # 어닝콜 심층추출 (종목별)
    parts.append("## 어닝콜 심층추출 (종목별)")
    for t, tr in transcripts.items():
        gflag = "grounded" if tr.get("grounded") else "⚠️요약(비grounding)"
        parts.append(f"### {t} — {tr.get('company_name', '')} [{gflag}, 출처 {tr.get('source','?')}]")
        hn = tr.get("headline_numbers") or {}
        if hn:
            parts.append("Headline: " + ", ".join(f"{k}={v}" for k, v in hn.items() if v))
        seg = tr.get("segment_performance") or []
        for item in seg[:8]:
            parts.append(f"Segment [{item.get('segment','?')}]: {item.get('metric','')} — {item.get('detail','')}")
        md = tr.get("management_discussion") or []
        for item in md[:12]:
            sp = item.get("speaker") or "?"
            tp = item.get("topic") or ""
            qt = item.get("quote") or ""
            parts.append(f"- [{sp} / {tp}] \"{qt}\"")
        g = tr.get("guidance") or {}
        if g:
            parts.append("Guidance: " + ", ".join(f"{k}={v}" for k, v in g.items() if v))
        qa = tr.get("qa_highlights") or []
        for item in qa[:10]:
            parts.append(f"Q ({item.get('analyst', '?')}): {item.get('question', '')}")
            parts.append(f"A: {item.get('answer_summary', '')}")
        if tr.get("capital_allocation"):
            parts.append(f"CapitalAllocation: {tr.get('capital_allocation')}")
        if tr.get("management_tone"):
            parts.append(f"Tone: {tr.get('management_tone')}")
        sr = tr.get("surprises_and_risks") or []
        for item in sr:
            parts.append(f"⚡ {item}")
        nv = tr.get("notable_verbatim") or []
        for item in nv[:5]:
            parts.append(f"Verbatim: \"{item}\"")
        # 검증 결과
        vres = verify_by_ticker.get(t) or []
        for r in vres:
            parts.append(f"[verify] {r.message}")
        parts.append("")

    # 재무 6년치
    parts.append("## 재무 (SEC EDGAR 10-K FY)")
    for t, fin in financials.items():
        parts.append(f"### {t}")
        rev_by = {p.fy: p.val for p in fin.revenue}
        cap_by = {p.fy: p.val for p in fin.capex}
        ocf_by = {p.fy: p.val for p in fin.ocf}
        fcf_by = {p.fy: p.val for p in fin.fcf()}
        fys = sorted(set(list(cap_by.keys()) + list(ocf_by.keys()) + list(rev_by.keys())))[-6:]
        parts.append("FY | Revenue | CapEx | OCF | FCF | OCF/CapEx | CapEx YoY")
        prev_cap = None
        for fy in fys:
            rev = fmt_usd(rev_by[fy]) if fy in rev_by else "—"
            cap = cap_by.get(fy)
            ocf = ocf_by.get(fy)
            fcf = fcf_by.get(fy)
            cap_s = fmt_usd(cap) if cap is not None else "—"
            ocf_s = fmt_usd(ocf) if ocf is not None else "—"
            fcf_s = fmt_usd(fcf) if fcf is not None else "—"
            ratio = f"{ocf/cap:.2f}x" if (ocf is not None and cap and cap > 0) else "—"
            if prev_cap and cap and prev_cap > 0:
                yoy = f"{(cap - prev_cap) / prev_cap * 100:+.1f}%"
            else:
                yoy = "—"
            parts.append(f"FY{fy} | {rev} | {cap_s} | {ocf_s} | {fcf_s} | {ratio} | {yoy}")
            if cap is not None:
                prev_cap = cap
        parts.append("")

    return "\n".join(parts)


# ------------------------------------------------------------------
# 모델 티어 / 프롬프트 / 메시지 헬퍼
# ------------------------------------------------------------------
def _summary_model() -> str:
    """0단계 parse — 갓성비 (kimi)."""
    return os.getenv("OPENROUTER_MODEL") or "moonshotai/kimi-k2.6"


def _extract_model() -> str:
    """종목별 전문 심층추출 — sonnet (긴 입력 정밀 읽기, 비용 절충).

    EARNINGS_EXTRACT_MODEL > IDEA_NARROW_MODEL > sonnet 기본.
    """
    return (
        os.getenv("EARNINGS_EXTRACT_MODEL")
        or os.getenv("IDEA_NARROW_MODEL")
        or "anthropic/claude-sonnet-4.5"
    )


def _synthesis_model() -> str:
    """최종 비교 합성 + 커스텀 분석 — Opus (딥리서치급 추론).

    EARNINGS_SYNTHESIS_MODEL > IDEA_SYNTHESIS_MODEL > opus 기본.
    """
    return (
        os.getenv("EARNINGS_SYNTHESIS_MODEL")
        or os.getenv("IDEA_SYNTHESIS_MODEL")
        or "anthropic/claude-opus-4.7"
    )


def _load_prompt(name: str) -> str:
    """prompts/<name>.txt 로드. 실패 시 빈 string."""
    path = Path(__file__).resolve().parent.parent / "prompts" / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        log.warning("프롬프트 파일 로드 실패: %s", path)
        return ""


async def _safe_reply(update: Update, text: str) -> None:
    try:
        if update.message:
            await update.message.reply_text(text)
    except Exception:
        log.exception("reply 실패")


async def _safe_send(bot: Bot, chat_id: str, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        log.exception("send_message 실패 (chat_id=%s)", chat_id)


# ------------------------------------------------------------------
# self-test + builder
# ------------------------------------------------------------------
async def _self_test(app: Application) -> None:
    test_prompt = (os.getenv("EARNINGS_TEST_PROMPT") or "").strip()
    if not test_prompt:
        return
    chat_id = (
        os.getenv("EARNINGS_TEST_CHAT_ID")
        or os.getenv("EARNINGS_CHAT_ID")
        or os.getenv("TELEGRAM_CHAT_ID")
        or (os.getenv(ALLOWED_ENV, "").split(",") + [""])[0].strip()
        or (os.getenv(FALLBACK_ALLOWED_ENV, "").split(",") + [""])[0].strip()
    )
    if not chat_id:
        log.warning("[earnings self-test] chat_id 없음 — 스킵")
        return

    log.info("=" * 60)
    log.info("[earnings self-test] 시작 — chat_id=%s prompt=%r", chat_id, test_prompt)
    log.info("=" * 60)

    class _FakeChat:
        def __init__(self, cid: str) -> None:
            self.id = int(cid)

    class _FakeMessage:
        def __init__(self, cid: str, text: str) -> None:
            self.text = text
            self.chat = _FakeChat(cid)

        async def reply_text(self, *a, **kw) -> None:
            try:
                await app.bot.send_message(
                    chat_id=self.chat.id,
                    text=str(a[0]) if a else (kw.get("text") or ""),
                )
            except Exception:
                log.exception("[earnings self-test] reply 실패")

    class _FakeUpdate:
        def __init__(self, cid: str, text: str) -> None:
            self.effective_chat = _FakeChat(cid)
            self.message = _FakeMessage(cid, text)

    class _FakeContext:
        def __init__(self) -> None:
            self.bot = app.bot
            self.args: list[str] = []

    await asyncio.sleep(15)
    try:
        await _run_pipeline(_FakeUpdate(chat_id, test_prompt), _FakeContext(), test_prompt)
    except Exception:
        log.exception("[earnings self-test] 최상위 예외")
    log.info("[earnings self-test] 종료")


EARNINGS_COMMANDS = [
    ("earnings", "📞 미국 기업 어닝콜 + 비교 PDF (5-10분)"),
    ("help", "ℹ️ 사용법"),
]


def build_earnings_app(token: str) -> Application:
    """전용 EarningsBot Application 빌더 (orchestrator BOT_SPECS에서 호출).

    핸들러: /start·/help · /earnings · 자유 텍스트(_on_text)로도 바로 진입.
    EARNINGS_TEST_PROMPT env가 있으면 부팅 후 1회 self-test 자동 실행 (CLAUDE.md 검증 의무).
    """
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(CommandHandler("earnings", _cmd_earnings))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_self_test(app))
    except RuntimeError:
        pass
    return app
