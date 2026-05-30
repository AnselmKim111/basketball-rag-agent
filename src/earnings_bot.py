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


async def _cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backfill <TICKER> [N] — 한 종목에 대해 직전 N분기 longitudinal history 시드.

    매 분기당 흐름: transcripts.fetch_and_extract → history.save_call. 컨센서스는 fetch 안 함
    (과거 컨센은 무의미). AV 25 req/day 제한 안에서. asyncio.create_task로 분리해 즉시 응답.
    """
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/backfill <TICKER> [N=4]`\n예: `/backfill MSFT 6`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ticker = args[0].upper().strip()
    n = 4
    if len(args) > 1:
        try:
            n = max(1, min(int(args[1]), 12))  # 1-12 분기 cap (AV 25/day)
        except ValueError:
            await update.message.reply_text("N은 정수. 예: `/backfill MSFT 6`")
            return
    asyncio.create_task(_run_backfill(update, context, ticker, n))


async def _run_backfill(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ticker: str, n: int
) -> None:
    """직전 N분기 시퀀셜 backfill 워커. AV rate-limit 대응 — 실패 1건은 skip하고 계속."""
    from datetime import datetime as _dt
    from src import summarizer
    from src.earnings import history, transcripts as trans

    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)

    # 현재 시점에서 직전 N분기 시퀀스 도출 (calendar quarter 기준)
    today = _dt.utcnow()
    cur_y = today.year
    cur_q = (today.month - 1) // 3 + 1  # 1-4
    # 직전 분기부터 거꾸로 N개 (이번 분기는 미보고 가능성 ↑ → 직전부터)
    seq: list[tuple[int, int]] = []
    y, q = cur_y, cur_q
    for _ in range(n):
        q -= 1
        if q == 0:
            q = 4
            y -= 1
        seq.append((y, q))

    await _safe_send(
        bot, chat_id,
        f"🔁 /backfill {ticker} {n}분기 — 시퀀스: " + ", ".join(f"{yy}Q{qq}" for yy, qq in seq),
    )

    extract_model = _extract_model()
    success = 0
    failed: list[str] = []
    skipped_already: list[str] = []
    loop = asyncio.get_running_loop()
    client = summarizer.get_client()

    for idx, (yy, qq) in enumerate(seq, 1):
        # 이미 저장돼 있으면 skip
        if history.load_call(ticker, yy, qq):
            skipped_already.append(f"{yy}Q{qq}")
            await _safe_send(bot, chat_id, f"  ⏭️ ({idx}/{n}) {ticker} {yy}Q{qq} 이미 캐시 — 스킵")
            continue
        await _safe_send(bot, chat_id, f"  ⏳ ({idx}/{n}) {ticker} {yy}Q{qq} 전문 확보 + 추출 …")
        fiscal_label = f"Q{qq} {yy}"
        try:
            tr = await loop.run_in_executor(
                None,
                lambda yy=yy, qq=qq, fl=fiscal_label: trans.fetch_and_extract(
                    client, ticker, yy, qq, extract_model=extract_model, fiscal_label=fl,
                ),
            )
        except Exception:
            log.exception("[backfill] fetch_and_extract 실패 %s %sQ%s", ticker, yy, qq)
            tr = None
        if tr is None or tr.get("extract_failed"):
            failed.append(f"{yy}Q{qq}")
            await _safe_send(bot, chat_id, f"  ⚠️ ({idx}/{n}) {ticker} {yy}Q{qq} 실패 — 계속")
            continue
        try:
            history.save_call(
                ticker=ticker, year=yy, quarter=qq, fiscal_label=fiscal_label,
                extract=tr, consensus=None, consensus_deltas=[], verify_results=[],
                synthesis_excerpt="", counter_excerpt="",
            )
            success += 1
        except Exception:
            log.exception("[backfill] save_call 실패 %s %sQ%s", ticker, yy, qq)
            failed.append(f"{yy}Q{qq}(save)")

    msg = (
        f"✅ /backfill {ticker} 완료 — 신규 {success} · 스킵(기존) {len(skipped_already)} · "
        f"실패 {len(failed)}\n"
        f"  · 시드 분기: " + (", ".join(f"{y}Q{q}" for y, q in seq) or "—") + "\n"
        + (f"  · 실패 분기: {', '.join(failed)}\n" if failed else "")
        + "이후 이 종목 분석부터 분기간 diff·hyperscaler walker가 풍부해집니다."
    )
    await _safe_send(bot, chat_id, msg)


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

    # 3) SEC EDGAR 재무 + 컨센서스 + 생태계 맵 + Form 4 + 8-K + 10-K Risk diff
    #    (먼저 6-track 병렬 수집)
    await _safe_send(bot, chat_id, "📊 SEC + 컨센 + 생태계 + 인사이더 + 8-K + 10-K Risk 6-track 수집 중 …")
    (
        financials, consensus_by_ticker, ecosystem_by_ticker,
        insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
    ) = await asyncio.gather(
        _step_fetch_financials(tickers),
        _step_fetch_consensus(tickers, fiscal_label),
        _step_fetch_ecosystem(tickers),
        _step_fetch_insider(tickers, window_days=30),
        _step_fetch_8k(tickers, window_days=90),
        _step_fetch_risk_diff(tickers),
    )
    if not financials:
        await _safe_send(bot, chat_id, "⚠️ SEC EDGAR 데이터를 못 가져왔습니다 — 차트·검증 제한됩니다.")
    if consensus_by_ticker:
        got_c = sum(1 for v in consensus_by_ticker.values() if v is not None)
        got_e = sum(1 for v in (ecosystem_by_ticker or {}).values() if v is not None)
        got_i = sum(1 for v in (insider_by_ticker or {}).values() if v is not None)
        got_k = sum(1 for v in (events_8k_by_ticker or {}).values() if v is not None)
        got_r = sum(
            1 for v in (risk_diff_by_ticker or {}).values()
            if v and not v.get("diff_skipped")
        )
        await _safe_send(
            bot, chat_id,
            f"  📐 컨센 {got_c}/{len(tickers)} · 🌐 생태계 {got_e}/{len(tickers)} · "
            f"👥 인사이더 {got_i}/{len(tickers)} · 📂 8-K {got_k}/{len(tickers)} · "
            f"📜 10-K Risk {got_r}/{len(tickers)}"
        )

    # 2+3) 종목별: 진짜 전문 확보 → 심층추출 → 숫자 교차검증 + 컨센서스 delta + 분기간 diff
    transcripts: dict[str, dict] = {}
    verify_by_ticker: dict[str, list] = {}
    consensus_delta_by_ticker: dict[str, list] = {}
    diff_by_ticker: dict[str, dict] = {}
    # risk_diff_by_ticker는 위 6-track gather에서 채워짐 (Track I)
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
        # 컨센서스 delta (beat/miss/inline)
        snap = (consensus_by_ticker or {}).get(t)
        if snap is not None:
            from src.earnings.verify import compute_consensus_delta
            deltas = compute_consensus_delta(tr, snap)
            consensus_delta_by_ticker[t] = deltas
            tr["_consensus_deltas"] = [d.message for d in deltas]
            tr["_consensus_snap"] = snap   # synthesis payload용 (직렬화는 따로)
        transcripts[t] = tr
        # 분기간 diff (Track C — history에 이전 분기 있을 때만)
        diff = await _step_compute_diff(t, tr, fiscal_year, fiscal_quarter, prior_n=4)
        if diff:
            diff_by_ticker[t] = diff
            tr["_diff"] = diff
        # 즉시 텔레그램 발송 (전문 심층요약 + 검증결과 + 컨센 delta + 컨센 snapshot + diff 요약)
        from src.earnings.transcripts import format_transcript_text
        from src.earnings.verify import format_results, format_consensus_deltas
        from src.earnings.consensus import fmt_consensus_text
        body = format_transcript_text(tr)
        vtext = format_results(vres)
        if vtext:
            body += "\n\n" + vtext
        if snap is not None:
            ctext = format_consensus_deltas(consensus_delta_by_ticker.get(t) or [])
            if ctext:
                body += "\n\n" + ctext
            body += "\n\n" + fmt_consensus_text(snap)
        if diff:
            body += "\n\n" + _format_diff_text(diff)
        # 생태계 맵 (Track D)
        eco = (ecosystem_by_ticker or {}).get(t)
        if eco:
            from src.earnings.ecosystem import fmt_ecosystem_text
            body += "\n\n" + fmt_ecosystem_text(t, eco)
        # 인사이더 매매 (Track F)
        insider = (insider_by_ticker or {}).get(t)
        if insider is not None:
            from src.earnings.sec_edgar import fmt_insider_summary
            itxt = fmt_insider_summary(insider)
            if itxt:
                body += "\n\n" + itxt
        # NLP 시그널 (Track K — 정규식, LLM 0회)
        sig = tr.get("_signals")
        if sig:
            from src.earnings.nlp_signals import fmt_signals_text
            body += "\n\n" + fmt_signals_text(sig)
        # 8-K 머티어리얼 이벤트 (Track H)
        events_8k = (events_8k_by_ticker or {}).get(t)
        if events_8k is not None:
            from src.earnings.sec_edgar import fmt_8k_summary
            etxt = fmt_8k_summary(events_8k)
            if etxt:
                body += "\n\n" + etxt
        # 10-K Risk diff (Track I)
        rd = (risk_diff_by_ticker or {}).get(t)
        if rd:
            rdtxt = _fmt_risk_diff_text(rd)
            if rdtxt:
                body += "\n\n" + rdtxt
        await send_text_chunked(bot, chat_id, body)

    if not transcripts:
        await _safe_send(bot, chat_id, "⚠️ 모든 종목의 전문 확보 실패. 잠시 후 재시도 권장.")
        return

    grounded_n = sum(1 for tr in transcripts.values() if tr.get("grounded"))
    await _safe_send(
        bot, chat_id,
        f"  ✅ {len(transcripts)}개 분석 완료 (전문 grounding {grounded_n}/{len(transcripts)})",
    )

    # 5.7) Hyperscaler capex 모자이크 walker (Track L — 외부 fetch 0회, history만)
    hyper_walker = _build_hyperscaler_capex_table(
        quarters=4,
        ecosystem_by_ticker=ecosystem_by_ticker,
        analyzed_tickers=list(transcripts.keys()),
    )
    if hyper_walker.get("applicable"):
        await _safe_send(bot, chat_id, fmt_hyperscaler_text(hyper_walker))

    # 6) 비교 합성 (Opus, 딥리서치급 — 컨센서스 delta·정성 서프라이즈 강제 인용)
    await _safe_send(bot, chat_id, "🧠 비교 합성 중 (Opus, 인용·시나리오 기반 8000자+)…")
    industry_summary = await _step_synthesize_industry(
        transcripts, financials, fiscal_label, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker,
    )
    if industry_summary:
        # 합성 결과엔 [TICKER] 인용·표·# 헤더가 있어 텔레그램 Markdown 파싱이 깨짐 → 평문 발송
        await send_text_chunked(bot, chat_id, industry_summary)
    else:
        industry_summary = "(비교 합성 실패 — PDF에는 차트·전문만 포함)"

    # 6.5) Counter-thesis 자동 합성 (Track E) — 동일 payload + opus 1회 추가
    await _safe_send(bot, chat_id, "🛡️ Counter-thesis 자동 반박 합성 중 (Opus)…")
    counter_summary = await _step_synthesize_counter(
        transcripts, financials, fiscal_label, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker,
    )
    if counter_summary:
        await send_text_chunked(bot, chat_id, counter_summary)
    else:
        counter_summary = ""

    # 6.6) longitudinal history 영속화 (Track C) — 합성·counter 끝난 시점에 1회.
    if fiscal_year and fiscal_quarter:
        from src.earnings import history
        for t, tr in transcripts.items():
            try:
                history.save_call(
                    ticker=t,
                    year=fiscal_year,
                    quarter=fiscal_quarter,
                    fiscal_label=fiscal_label,
                    extract=tr,
                    consensus=consensus_by_ticker.get(t),
                    consensus_deltas=consensus_delta_by_ticker.get(t),
                    verify_results=verify_by_ticker.get(t),
                    synthesis_excerpt=industry_summary or "",
                    counter_excerpt=counter_summary,
                )
            except Exception:
                log.exception("[earnings/history] save_call 실패 (%s)", t)

    # 7) 커스텀 분석 (있을 때, Opus)
    custom_answer = ""
    if custom_question:
        await _safe_send(bot, chat_id, f"🎯 커스텀 질문 심층 답변 중 (Opus) — {custom_question[:80]}")
        custom_answer = await _step_synthesize_custom(
            custom_question, transcripts, financials, fiscal_label, verify_by_ticker,
            consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
            ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
            hyper_walker=hyper_walker,
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


async def _step_fetch_risk_diff(tickers: list[str]) -> dict[str, Any]:
    """티커별 10-K Item 1A Risk Factor 1년 diff (Track I).

    1단계: 최신 + 1년 전 10-K Item 1A 텍스트 병렬 fetch (SEC HTML).
    2단계: 페어가 둘 다 있는 종목에 대해 sonnet 1회 diff JSON.
    실패는 graceful — 그 종목은 risk_diff_by_ticker[t] = None.
    """
    from src import summarizer
    from src.earnings import sec_edgar
    from src.idea_bot import _parse_json
    loop = asyncio.get_running_loop()

    snaps = await asyncio.gather(
        *[loop.run_in_executor(None, sec_edgar.fetch_10k_risk_factors, t) for t in tickers],
        return_exceptions=True,
    )

    system = _load_prompt("earnings_risk_diff")
    out: dict[str, Any] = {}
    model = _extract_model()

    async def _diff_one(ticker: str, current, prior) -> dict | None:
        if current is None or prior is None:
            return None
        user_msg = (
            f"# ticker: {ticker}\n"
            f"# current_filed: {current.filed} (chars: {current.risk_chars:,}, sent: {len(current.risk_text):,})\n"
            f"# prior_filed: {prior.filed} (chars: {prior.risk_chars:,}, sent: {len(prior.risk_text):,})\n\n"
            f"=== CURRENT 10-K Item 1A START ===\n{current.risk_text}\n=== END ===\n\n"
            f"=== PRIOR 10-K Item 1A START ===\n{prior.risk_text}\n=== END ===\n\n"
            "위 두 텍스트의 1년간 위험 진화를 schema 그대로 JSON으로 출력하세요."
        )
        def _call():
            client = summarizer.get_client()
            return summarizer.chat_with_retry(
                client,
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                max_tokens=4000,
                context=f"earnings-risk-diff:{ticker}",
            )
        try:
            content = await loop.run_in_executor(None, _call)
        except Exception:
            log.exception("risk diff LLM 실패 (%s)", ticker)
            return None
        return _parse_json(content) if content else None

    diff_jobs = []
    for t, snap_pair in zip(tickers, snaps):
        if isinstance(snap_pair, Exception):
            log.warning("10-K fetch 실패 (%s): %s", t, snap_pair)
            out[t] = None
            continue
        current, prior = snap_pair
        if current is None or prior is None:
            # 페어 둘 다 있어야 의미 — fetch 상태만 메타로 보관
            out[t] = {
                "ticker": t,
                "diff_skipped": True,
                "reason": (
                    "최신·이전 10-K Item 1A 페어 미확보 (추출 실패 또는 신규 상장)"
                ),
                "current_meta": ({"filed": current.filed, "chars": current.risk_chars} if current else None),
                "prior_meta": ({"filed": prior.filed, "chars": prior.risk_chars} if prior else None),
            }
            continue
        diff_jobs.append((t, current, prior))

    # 2단계: 페어 있는 종목 sonnet 병렬
    if diff_jobs:
        results = await asyncio.gather(*[_diff_one(t, c, p) for t, c, p in diff_jobs])
        for (t, current, prior), res in zip(diff_jobs, results):
            payload = res or {}
            payload.setdefault("ticker", t)
            payload.setdefault("current_filed", current.filed)
            payload.setdefault("prior_filed", prior.filed)
            payload["_current_meta"] = {"filed": current.filed, "chars": current.risk_chars}
            payload["_prior_meta"] = {"filed": prior.filed, "chars": prior.risk_chars}
            out[t] = payload
    return out


def _fmt_risk_diff_text(rd: dict | None) -> str:
    """텔레그램용 한국어 요약."""
    if not rd:
        return ""
    ticker = rd.get("ticker", "?")
    if rd.get("diff_skipped"):
        return f"📜 10-K Risk diff({ticker}): 추출 실패 — {rd.get('reason','')}"
    lines = [
        f"📜 10-K Risk diff({ticker}) — "
        f"최신 {rd.get('current_filed','?')} vs 이전 {rd.get('prior_filed','?')}"
    ]
    def _add(label, key, max_items=3):
        items = rd.get(key) or []
        if not items:
            return
        lines.append(f"  · {label} ({len(items)}건):")
        for it in items[:max_items]:
            t = (it.get("topic") or "?")[:60]
            lines.append(f"    - {t}")
    _add("🆕 신규 위험", "new_risks")
    _add("🗑️ 사라진 위험", "removed_risks")
    _add("🔺 강화", "intensified_risks")
    _add("🔻 완화", "softened_risks")
    _add("🔢 새 정량화", "new_quantification")
    if rd.get("pm_takeaway"):
        lines.append(f"  · PM takeaway: {rd.get('pm_takeaway')}")
    return "\n".join(lines)


async def _step_fetch_8k(tickers: list[str], window_days: int = 90) -> dict[str, Any]:
    """티커별 최근 N일 8-K material events (Track H). 메타 fetch 후 가장 의미 있는 항목
    상위 3건만 sonnet 1줄 요약 (비용 가드: 종목당 ~$0.03 max)."""
    from src.earnings import sec_edgar
    loop = asyncio.get_running_loop()
    # 1단계: 모든 종목 8-K 메타 병렬 fetch
    metas = await asyncio.gather(
        *[loop.run_in_executor(None, sec_edgar.fetch_recent_8k, t, window_days, True) for t in tickers],
        return_exceptions=True,
    )
    out: dict[str, Any] = {}
    summary_jobs: list[tuple[str, Any]] = []  # (ticker, event) 페어 — 요약 대상
    for t, meta in zip(tickers, metas):
        if isinstance(meta, Exception):
            log.warning("8-K fetch 실패 (%s): %s", t, meta)
            out[t] = None
            continue
        out[t] = meta
        if meta is None:
            continue
        # 2.02 (earnings release) 는 어차피 우리가 콜로 분석 중이므로 요약 우선순위 ↓
        prioritized = sorted(
            meta.events,
            key=lambda e: (0 if any(c != "2.02" for c in e.items) else 1, e.filed),
            reverse=True,  # 최신 + non-2.02 우선
        )
        for ev in prioritized[:3]:
            summary_jobs.append((t, ev))
    # 2단계: 우선순위 상위 이벤트만 sonnet 1줄 요약 (모두 병렬)
    if summary_jobs:
        from src.earnings import sec_edgar as _sec
        model = _extract_model()
        summaries = await asyncio.gather(
            *[loop.run_in_executor(None, _sec.summarize_event_8k, ev, tk, model) for tk, ev in summary_jobs],
            return_exceptions=True,
        )
        for (tk, ev), s in zip(summary_jobs, summaries):
            if isinstance(s, Exception):
                continue
            ev.summary_line = s
    return out


async def _step_fetch_ecosystem(tickers: list[str]) -> dict[str, Any]:
    """티커별 4-bucket 생태계 맵 (Track D). 캐시 hit이면 instant, miss면 perplexity 1회/종목."""
    from src.earnings import ecosystem
    loop = asyncio.get_running_loop()
    results = await asyncio.gather(
        *[loop.run_in_executor(None, ecosystem.get_ecosystem, t, True) for t in tickers],
        return_exceptions=True,
    )
    out: dict[str, Any] = {}
    for t, eco in zip(tickers, results):
        if isinstance(eco, Exception):
            log.warning("ecosystem fetch 실패 (%s): %s", t, eco)
            out[t] = None
            continue
        out[t] = eco
    return out


async def _step_fetch_insider(tickers: list[str], window_days: int = 30) -> dict[str, Any]:
    """티커별 최근 N일 Form 4 인사이더 매매 메타 (Track F). SEC 무료, 같은 USER_AGENT."""
    from src.earnings import sec_edgar
    loop = asyncio.get_running_loop()
    results = await asyncio.gather(
        *[loop.run_in_executor(None, sec_edgar.fetch_recent_form4, t, window_days) for t in tickers],
        return_exceptions=True,
    )
    out: dict[str, Any] = {}
    for t, s in zip(tickers, results):
        if isinstance(s, Exception):
            log.warning("Form 4 fetch 실패 (%s): %s", t, s)
            out[t] = None
            continue
        out[t] = s
    return out


async def _step_fetch_consensus(tickers: list[str], fiscal_label: str | None = None) -> dict[str, Any]:
    """sell-side 컨센서스 병렬 fetch — Yahoo Finance → perplexity 폴백.

    {ticker: ConsensusSnapshot | None}. Yahoo는 자체 cookie+crumb dance + 1회/일 정도면
    무료. 실패는 graceful — 그 종목의 컨센 delta가 unavailable로 표시됨.
    """
    from src.earnings import consensus
    loop = asyncio.get_running_loop()
    results = await asyncio.gather(
        *[loop.run_in_executor(None, consensus.fetch_consensus, t, fiscal_label) for t in tickers],
        return_exceptions=True,
    )
    out: dict[str, Any] = {}
    for t, snap in zip(tickers, results):
        if isinstance(snap, Exception):
            log.warning("consensus fetch 실패 (%s): %s", t, snap)
            out[t] = None
            continue
        out[t] = snap
    return out


async def _step_synthesize_industry(
    transcripts: dict[str, dict],
    financials: dict[str, Any],
    fiscal_period: str,
    verify_by_ticker: dict[str, list],
    consensus_by_ticker: dict[str, Any] | None = None,
    consensus_delta_by_ticker: dict[str, list] | None = None,
    diff_by_ticker: dict[str, dict] | None = None,
    ecosystem_by_ticker: dict[str, Any] | None = None,
    insider_by_ticker: dict[str, Any] | None = None,
    events_8k_by_ticker: dict[str, Any] | None = None,
    risk_diff_by_ticker: dict[str, Any] | None = None,
    hyper_walker: dict | None = None,
) -> str | None:
    """비교 합성 (Opus, 딥리서치급). 컨센서스·diff·생태계·인사이더 데이터를 모두 payload에 포함."""
    from src import summarizer
    system = _load_prompt("earnings_synthesis")
    user_payload = _build_synthesis_payload(
        transcripts, financials, fiscal_period, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker,
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


def _format_diff_text(diff: dict | None) -> str:
    """diff JSON → 텔레그램용 한국어 요약 (Track C)."""
    if not diff:
        return ""
    lines = ["🔁 분기간 diff (이전 N분기 vs 이번 분기)"]
    pq = diff.get("prior_quarters_used") or []
    if pq:
        lines.append(f"  · 비교 분기: {', '.join(pq)}")
    ts = diff.get("tone_shift") or []
    if ts:
        lines.append("  · 톤 시계열:")
        for t in ts[-5:]:
            lines.append(f"    - {t.get('quarter','?')}: {t.get('tone','?')}")
    gc = diff.get("guidance_cycle") or []
    if gc:
        lines.append("  · 가이던스 사이클:")
        for g in gc[-5:]:
            lines.append(f"    - {g.get('quarter','?')} [{g.get('metric','?')}]: {g.get('label','?')}")
    nec = diff.get("named_entity_churn") or {}
    new_e = nec.get("new_this_quarter") or []
    if new_e:
        lines.append("  · 신규 거래처·파트너:")
        for e in new_e[:5]:
            lines.append(f"    - {e.get('counterparty','?')} ({e.get('type','?')})")
    gone = nec.get("disappeared_since_last_quarter") or []
    if gone:
        lines.append("  · 사라진 멘션:")
        for e in gone[:5]:
            lines.append(f"    - {e.get('counterparty','?')} (마지막 {e.get('last_seen_quarter','?')})")
    nt = diff.get("new_themes_this_quarter") or []
    if nt:
        lines.append("  · 이번 분기 새 테마:")
        for x in nt[:5]:
            lines.append(f"    - {x.get('theme','?')}")
    rt = diff.get("recurring_themes") or []
    if rt:
        lines.append("  · 연속 테마(현황):")
        for x in rt[:5]:
            qs = ", ".join(x.get("quarters_seen") or [])
            lines.append(f"    - {x.get('theme','?')} ({qs}, {x.get('tone_arc','?')})")
    pm = (diff.get("pm_takeaway") or "").strip()
    if pm:
        lines.append(f"  · PM takeaway: {pm}")
    return "\n".join(lines)


def _load_hyperscaler_set() -> dict:
    """prompts/data/hyperscaler_set.json 1회 로드 (cached). 실패 시 기본값."""
    if hasattr(_load_hyperscaler_set, "_cache"):
        return _load_hyperscaler_set._cache
    path = Path(__file__).resolve().parent.parent / "prompts" / "data" / "hyperscaler_set.json"
    default = {
        "tickers": ["MSFT", "AMZN", "GOOGL", "META", "ORCL", "CRM"],
        "infra_vendors": ["NVDA", "AMD", "AVGO", "SMCI", "DELL", "HPE", "ANET", "VRT"],
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for k in ("tickers", "infra_vendors"):
            data.setdefault(k, default[k])
        _load_hyperscaler_set._cache = data  # type: ignore[attr-defined]
        return data
    except Exception:
        log.warning("hyperscaler_set.json 로드 실패 → 기본값")
        _load_hyperscaler_set._cache = default  # type: ignore[attr-defined]
        return default


def _build_hyperscaler_capex_table(
    quarters: int = 4,
    ecosystem_by_ticker: dict[str, Any] | None = None,
    analyzed_tickers: list[str] | None = None,
) -> dict:
    """6 hyperscaler × N분기 capex 가이드 walker. 외부 fetch 0회, history cache만.

    반환:
      {
        "applicable": bool,    # 분석 대상이 인프라 벤더거나 ecosystem.customers에 hyperscaler가 있으면 True
        "trigger_reason": str,
        "table": {
          ticker: [{quarter, capex_fy, capex_commentary, source_label}],
          ...
        },
        "missing_tickers": [...]  # history 미보유 hyperscaler
      }
    """
    from src.earnings import history
    cfg = _load_hyperscaler_set()
    hyper = [t.upper() for t in (cfg.get("tickers") or [])]
    vendors = {t.upper() for t in (cfg.get("infra_vendors") or [])}

    trigger_reason = ""
    applicable = False
    analyzed_set = {t.upper() for t in (analyzed_tickers or [])}
    if analyzed_set & vendors:
        applicable = True
        trigger_reason = "분석 종목이 infra_vendors에 포함"
    if not applicable and ecosystem_by_ticker:
        for t, eco in ecosystem_by_ticker.items():
            if not eco:
                continue
            customers = {c.upper() for c in (eco.get("customers") or [])}
            if customers & set(hyper):
                applicable = True
                trigger_reason = f"{t} ecosystem.customers에 hyperscaler 포함"
                break

    table: dict[str, list[dict]] = {}
    missing: list[str] = []
    for h in hyper:
        recent = history.load_recent(h, n=quarters)
        if not recent:
            missing.append(h)
            continue
        rows: list[dict] = []
        for call in recent:
            ext = call.get("extract") or {}
            g = ext.get("guidance") or {}
            cap_fy = g.get("capex_fy") or ""
            cap_comm = g.get("capex_commentary") or ""
            if not (cap_fy or cap_comm):
                continue
            rows.append({
                "quarter": call.get("fiscal_label") or f"FY{call.get('year')}Q{call.get('quarter')}",
                "capex_fy": str(cap_fy)[:240],
                "capex_commentary": str(cap_comm)[:600],
                "source_label": ext.get("source") or "?",
            })
        if rows:
            table[h] = rows
    return {
        "applicable": applicable,
        "trigger_reason": trigger_reason,
        "table": table,
        "missing_tickers": missing,
        "quarters_requested": quarters,
    }


def hyperscaler_payload_block(walker: dict | None) -> str:
    """walker → synthesis 페이로드용 영문 블록. 비어 있으면 빈 문자열."""
    if not walker or not walker.get("applicable"):
        return ""
    table = walker.get("table") or {}
    if not table:
        return (
            "### Hyperscaler capex walker — N/A (history에 hyperscaler 콜 없음, "
            "/backfill <MSFT|AMZN|GOOGL|META|ORCL|CRM> N 권장)"
        )
    lines = [
        f"### Hyperscaler capex walker (trigger: {walker.get('trigger_reason','?')})"
    ]
    for ticker, rows in table.items():
        lines.append(f"#### {ticker}")
        for r in rows:
            lines.append(f"  - {r.get('quarter')}: capex_fy={r.get('capex_fy')}")
            if r.get("capex_commentary"):
                lines.append(f"    commentary: {r.get('capex_commentary')}")
    missing = walker.get("missing_tickers") or []
    if missing:
        lines.append(f"missing (history 없음): {', '.join(missing)}")
    return "\n".join(lines)


def fmt_hyperscaler_text(walker: dict | None) -> str:
    """텔레그램용 한국어 요약."""
    if not walker or not walker.get("applicable"):
        return ""
    table = walker.get("table") or {}
    if not table:
        return (
            "🛰️ Hyperscaler capex 모자이크 — N/A\n"
            "  · history에 hyperscaler 콜 없음. `/backfill MSFT 6` 등으로 시드 권장."
        )
    lines = [f"🛰️ Hyperscaler capex 모자이크 ({walker.get('trigger_reason','')})"]
    for ticker, rows in table.items():
        lines.append(f"  · {ticker}: " + " / ".join(
            f"{r.get('quarter')}: {(r.get('capex_fy') or '')[:60]}" for r in rows[:4]
        ))
    miss = walker.get("missing_tickers") or []
    if miss:
        lines.append(f"  · history 없음: {', '.join(miss)} — backfill 권장")
    return "\n".join(lines)


async def _step_compute_diff(
    ticker: str,
    current_extract: dict,
    fiscal_year: int | None,
    fiscal_quarter: int | None,
    prior_n: int = 4,
) -> dict | None:
    """분기간 diff 합성 (sonnet) — 현재 + 이전 N개 분기 history → 구조화 변화 JSON.

    Track C. prior 분기가 0건이면 의미 없으니 None 반환 (synthesis에서 생략).
    """
    from src import summarizer
    from src.earnings import history
    from src.idea_bot import _parse_json

    exclude = (fiscal_year, fiscal_quarter) if (fiscal_year and fiscal_quarter) else None
    priors = history.load_recent(ticker, n=prior_n, exclude=exclude)
    if not priors:
        return None

    system = _load_prompt("earnings_diff")
    if not system:
        log.warning("earnings_diff.txt 로드 실패 — diff 스킵")
        return None

    # 페이로드: 현재 분기 + 이전 분기들의 extract·consensus만 추려서 LLM에 제출
    def _shrink(call_payload: dict) -> dict:
        """저장 페이로드에서 LLM에 필요한 최소 필드만 추림 (토큰 절약)."""
        ext = call_payload.get("extract") or {}
        cons = call_payload.get("consensus") or {}
        return {
            "fiscal_label": call_payload.get("fiscal_label") or f"FY{call_payload.get('year')}Q{call_payload.get('quarter')}",
            "headline_numbers": ext.get("headline_numbers") or {},
            "guidance": ext.get("guidance") or {},
            "management_tone": ext.get("management_tone") or "",
            "named_deals": ext.get("named_deals") or [],
            "supply_chain_signals": ext.get("supply_chain_signals") or [],
            "competitor_callouts": ext.get("competitor_callouts") or [],
            "macro_policy_mentions": ext.get("macro_policy_mentions") or [],
            "segment_geo_mix": ext.get("segment_geo_mix") or [],
            "notable_verbatim": (ext.get("notable_verbatim") or [])[:3],
            "consensus_excerpt": {
                "revenue_est_current_q": cons.get("revenue_est_current_q") if isinstance(cons, dict) else None,
                "eps_est_current_q": cons.get("eps_est_current_q") if isinstance(cons, dict) else None,
                "rec_mean": cons.get("rec_mean") if isinstance(cons, dict) else None,
            },
        }

    current_payload = {
        "fiscal_label": current_extract.get("fiscal_period") or (f"FY{fiscal_year}Q{fiscal_quarter}" if fiscal_year else "?"),
        "headline_numbers": current_extract.get("headline_numbers") or {},
        "guidance": current_extract.get("guidance") or {},
        "management_tone": current_extract.get("management_tone") or "",
        "named_deals": current_extract.get("named_deals") or [],
        "supply_chain_signals": current_extract.get("supply_chain_signals") or [],
        "competitor_callouts": current_extract.get("competitor_callouts") or [],
        "macro_policy_mentions": current_extract.get("macro_policy_mentions") or [],
        "segment_geo_mix": current_extract.get("segment_geo_mix") or [],
        "notable_verbatim": (current_extract.get("notable_verbatim") or [])[:3],
    }

    user_msg = (
        f"# ticker: {ticker}\n"
        f"# current_quarter (분석 대상): {current_payload['fiscal_label']}\n"
        f"# prior_quarters (신규순, {len(priors)}개):\n"
        + json.dumps([_shrink(p) for p in priors], ensure_ascii=False, indent=2)
        + f"\n\n# current_quarter_extract:\n"
        + json.dumps(current_payload, ensure_ascii=False, indent=2)
        + "\n\n위 데이터로 분기간 변화 JSON을 schema 그대로 출력하세요."
    )

    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_extract_model(),  # sonnet — diff는 추론 깊이 필요
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=4500,
            context="earnings-diff",
        )

    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("diff 합성 실패 (%s)", ticker)
        return None
    if not content:
        return None
    return _parse_json(content)


async def _step_synthesize_counter(
    transcripts: dict[str, dict],
    financials: dict[str, Any],
    fiscal_period: str,
    verify_by_ticker: dict[str, list],
    consensus_by_ticker: dict[str, Any] | None = None,
    consensus_delta_by_ticker: dict[str, list] | None = None,
    diff_by_ticker: dict[str, dict] | None = None,
    ecosystem_by_ticker: dict[str, Any] | None = None,
    insider_by_ticker: dict[str, Any] | None = None,
    events_8k_by_ticker: dict[str, Any] | None = None,
    risk_diff_by_ticker: dict[str, Any] | None = None,
    hyper_walker: dict | None = None,
) -> str | None:
    """Counter-thesis 합성 (Opus) — 동일 cached payload + 다른 시스템 프롬프트.

    idea_bot.py:376-577 `/contrarian` 패턴 차용. 비용: opus 1회 추가
    (~$0.05-0.10) — synthesis와 동일한 payload 재사용해서 추출/Yahoo fetch는 0회.
    """
    from src import summarizer
    system = _load_prompt("earnings_contrarian")
    if not system:
        log.warning("earnings_contrarian.txt 로드 실패 — counter-thesis 스킵")
        return None
    user_payload = _build_synthesis_payload(
        transcripts, financials, fiscal_period, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker,
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
            max_tokens=8000,
            context="earnings-counter",
        )

    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("counter-thesis 합성 실패")
        return None
    return content or None


async def _step_synthesize_custom(
    question: str,
    transcripts: dict[str, dict],
    financials: dict[str, Any],
    fiscal_period: str,
    verify_by_ticker: dict[str, list],
    consensus_by_ticker: dict[str, Any] | None = None,
    consensus_delta_by_ticker: dict[str, list] | None = None,
    diff_by_ticker: dict[str, dict] | None = None,
    ecosystem_by_ticker: dict[str, Any] | None = None,
    insider_by_ticker: dict[str, Any] | None = None,
    events_8k_by_ticker: dict[str, Any] | None = None,
    risk_diff_by_ticker: dict[str, Any] | None = None,
    hyper_walker: dict | None = None,
) -> str | None:
    """커스텀 분석 답변 합성 (Opus). 컨센서스가 있으면 PM-grade로 활용."""
    from src import summarizer
    system = _load_prompt("earnings_custom")
    user_payload = (
        f"# 사용자 질문\n{question}\n\n"
        + _build_synthesis_payload(
            transcripts, financials, fiscal_period, verify_by_ticker,
            consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
            ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        )
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
    consensus_by_ticker: dict[str, Any] | None = None,
    consensus_delta_by_ticker: dict[str, list] | None = None,
    diff_by_ticker: dict[str, dict] | None = None,
    ecosystem_by_ticker: dict[str, Any] | None = None,
    insider_by_ticker: dict[str, Any] | None = None,
    events_8k_by_ticker: dict[str, Any] | None = None,
    risk_diff_by_ticker: dict[str, Any] | None = None,
    hyper_walker: dict | None = None,
) -> str:
    """LLM에 넘길 유저 메시지. 종목별 심층추출 + 재무 6년치 + 검증 + 컨센서스 + diff + 생태계 + 인사이더 + 8-K + 10-K risk diff + hyperscaler capex 모자이크."""
    from src.earnings.sec_edgar import fmt_usd, events_8k_payload_block
    from src.earnings.consensus import consensus_payload_block
    from src.earnings.ecosystem import ecosystem_payload_block
    from src.earnings.sec_edgar import fmt_insider_summary
    from src.earnings import history as _history
    verify_by_ticker = verify_by_ticker or {}
    consensus_by_ticker = consensus_by_ticker or {}
    consensus_delta_by_ticker = consensus_delta_by_ticker or {}
    diff_by_ticker = diff_by_ticker or {}
    ecosystem_by_ticker = ecosystem_by_ticker or {}
    insider_by_ticker = insider_by_ticker or {}
    events_8k_by_ticker = events_8k_by_ticker or {}
    risk_diff_by_ticker = risk_diff_by_ticker or {}
    parts: list[str] = []
    parts.append(f"# 분기: {fiscal_period}")
    parts.append(f"# 분석 기업: {', '.join(transcripts.keys())}")
    grounded_n = sum(1 for tr in transcripts.values() if tr.get("grounded"))
    parts.append(f"# 전문 grounding: {grounded_n}/{len(transcripts)} (grounded=True인 종목만 verbatim 신뢰)")
    consensus_n = sum(1 for v in consensus_by_ticker.values() if v is not None)
    if consensus_by_ticker:
        parts.append(
            f"# 컨센서스 grounding: {consensus_n}/{len(transcripts)} "
            "(sell-side avg estimates from Yahoo Finance / perplexity)"
        )
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
        # 정성 서프라이즈 (Track A 신규 — synthesis가 §1~§4에 강제 인용해야 할 1차 재료)
        deals = tr.get("named_deals") or []
        for d in deals:
            cp = d.get("counterparty", "?")
            mag = d.get("magnitude", "—")
            ten = d.get("tenure") or "—"
            stat = d.get("status", "?")
            prior = d.get("prior_quarter_mentioned")
            prior_tag = "new" if prior is False else ("continuation" if prior is True else "?")
            qv = d.get("verbatim") or ""
            parts.append(f"Deal: counterparty={cp}, size={mag}, tenure={ten}, status={stat}, prior={prior_tag}; \"{qv}\"")
        sc = tr.get("supply_chain_signals") or []
        for s in sc:
            parts.append(f"SupplyChain[{s.get('signal_type','?')}] {s.get('vendor_or_component','?')}: \"{s.get('verbatim','')}\"")
        cc = tr.get("competitor_callouts") or []
        for c in cc:
            parts.append(f"Competitor[{c.get('competitor','?')}/{c.get('dimension','?')}/{c.get('implied_direction','?')}]: \"{c.get('verbatim','')}\"")
        mp = tr.get("macro_policy_mentions") or []
        for m in mp:
            parts.append(f"Macro[{m.get('theme','?')}/{m.get('region','?')}/{m.get('impact_direction','?')}]: \"{m.get('verbatim','')}\"")
        sgm = tr.get("segment_geo_mix") or []
        for item in sgm:
            sr_name = item.get('segment_or_region','?')
            share = item.get('share_pct') or '—'
            yoy = item.get('yoy') or '—'
            call = item.get('callout') or ''
            parts.append(f"GeoMix[{sr_name}]: share={share}, yoy={yoy} — {call}")
        nv = tr.get("notable_verbatim") or []
        for item in nv[:5]:
            parts.append(f"Verbatim: \"{item}\"")
        # 검증 결과
        vres = verify_by_ticker.get(t) or []
        for r in vres:
            parts.append(f"[verify] {r.message}")
        # 컨센서스 delta (beat/miss 정량)
        cdres = consensus_delta_by_ticker.get(t) or []
        for d in cdres:
            parts.append(f"[consensus_delta] {d.message}")
        # sell-side 컨센서스 snapshot (synthesis가 §1·§4·§8에서 인용 강제)
        snap = consensus_by_ticker.get(t)
        if snap is not None:
            parts.append(consensus_payload_block(snap))
        # 분기간 diff (Track C — 이전 N분기와의 톤·가이던스·entity 변화)
        diff = diff_by_ticker.get(t)
        if diff:
            parts.append(f"### {t} quarter-over-quarter diff")
            parts.append(json.dumps(diff, ensure_ascii=False))
        # 생태계 맵 (Track D) — 같은 분기 history가 있는 ecosystem ticker는 excerpt 첨부
        eco = ecosystem_by_ticker.get(t)
        if eco:
            related_excerpts: dict[str, dict] = {}
            related_tickers: list[str] = []
            for bucket in ("customers", "inputs", "peers", "enablers"):
                related_tickers.extend((eco.get(bucket) or [])[:3])
            for rt in dict.fromkeys(related_tickers):  # dedup, preserve order
                recent = _history.load_recent(rt, n=1)
                if recent:
                    excerpt = recent[0].get("extract") or {}
                    related_excerpts[rt] = {
                        "fiscal_label": recent[0].get("fiscal_label"),
                        "management_tone": excerpt.get("management_tone"),
                        "guidance": excerpt.get("guidance") or {},
                    }
            parts.append(ecosystem_payload_block(t, eco, related_excerpts or None))
        # 인사이더 매매 (Track F — Form 4 최근 30일)
        insider = insider_by_ticker.get(t)
        if insider is not None:
            parts.append(f"### {t} insider activity (last {insider.window_days} days)")
            parts.append(f"form4_count={insider.form4_count}")
            for f in (insider.forms or [])[:6]:
                rep = (f.get("reporter") or "?")
                parts.append(f"  - {f.get('filed','?')} reporter={rep}")
        # NLP 시그널 (Track K)
        sig = tr.get("_signals")
        if sig:
            from src.earnings.nlp_signals import signals_payload_block
            parts.append(signals_payload_block(t, sig))
        # 8-K 머티어리얼 이벤트 (Track H)
        ek = events_8k_by_ticker.get(t)
        if ek is not None:
            block = events_8k_payload_block(ek)
            if block:
                parts.append(block)
        # 10-K Risk Factor diff (Track I)
        rd = risk_diff_by_ticker.get(t)
        if rd:
            parts.append(f"### {t} 10-K Risk Factor 1-year diff")
            parts.append(json.dumps(rd, ensure_ascii=False))
        parts.append("")

    # Hyperscaler capex 모자이크 (Track L) — 분석 종목별 블록 끝난 뒤 1회만
    if hyper_walker and hyper_walker.get("applicable"):
        block = hyperscaler_payload_block(hyper_walker)
        if block:
            parts.append(block)
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
    ("backfill", "🔁 종목 longitudinal history 시드 (`/backfill MSFT 6`)"),
    ("help", "ℹ️ 사용법"),
]


def build_earnings_app(token: str) -> Application:
    """전용 EarningsBot Application 빌더 (orchestrator BOT_SPECS에서 호출).

    핸들러: /start·/help · /earnings · /backfill · 자유 텍스트(_on_text)로도 바로 진입.
    EARNINGS_TEST_PROMPT env가 있으면 부팅 후 1회 self-test 자동 실행 (CLAUDE.md 검증 의무).
    """
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(CommandHandler("earnings", _cmd_earnings))
    app.add_handler(CommandHandler("backfill", _cmd_backfill))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_self_test(app))
    except RuntimeError:
        pass
    return app
