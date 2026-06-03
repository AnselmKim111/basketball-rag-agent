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


async def _step_synthesize_pre_call(
    ticker: str, year: int, quarter: int, fiscal_label: str,
) -> tuple[str | None, Any]:
    """Track G — 콜 발표 전 기대치 보고서 합성 (Opus).

    입력: 현재 컨센서스(Yahoo) + 이전 N분기 history + 생태계 맵.
    반환: (report_text, consensus_snapshot).
    """
    from src import summarizer
    from src.earnings import consensus, history, ecosystem
    loop = asyncio.get_running_loop()
    # 1) 데이터 병렬 fetch
    snap, eco = await asyncio.gather(
        loop.run_in_executor(None, consensus.fetch_consensus, ticker, fiscal_label),
        loop.run_in_executor(None, ecosystem.get_ecosystem, ticker, True),
    )
    priors = history.load_recent(ticker, n=4, exclude=(year, quarter))

    # 2) payload 빌드
    parts: list[str] = []
    parts.append(f"# 분석 대상: {ticker}")
    parts.append(f"# 분기: {fiscal_label}")
    parts.append("")
    if snap is not None:
        from src.earnings.consensus import consensus_payload_block
        parts.append(consensus_payload_block(snap))
    else:
        parts.append("### consensus snapshot — N/A")
    parts.append("")
    if priors:
        parts.append(f"### prior {len(priors)} quarters history (most recent first)")
        for p in priors:
            ext = p.get("extract") or {}
            parts.append(
                f"- {p.get('fiscal_label','?')} tone='{(ext.get('management_tone') or '')[:120]}' "
                f"guidance_keys={list((ext.get('guidance') or {}).keys())[:6]} "
                f"named_deals_n={len(ext.get('named_deals') or [])} "
                f"recurring_themes_hint={(ext.get('notable_verbatim') or [''])[0][:80] if ext.get('notable_verbatim') else ''}"
            )
    else:
        parts.append("### prior history — N/A (/backfill 권장)")
    parts.append("")
    if eco:
        from src.earnings.ecosystem import ecosystem_payload_block
        parts.append(ecosystem_payload_block(ticker, eco, None))
    else:
        parts.append("### ecosystem map — N/A")
    user_payload = "\n".join(parts)

    system = _load_prompt("earnings_pre_call")
    if not system:
        log.warning("earnings_pre_call.txt 로드 실패 — pre-call 합성 스킵")
        return None, snap

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
            max_tokens=7000,
            context=f"earnings-precall:{ticker}",
        )
    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("pre-call 합성 실패 (%s %s)", ticker, fiscal_label)
        return None, snap
    return (content or None), snap


async def _step_synthesize_pre_vs_actual(
    ticker: str, year: int, quarter: int, fiscal_label: str,
    actual_extract: dict,
    consensus_snap: Any | None,
    consensus_deltas: list | None,
) -> str | None:
    """Track G — pre-call vs actual diff (Opus 1회). pre-call 캐시가 있을 때만 호출."""
    from src import summarizer
    from src.earnings import precall

    pre = precall.load(ticker, year, quarter)
    if not pre:
        return None

    system = _load_prompt("earnings_pre_vs_actual")
    if not system:
        log.warning("earnings_pre_vs_actual.txt 로드 실패")
        return None

    # actual 추출에서 핵심만 압축
    hn = actual_extract.get("headline_numbers") or {}
    guidance = actual_extract.get("guidance") or {}
    deals = actual_extract.get("named_deals") or []
    sc = actual_extract.get("supply_chain_signals") or []
    cc = actual_extract.get("competitor_callouts") or []
    mp = actual_extract.get("macro_policy_mentions") or []
    tone = actual_extract.get("management_tone") or ""
    surprises = actual_extract.get("surprises_and_risks") or []

    user_payload = (
        f"# ticker: {ticker}\n# fiscal_label: {fiscal_label}\n\n"
        "## PRE-CALL REPORT (직전 합성, 콜 발표 전)\n"
        f"generated_at: {pre.get('generated_at','?')}\n"
        f"pre_call_bias: {pre.get('pre_call_bias','?')}\n\n"
        f"{pre.get('report_text','(pre-call report body missing)')}\n\n"
        "## ACTUAL CALL EXTRACT (지금 콜 결과)\n"
        f"headline_numbers: {json.dumps(hn, ensure_ascii=False)}\n"
        f"guidance: {json.dumps(guidance, ensure_ascii=False)}\n"
        f"management_tone: {tone}\n"
        f"named_deals ({len(deals)}): {json.dumps(deals[:6], ensure_ascii=False)}\n"
        f"supply_chain_signals ({len(sc)}): {json.dumps(sc[:6], ensure_ascii=False)}\n"
        f"competitor_callouts ({len(cc)}): {json.dumps(cc[:6], ensure_ascii=False)}\n"
        f"macro_policy_mentions ({len(mp)}): {json.dumps(mp[:6], ensure_ascii=False)}\n"
        f"surprises_and_risks: {json.dumps(surprises[:6], ensure_ascii=False)}\n\n"
        "## CONSENSUS SNAPSHOT (콜 시점)\n"
        f"{json.dumps({k: v for k, v in (consensus_snap.__dict__ if consensus_snap else {}).items() if not k.startswith('_')}, ensure_ascii=False, default=str)[:2000]}\n\n"
        "## CONSENSUS DELTAS\n"
        + "\n".join(f"- {d.message}" for d in (consensus_deltas or []))
        + "\n\n위 정보로 schema 그대로 'Pre vs Actual' 보고서 작성."
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
            max_tokens=5500,
            context=f"earnings-pre-vs-actual:{ticker}",
        )
    try:
        return (await loop.run_in_executor(None, _call)) or None
    except Exception:
        log.exception("pre-vs-actual 합성 실패 (%s)", ticker)
        return None


async def _step_extract_predictions(
    ticker: str, year: int, quarter: int,
    synthesis_text: str, counter_text: str,
) -> int:
    """Phase 7C — synthesis + counter에서 검증 가능한 prediction 5-10개 추출 → SQLite INSERT.

    반환: INSERT 성공 건수.
    """
    from src import summarizer
    from src.earnings import predictions_db
    from src.idea_bot import _parse_json

    if not synthesis_text and not counter_text:
        return 0
    system = _load_prompt("earnings_predictions_extract")
    if not system:
        return 0

    user_msg = (
        f"# ticker: {ticker}\n# year: {year} quarter: {quarter}\n\n"
        f"## SYNTHESIS REPORT\n{synthesis_text or '(empty)'}\n\n"
        f"## COUNTER-THESIS REPORT\n{counter_text or '(empty)'}\n\n"
        "위 두 보고서에서 schema 그대로 predictions JSON 추출."
    )
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_extract_model(),  # sonnet (구조화 추출)
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=3000,
            context=f"earnings-predictions-extract:{ticker}",
        )
    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("[predictions extract] LLM 실패 (%s)", ticker)
        return 0
    parsed = _parse_json(content) if content else None
    if not isinstance(parsed, dict):
        return 0
    rows = []
    for p in (parsed.get("predictions") or []):
        if not isinstance(p, dict):
            continue
        rows.append({
            "ticker": ticker, "year": year, "quarter": quarter,
            "type": p.get("type") or "trigger",
            "statement_text": p.get("statement_text") or "",
            "conviction": p.get("conviction"),
            "metric_threshold": p.get("metric_threshold"),
            "payload_excerpt": p.get("payload_excerpt") or "",
        })
    return predictions_db.insert_predictions_bulk(rows)


async def _step_retrospective(
    ticker: str, current_year: int, current_quarter: int,
    actual_extract: dict, consensus_snap: Any | None, consensus_deltas: list | None,
) -> str | None:
    """Phase 7C — 이전 분기 predictions 회고. opus 1회 + verifications INSERT + report 반환."""
    from src import summarizer
    from src.earnings import predictions_db
    from src.idea_bot import _parse_json

    priors = predictions_db.list_unverified_predictions(ticker, current_year, current_quarter)
    if not priors:
        return None
    system = _load_prompt("earnings_retrospective")
    if not system:
        return None

    # actual 데이터 요약
    hn = actual_extract.get("headline_numbers") or {}
    g = actual_extract.get("guidance") or {}
    tone = actual_extract.get("management_tone") or ""
    deals = actual_extract.get("named_deals") or []
    sc = actual_extract.get("supply_chain_signals") or []
    mp = actual_extract.get("macro_policy_mentions") or []
    cons_summary = ""
    if consensus_snap is not None:
        try:
            cons_summary = json.dumps(
                {k: v for k, v in (consensus_snap.__dict__ if hasattr(consensus_snap, "__dict__") else {}).items()
                 if not k.startswith("_") and v is not None},
                ensure_ascii=False, default=str,
            )[:1500]
        except Exception:
            cons_summary = "(consensus snapshot 직렬화 실패)"

    user_msg = (
        f"# ticker: {ticker}\n# current_year: {current_year} current_quarter: {current_quarter}\n\n"
        "## PRIOR PREDICTIONS (verifications 미완)\n"
        + json.dumps(
            [{"prediction_id": p["id"], "year": p["year"], "quarter": p["quarter"],
              "type": p["type"], "statement_text": p["statement_text"],
              "conviction": p.get("conviction"), "metric_threshold": p.get("metric_threshold_json")}
             for p in priors], ensure_ascii=False, indent=2,
        )
        + "\n\n## ACTUAL THIS QUARTER\n"
        + f"headline_numbers: {json.dumps(hn, ensure_ascii=False)}\n"
        + f"guidance: {json.dumps(g, ensure_ascii=False)}\n"
        + f"management_tone: {tone}\n"
        + f"named_deals: {json.dumps(deals[:5], ensure_ascii=False)}\n"
        + f"supply_chain_signals: {json.dumps(sc[:5], ensure_ascii=False)}\n"
        + f"macro_policy_mentions: {json.dumps(mp[:5], ensure_ascii=False)}\n"
        + f"consensus_snapshot: {cons_summary}\n"
        + "consensus_deltas: " + "\n".join(f"- {d.message}" for d in (consensus_deltas or []))
        + "\n\n위 데이터로 schema 그대로 회고 JSON 출력."
    )
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_synthesis_model(),  # opus (정밀 판정)
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=5000,
            context=f"earnings-retrospective:{ticker}",
        )
    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("[retrospective] LLM 실패 (%s)", ticker)
        return None
    parsed = _parse_json(content) if content else None
    if not isinstance(parsed, dict):
        return None

    # verifications INSERT
    inserted = 0
    for v in (parsed.get("verifications") or []):
        pid = v.get("prediction_id")
        if not pid:
            continue
        rid = predictions_db.insert_verification(
            prediction_id=int(pid),
            verified_year=current_year, verified_quarter=current_quarter,
            outcome=v.get("outcome") or "unverifiable",
            evidence_text=v.get("evidence_text") or "",
            model_used=_synthesis_model(),
        )
        if rid:
            inserted += 1
    log.info("[retrospective] %s — verifications INSERTED %d/%d", ticker, inserted, len(parsed.get("verifications") or []))
    return parsed.get("retrospective_report") or None


async def _cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/digest [days=7] — 사용자 watchlist 기준 지난 N일 동안의 콜 digest."""
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    args = context.args or []
    days = 7
    if args:
        try:
            days = max(1, min(int(args[0]), 30))
        except ValueError:
            pass
    asyncio.create_task(_run_digest(update.effective_chat.id, context.bot, days))


async def _run_digest(chat_id: int | str, bot: Bot, days: int = 7) -> None:
    """digest 워커. watchlist에 있는 ticker 중 지난 N일 처리된 콜 모음 + opus 1회 요약."""
    from src.earnings import knowledge_db, earnings_watchlist as wl
    from src import summarizer

    loop = asyncio.get_running_loop()
    watched = wl.list_for_chat(chat_id)
    if not watched:
        await _safe_send(bot, str(chat_id), "📰 digest 생략 — watchlist 비어 있음 (`/watch <TICKER>` 권장).")
        return
    recent = await loop.run_in_executor(None, knowledge_db.list_recent_calls, days, 60)
    in_watch = [r for r in recent if r["ticker"] in {t.upper() for t in watched}]
    if not in_watch:
        await _safe_send(bot, str(chat_id), f"📰 지난 {days}일 watchlist 종목 신규 콜 없음 ({len(watched)}개 watch 중).")
        return

    payload_parts = [f"# 사용자 watchlist: {', '.join(sorted(watched))}", f"# 윈도: 최근 {days}일", ""]
    for c in in_watch:
        payload_parts.append(
            f"## [{c['ticker']} FY{c['year']}Q{c['quarter']}] (grounded={c['grounded']}) {c.get('call_date','')}"
        )
        payload_parts.append((c.get("syn_head") or "")[:600])
        if c.get("cnt_head"):
            payload_parts.append(f"counter excerpt: {(c.get('cnt_head') or '')[:300]}")
        payload_parts.append("")
    user_msg = "\n".join(payload_parts)

    system = (
        "당신은 시니어 PM의 비서. 사용자 watchlist의 지난 N일 어닝콜들을 종합해 한국어로 정리한다. "
        "구조:\n"
        "1. 🔥 가장 강한 surprise (종목별)\n"
        "2. 🛡️ counter-thesis 경계할 점\n"
        "3. 📐 컨센 vs 실제 가장 큰 비대칭\n"
        "4. 👥 인사이더·8-K·MD&A 알림\n"
        "5. 다음 주 모니터링 트리거\n"
        "각 항목 짧고 정량. 데이터에 없는 사실 추측 금지. 4000자 이하."
    )

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client, model=_synthesis_model(),  # opus
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3, max_tokens=4500, context="earnings-digest",
        )
    await _safe_send(bot, str(chat_id), f"📰 digest 합성 중 ({len(in_watch)}개 콜, 지난 {days}일)…")
    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("digest 합성 실패")
        content = None
    if content:
        await send_text_chunked(bot, str(chat_id), content)
    else:
        # 폴백: raw
        lines = [f"📰 digest (raw) — watchlist 신규 콜 {len(in_watch)}건"]
        for c in in_watch[:10]:
            lines.append(f"  · [{c['ticker']} FY{c['year']}Q{c['quarter']}]: {(c.get('syn_head') or '')[:200]}")
        await _safe_send(bot, str(chat_id), "\n".join(lines))


async def earnings_digest_cron_job(bot: Bot) -> None:
    """orchestrator APScheduler — 매주 월 09:00 KST, 모든 chat의 watchlist 기준 digest 자동 발송."""
    from src.earnings import earnings_watchlist as wl
    log.info("[earnings_digest_cron] 시작")
    all_t = wl.all_tickers()
    if not all_t:
        log.info("[earnings_digest_cron] watch 종목 0 — skip")
        return
    # subscriber 별로 한 번씩
    seen_chats: set[str] = set()
    for t in all_t:
        for cid in wl.subscribers_of(t):
            if cid in seen_chats:
                continue
            seen_chats.add(cid)
    for cid in seen_chats:
        try:
            await _run_digest(cid, bot, days=7)
        except Exception:
            log.exception("[earnings_digest_cron] chat=%s digest 실패", cid)


async def _cmd_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/query <자유 텍스트> — knowledge_db 부분 매칭 + sonnet 결과 정리.

    예시:
      /query SK Hynix mentions in hyperscaler calls
      /query NVDA recent counter-thesis 약점
    """
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    from src.earnings import knowledge_db
    args = (context.args or [])
    if not args:
        await update.message.reply_text(
            "사용법: `/query <자유 텍스트>`\n"
            "예시:\n"
            "  `/query SK Hynix mentions`\n"
            "  `/query NVDA counter-thesis 약점`\n"
            "  `/query AAPL FY2026Q1`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    query_text = " ".join(args).strip()
    asyncio.create_task(_run_query(update, context, query_text))


async def _run_query(update: Update, context: ContextTypes.DEFAULT_TYPE, q: str) -> None:
    from src.earnings import knowledge_db
    from src import summarizer
    from src.idea_bot import _parse_json
    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)
    await _safe_send(bot, chat_id, f"🔍 /query 시작 — \"{q[:100]}\"")
    loop = asyncio.get_running_loop()

    # 1) ticker가 명시되면 그 종목 분기 정리
    ticker_m = re.search(r"\b([A-Z]{1,5})\b", q)
    ticker_candidate = ticker_m.group(1) if ticker_m else None

    # 2) 엔티티/풀텍스트 부분 매칭 (단순 LIKE)
    keyword_m = re.findall(r"[A-Za-z가-힣][\w가-힣]{2,30}", q)
    candidates = sorted(set(keyword_m), key=len, reverse=True)[:5]
    entity_hits: list[dict] = []
    text_hits: list[dict] = []
    for kw in candidates:
        entity_hits.extend(await loop.run_in_executor(None, knowledge_db.search_entity_mentions, kw, 12))
        text_hits.extend(await loop.run_in_executor(None, knowledge_db.fulltext_search, kw, 8))

    # 3) ticker별 최근 콜 메타
    ticker_calls = []
    if ticker_candidate:
        ticker_calls = await loop.run_in_executor(None, knowledge_db.list_calls_for_ticker, ticker_candidate, 6)

    if not entity_hits and not text_hits and not ticker_calls:
        await _safe_send(bot, chat_id, "🤷 매칭 결과 없음. DB가 비어 있거나 다른 단어로 시도.")
        return

    # 4) sonnet 1회로 자연어 응답 합성
    raw_data = {
        "query": q, "ticker_candidate": ticker_candidate,
        "keywords_used": candidates,
        "entity_matches": entity_hits[:20],
        "fulltext_matches": [{k: v for k, v in h.items() if k != "syn_head"} for h in text_hits[:10]],
        "fulltext_snippets": [{"id": h["id"], "snippet": (h.get("syn_head") or "")[:300]} for h in text_hits[:8]],
        "ticker_calls": ticker_calls,
    }
    system = (
        "당신은 어닝콜 봇의 query 응답기다. raw_data를 받아 사용자 질문에 한국어로 명확히 답한다. "
        "결과 없으면 '결과 없음'. 데이터에 없는 사실 추측 금지. "
        "각 결과에 [TICKER FY...Q...] 라벨. 4000자 이하."
    )
    user_msg = f"# 질문\n{q}\n\n# raw_data (knowledge_db search 결과)\n{json.dumps(raw_data, ensure_ascii=False, default=str)[:30000]}"

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client, model=_extract_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2, max_tokens=3500, context="earnings-query",
        )
    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("/query LLM 실패")
        content = None
    if content:
        await send_text_chunked(bot, chat_id, content)
    else:
        # 폴백: raw 결과만이라도
        lines = ["🔍 매칭 (raw)"]
        for e in entity_hits[:10]:
            lines.append(f"  · [{e['ticker']} FY{e['year']}Q{e['quarter']}] {e['mention_text']} ({e['mention_type']})")
        for h in text_hits[:5]:
            lines.append(f"  · [{h['ticker']} FY{h['year']}Q{h['quarter']}] {(h.get('syn_head') or '')[:200]}")
        await _safe_send(bot, chat_id, "\n".join(lines))


async def _cmd_credibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/credibility <TICKER> — 봇의 누적 적중률 표시."""
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    from src.earnings import predictions_db
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/credibility <TICKER>`\n예: `/credibility NVDA`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ticker = args[0].upper().strip()
    loop = asyncio.get_running_loop()
    score = await loop.run_in_executor(None, predictions_db.credibility_score, ticker)
    await update.message.reply_text(predictions_db.fmt_credibility(score) or f"{ticker} 적중률 조회 실패")


async def _cmd_preearnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/preearnings <TICKER> [FYxxxxQy] — 콜 발표 전 사전 기대치 보고서."""
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/preearnings <TICKER> [FY2026Q1]`\n예: `/preearnings MSFT` (분기 미명시 시 자동 추정)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ticker = args[0].upper().strip()
    period = " ".join(args[1:]).strip() if len(args) > 1 else ""
    asyncio.create_task(_run_pre_call(update, context, ticker, period))


async def _run_pre_call(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ticker: str, period: str,
) -> None:
    """pre-call 워커."""
    from datetime import datetime as _dt
    from src.earnings import precall
    from src.earnings.transcript_source import resolve_year_quarter

    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)

    # 분기 도출 — 명시 안 됐으면 다음 어닝 예정일 기준, 그것도 없으면 다음 calendar 분기
    y, q = resolve_year_quarter(period) if period else (None, None)
    if not y or not q:
        from src.earnings import consensus as _cons
        loop = asyncio.get_running_loop()
        try:
            nd = await loop.run_in_executor(None, _cons.fetch_next_earnings_date, ticker)
        except Exception:
            nd = None
        if nd:
            try:
                ref = _dt.strptime(nd, "%Y-%m-%d")
                y, q = ref.year, (ref.month - 1) // 3 + 1
            except Exception:
                pass
    if not y or not q:
        today = _dt.utcnow()
        y, q = today.year, (today.month - 1) // 3 + 1

    fiscal_label = f"Q{q} {y}"
    await _safe_send(bot, chat_id, f"🎯 /preearnings 시작 — {ticker} {fiscal_label} 사전 기대치 합성 중 …")

    report, snap = await _step_synthesize_pre_call(ticker, y, q, fiscal_label)
    if not report:
        await _safe_send(bot, chat_id, "⚠️ Pre-call 합성 실패 — 컨센·history·생태계 데이터 부족 가능.")
        return

    # 보고서 발송
    await send_text_chunked(bot, chat_id, report)

    # 캐시 저장 — 콜 transcript 등재 시 자동 비교
    try:
        precall.save(
            ticker=ticker, year=y, quarter=q, fiscal_label=fiscal_label,
            report_text=report, consensus=snap, key_questions=[], pre_call_bias="",
        )
        await _safe_send(
            bot, chat_id,
            f"💾 pre-call 캐시 저장 — 콜 transcript 등재 시 자동으로 'pre vs actual' diff가 따라옵니다.",
        )
    except Exception:
        log.exception("pre-call 캐시 저장 실패")


async def _cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/watch <TICKER> [more] — 다음 콜 자동 발송 예약."""
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    from src.earnings import earnings_watchlist as wl
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/watch <TICKER1> [TICKER2 ...]`\n예: `/watch MSFT NVDA AAPL`\n\n"
            "콜 발표 직후 자동으로 풀 분석(Tracks A-L) 보고서를 보냅니다.\n"
            "관리: /watchlist · /unwatch",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    chat_id = update.effective_chat.id
    msgs: list[str] = []
    for raw in args:
        t = raw.strip().upper()
        ok, m = wl.add(chat_id, t)
        msgs.append(f"{'✅' if ok else '⏭️'} {m}")
    current = wl.list_for_chat(chat_id)
    await update.message.reply_text(
        "\n".join(msgs) + (f"\n\n현재 watch: {', '.join(current) or '비어 있음'}")
    )


async def _cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unwatch <TICKER> [more]."""
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    from src.earnings import earnings_watchlist as wl
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/unwatch <TICKER1> [TICKER2 ...]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    chat_id = update.effective_chat.id
    msgs: list[str] = []
    for raw in args:
        t = raw.strip().upper()
        ok, m = wl.remove(chat_id, t)
        msgs.append(f"{'✅' if ok else '⏭️'} {m}")
    current = wl.list_for_chat(chat_id)
    await update.message.reply_text(
        "\n".join(msgs) + (f"\n\n현재 watch: {', '.join(current) or '비어 있음'}")
    )


async def _cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/watchlist — 현재 chat의 자동 발송 ticker 목록 + 다음 어닝 예정일."""
    if not _is_authorized(update):
        await deny_message(update, "어닝콜 기능")
        return
    from src.earnings import earnings_watchlist as wl
    from src.earnings import consensus
    chat_id = update.effective_chat.id
    tickers = wl.list_for_chat(chat_id)
    if not tickers:
        await update.message.reply_text(
            "watch 중인 종목 없음. `/watch MSFT NVDA` 형태로 추가.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await update.message.reply_text(f"🔭 watch ({len(tickers)}): {', '.join(tickers)}\n다음 어닝 예정 조회 중 …")
    loop = asyncio.get_running_loop()
    dates = await asyncio.gather(
        *[loop.run_in_executor(None, consensus.fetch_next_earnings_date, t) for t in tickers],
        return_exceptions=True,
    )
    lines = ["📅 다음 어닝 예정 (Yahoo 기준):"]
    for t, d in zip(tickers, dates):
        if isinstance(d, Exception) or not d:
            lines.append(f"  · {t}: 미상")
        else:
            lines.append(f"  · {t}: {d}")
    await update.message.reply_text("\n".join(lines))


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

    # Phase 9 — 메시지 1: 시작 알림 (한 줄)
    await _safe_reply(update, "📞 어닝콜 분석 시작 — 약 5-10분 소요 (결과는 한 번에 발송)")

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
        # 진행 알림 제거 — 시작 알림 1줄로 충분
        log.info(
            "[Phase 9] tickers 모드: %s, 분기=%s, custom=%s",
            tickers, fiscal_period, custom_question[:50] if custom_question else "(none)",
        )
    elif mode == "criteria":
        criteria = (parsed.get("criteria") or user_text).strip()
        # criteria 모드는 LLM이 ticker 확장하니 결과만 1줄 노출
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
                + (f"\n💡 {expansion.get('exclusion_notes')}" if expansion.get("exclusion_notes") else ""),
            )
            return
        tickers = tickers[:8]
        period_guess = expansion.get("target_fiscal_period")
        if period_guess and not fiscal_period:
            fiscal_period = period_guess
        # 확장 결과 한 줄 (어떤 ticker로 갔는지 사용자 검증 필요)
        await _safe_send(
            bot, chat_id,
            f"🌐 조건 확장: {', '.join(tickers)} — 분석 진행 …",
        )
    else:  # custom_only — 회사 미상
        await _safe_send(
            bot, chat_id,
            "⚠️ 분석 대상을 찾지 못했습니다.\n\n"
            "입력 예시:\n"
            "  • 회사 지정: AAPL MSFT GOOGL 2026 1Q\n"
            "  • 조건: 빅테크 2026 1Q / capex 상위 5\n\n"
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

    # Phase 9 — 백그라운드: 7-track fetch (텔레그램 알림 X, 로그만)
    log.info("[Phase 9] 7-track fetch 시작 (tickers=%s, fiscal=%s)", tickers, fiscal_label)
    (
        financials, consensus_by_ticker, ecosystem_by_ticker,
        insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        mdna_diff_by_ticker,
    ) = await asyncio.gather(
        _step_fetch_financials(tickers),
        _step_fetch_consensus(tickers, fiscal_label),
        _step_fetch_ecosystem(tickers),
        _step_fetch_insider(tickers, window_days=30),
        _step_fetch_8k(tickers, window_days=90),
        _step_fetch_risk_diff(tickers),
        _step_fetch_mdna_diff(tickers),
    )
    if not financials:
        log.warning("[Phase 9] SEC EDGAR 데이터 fetch 실패 — 차트·검증 제한")
    log.info(
        "[Phase 9] 7-track 완료: consensus=%d/%d ecosystem=%d/%d insider=%d/%d 8k=%d/%d risk=%d mdna=%d",
        sum(1 for v in (consensus_by_ticker or {}).values() if v is not None), len(tickers),
        sum(1 for v in (ecosystem_by_ticker or {}).values() if v is not None), len(tickers),
        sum(1 for v in (insider_by_ticker or {}).values() if v is not None), len(tickers),
        sum(1 for v in (events_8k_by_ticker or {}).values() if v is not None), len(tickers),
        sum(1 for v in (risk_diff_by_ticker or {}).values() if v and not v.get("diff_skipped")),
        sum(1 for v in (mdna_diff_by_ticker or {}).values() if v and not v.get("diff_skipped")),
    )

    # Phase 9 — 종목별 extract (텔레그램 알림 X, 로그만)
    transcripts: dict[str, dict] = {}
    verify_by_ticker: dict[str, list] = {}
    consensus_delta_by_ticker: dict[str, list] = {}
    diff_by_ticker: dict[str, dict] = {}
    log.info("[Phase 9] 종목별 extract 시작 (%d ticker)", len(tickers))
    for idx, t in enumerate(tickers, 1):
        log.info("[Phase 9] (%d/%d) %s extract 중 …", idx, len(tickers), t)
        tr = await _step_deep_extract(t, fiscal_year, fiscal_quarter, fiscal_label)
        if tr is None:
            log.warning("[Phase 9] (%d/%d) %s 전문 확보 실패 — 스킵", idx, len(tickers), t)
            continue
        vres = await _step_verify(tr, financials.get(t), fiscal_year, fiscal_quarter)
        verify_by_ticker[t] = vres
        tr["_verify"] = [v.message for v in vres]
        snap = (consensus_by_ticker or {}).get(t)
        if snap is not None:
            from src.earnings.verify import compute_consensus_delta
            deltas = compute_consensus_delta(tr, snap)
            consensus_delta_by_ticker[t] = deltas
            tr["_consensus_deltas"] = [d.message for d in deltas]
            tr["_consensus_snap"] = snap
        transcripts[t] = tr
        diff = await _step_compute_diff(t, tr, fiscal_year, fiscal_quarter, prior_n=4)
        if diff:
            diff_by_ticker[t] = diff
            tr["_diff"] = diff

    if not transcripts:
        await _safe_send(bot, chat_id, "⚠️ 모든 종목의 전문 확보 실패. 잠시 후 재시도 권장.")
        return

    grounded_n = sum(1 for tr in transcripts.values() if tr.get("grounded"))
    log.info("[Phase 9] extract 완료: %d/%d, grounded %d/%d",
             len(transcripts), len(tickers), grounded_n, len(transcripts))

    # Hyperscaler capex walker (외부 fetch 0회)
    hyper_walker = _build_hyperscaler_capex_table(
        quarters=4,
        ecosystem_by_ticker=ecosystem_by_ticker,
        analyzed_tickers=list(transcripts.keys()),
    )
    if hyper_walker.get("applicable"):
        log.info("[Phase 9] hyperscaler walker applicable: %d", len(hyper_walker.get("table") or []))

    # 시장 반응 fetch
    log.info("[Phase 9] 시장 반응 fetch 시작")
    market_reaction_by_ticker = await _step_fetch_market_reaction(
        list(transcripts.keys()), transcripts, consensus_by_ticker,
    )
    got_mr = sum(
        1 for v in (market_reaction_by_ticker or {}).values()
        if v is not None and getattr(v, "ret_1d", None) is not None
    )
    log.info("[Phase 9] 시장 반응 완료: %d/%d", got_mr, len(transcripts))

    # 비교 합성 (Opus) — 텔레그램 발송 X, PDF용으로만
    log.info("[Phase 9] 비교 합성 (Opus) 시작 …")
    industry_summary, synthesis_gate_meta = await _step_synthesize_industry(
        transcripts, financials, fiscal_label, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker, market_reaction_by_ticker,
    )
    if not industry_summary:
        industry_summary = "(비교 합성 실패 — PDF에는 차트·전문만 포함)"
    log.info("[Phase 9] 비교 합성 완료: %d자, attempts=%d",
             len(industry_summary), (synthesis_gate_meta or {}).get("attempts", 1))

    # Multi-Perspective + Meta (Sonnet x3 + Opus x1) — 텔레그램 발송 X, PDF용으로만
    log.info("[Phase 9] Multi-Perspective + Meta 합성 …")
    perspectives = await _step_multi_perspective(
        transcripts, financials, fiscal_label, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker, market_reaction_by_ticker,
    )
    meta_text = ""
    if any(perspectives.values()):
        meta_text = await _step_meta_synthesis(perspectives) or ""

    # Counter-thesis (Opus) — 텔레그램 발송 X, PDF용으로만
    log.info("[Phase 9] Counter-thesis 합성 …")
    counter_summary = await _step_synthesize_counter(
        transcripts, financials, fiscal_label, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker, market_reaction_by_ticker,
    ) or ""

    # Pre vs Actual diff — 텔레그램 발송 X, PDF에 함께 (있을 때만)
    pre_vs_actual_by_ticker: dict[str, str] = {}
    if fiscal_year and fiscal_quarter:
        from src.earnings import precall as _precall
        for t, tr in transcripts.items():
            if not _precall.exists(t, fiscal_year, fiscal_quarter):
                continue
            try:
                pa_text = await _step_synthesize_pre_vs_actual(
                    t, fiscal_year, fiscal_quarter, fiscal_label, tr,
                    consensus_by_ticker.get(t), consensus_delta_by_ticker.get(t),
                )
            except Exception:
                log.exception("[pre-vs-actual] 합성 실패 (%s)", t)
                pa_text = None
            if pa_text:
                pre_vs_actual_by_ticker[t] = pa_text

    # Predictions INSERT + Retrospective — 텔레그램 발송 X, PDF에 함께 (있을 때만)
    retrospective_by_ticker: dict[str, str] = {}
    if fiscal_year and fiscal_quarter:
        for t, tr in transcripts.items():
            try:
                n = await _step_extract_predictions(
                    t, fiscal_year, fiscal_quarter,
                    industry_summary or "", counter_summary or "",
                )
                if n > 0:
                    log.info("[Phase9] %s — %d predictions INSERTED FY%sQ%s", t, n, fiscal_year, fiscal_quarter)
            except Exception:
                log.exception("[Phase9] predictions extract 실패 (%s)", t)
            try:
                retro = await _step_retrospective(
                    t, fiscal_year, fiscal_quarter, tr,
                    consensus_by_ticker.get(t), consensus_delta_by_ticker.get(t),
                )
                if retro:
                    retrospective_by_ticker[t] = retro
                    tr["_retrospective"] = retro
            except Exception:
                log.exception("[Phase9] retrospective 실패 (%s)", t)

    # Longitudinal history 영속화
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

    # 커스텀 분석 (있을 때, Opus) — 텔레그램 발송 X, PDF에 함께
    custom_answer = ""
    if custom_question:
        log.info("[Phase 9] 커스텀 분석 합성 (Opus): %s", custom_question[:80])
        custom_answer = await _step_synthesize_custom(
            custom_question, transcripts, financials, fiscal_label, verify_by_ticker,
            consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
            ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
            hyper_walker=hyper_walker,
            market_reaction_by_ticker=market_reaction_by_ticker,
        ) or ""

    # ============================================================
    # Phase 9 — 텔레그램 송출: 정확히 [한글번역 × N] + [Editor's Pick] + [PDF]
    # ============================================================

    # 메시지 2 (종목당): 어닝콜 한글 번역 (Sonnet 1회/종목)
    log.info("[Phase 9] 한글 번역 합성 시작 (%d ticker)", len(transcripts))
    for t, tr in transcripts.items():
        ko = await _step_translate_transcript(t, tr)
        if ko:
            try:
                await send_text_chunked(bot, chat_id, ko, parse_mode="Markdown")
            except Exception:
                log.exception("[Phase 9] 한글 번역 발송 실패 (Markdown 파싱 가능성) — 평문 재시도 (%s)", t)
                try:
                    await send_text_chunked(bot, chat_id, ko)
                except Exception:
                    log.exception("[Phase 9] 평문 재시도도 실패 (%s)", t)
        else:
            log.warning("[Phase 9] %s 한글 번역 실패 — 메시지 스킵", t)

    # 메시지 3: Editor's Pick (Opus 1회) — 시장 기대 vs 진짜 중요한 것
    log.info("[Phase 9] Editor's Pick 합성 (Opus) …")
    pick = await _step_editors_pick(
        transcripts, financials, fiscal_label, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker, market_reaction_by_ticker,
        industry_summary=industry_summary, counter_summary=counter_summary, meta_text=meta_text,
    )
    if pick:
        try:
            await send_text_chunked(bot, chat_id, pick, parse_mode="Markdown")
        except Exception:
            log.exception("[Phase 9] Editor's Pick Markdown 발송 실패 — 평문 재시도")
            try:
                await send_text_chunked(bot, chat_id, pick)
            except Exception:
                log.exception("[Phase 9] Editor's Pick 평문 재시도도 실패")
    else:
        log.warning("[Phase 9] Editor's Pick 합성 실패 — 메시지 스킵")

    # 메시지 4: PDF 빌드 + 발송 (모든 deep dive 포함)
    log.info("[Phase 9] PDF 빌드 …")
    pdf_path = await _step_build_pdf(
        tickers=list(transcripts.keys()),
        fiscal_period=fiscal_label,
        transcripts=transcripts,
        financials=financials,
        industry_summary=industry_summary,
        custom_question=custom_question,
        custom_answer=custom_answer,
        verify_by_ticker=verify_by_ticker,
        consensus_by_ticker=consensus_by_ticker,
        consensus_delta_by_ticker=consensus_delta_by_ticker,
        market_reaction_by_ticker=market_reaction_by_ticker,
        counter_summary=counter_summary,
        meta_text=meta_text,
    )
    if pdf_path and pdf_path.exists():
        await send_pdf(
            bot, chat_id, pdf_path,
            caption=(
                f"📄 전체 deep dive — {', '.join(tickers[:6])} · {fiscal_label}\n"
                "(synthesis · multi-perspective · counter · meta · 차트 · 재무)"
            ),
        )
        log.info("[earnings: send_results 완료] PDF=%s tickers=%s grounded=%d/%d",
                 pdf_path.name, tickers, grounded_n, len(transcripts))
    else:
        await _safe_send(bot, chat_id, "⚠️ PDF 생성 실패 — 위 텔레그램 메시지만 확인해 주세요.")
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


async def _step_fetch_mdna_diff(tickers: list[str]) -> dict[str, Any]:
    """Phase 7E — 10-K Item 7 MD&A 1년 diff (Track I 패턴 재사용)."""
    from src import summarizer
    from src.earnings import sec_edgar
    from src.idea_bot import _parse_json
    loop = asyncio.get_running_loop()
    snaps = await asyncio.gather(
        *[loop.run_in_executor(None, sec_edgar.fetch_10k_mdna, t) for t in tickers],
        return_exceptions=True,
    )
    system = _load_prompt("earnings_mdna_diff")
    out: dict[str, Any] = {}
    model = _extract_model()

    async def _diff_one(ticker: str, current, prior) -> dict | None:
        if current is None or prior is None:
            return None
        user_msg = (
            f"# ticker: {ticker}\n"
            f"# current_filed: {current.filed} (chars: {current.risk_chars:,})\n"
            f"# prior_filed: {prior.filed} (chars: {prior.risk_chars:,})\n\n"
            f"=== CURRENT 10-K MD&A START ===\n{current.risk_text}\n=== END ===\n\n"
            f"=== PRIOR 10-K MD&A START ===\n{prior.risk_text}\n=== END ===\n\n"
            "schema 그대로 JSON 출력."
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
                max_tokens=4500,
                context=f"earnings-mdna-diff:{ticker}",
            )
        try:
            content = await loop.run_in_executor(None, _call)
        except Exception:
            log.exception("MD&A diff LLM 실패 (%s)", ticker)
            return None
        return _parse_json(content) if content else None

    diff_jobs = []
    for t, snap_pair in zip(tickers, snaps):
        if isinstance(snap_pair, Exception):
            out[t] = None
            continue
        current, prior = snap_pair
        if current is None or prior is None:
            out[t] = {"diff_skipped": True, "reason": "MD&A 페어 미확보"}
            continue
        diff_jobs.append((t, current, prior))
    if diff_jobs:
        results = await asyncio.gather(*[_diff_one(t, c, p) for t, c, p in diff_jobs])
        for (t, current, prior), res in zip(diff_jobs, results):
            payload = res or {}
            payload.setdefault("ticker", t)
            payload.setdefault("current_filed", current.filed)
            payload.setdefault("prior_filed", prior.filed)
            out[t] = payload
    return out


def _fmt_mdna_diff_text(rd: dict | None) -> str:
    """텔레그램용 한국어 요약."""
    if not rd:
        return ""
    ticker = rd.get("ticker", "?")
    if rd.get("diff_skipped"):
        return f"📊 MD&A diff({ticker}): {rd.get('reason','추출 실패')}"
    lines = [f"📊 MD&A 1-year diff({ticker}) — {rd.get('current_filed','?')} vs {rd.get('prior_filed','?')}"]
    def _add(label, key, max_items=3):
        items = rd.get(key) or []
        if not items:
            return
        lines.append(f"  · {label} ({len(items)}건):")
        for it in items[:max_items]:
            t = (it.get("topic") or it.get("segment") or it.get("area") or it.get("theme") or "?")[:60]
            lines.append(f"    - {t}")
    _add("📈 매출 동인 변화", "revenue_drivers_change")
    _add("💼 세그먼트 마진", "segment_margin_evolution")
    _add("⛓️ 공급망 톤", "supply_chain_commentary_shift")
    _add("🔮 전망 변화", "forward_outlook_change")
    _add("🌐 매크로", "macro_analysis_shift")
    if rd.get("pm_takeaway"):
        lines.append(f"  · PM takeaway: {rd.get('pm_takeaway')}")
    return "\n".join(lines)


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
    """티커별 최근 N일 Form 4 인사이더 매매 (Track F + Phase 7E XML 본문 파싱).

    1단계: fetch_recent_form4로 메타 수집 (병렬).
    2단계: 각 form의 primary_doc_url에서 XML 파싱 → 거래코드·수량·달러·관계 추출.
    """
    from src.earnings import sec_edgar
    loop = asyncio.get_running_loop()
    metas = await asyncio.gather(
        *[loop.run_in_executor(None, sec_edgar.fetch_recent_form4, t, window_days) for t in tickers],
        return_exceptions=True,
    )
    # 2단계: 모든 종목 합쳐서 XML 파싱 병렬 (forms 단위)
    parse_jobs = []
    out: dict[str, Any] = {}
    for t, s in zip(tickers, metas):
        if isinstance(s, Exception):
            log.warning("Form 4 fetch 실패 (%s): %s", t, s)
            out[t] = None
            continue
        out[t] = s
        if s is not None:
            parse_jobs.append((t, s))
    if parse_jobs:
        await asyncio.gather(*[
            loop.run_in_executor(None, sec_edgar.parse_insider_summary, s, 8)
            for _, s in parse_jobs
        ], return_exceptions=True)
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


async def _step_fetch_market_reaction(
    tickers: list[str],
    transcripts: dict[str, dict] | None = None,
    consensus_by_ticker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """콜 발표 전후 시장 반응 병렬 fetch (Phase 8 Track M).

    call_date는 transcripts[t]["call_date"]에서 우선 추출, 없으면 None (모듈이 추정).
    {ticker: MarketReaction | None}. 실패는 graceful.
    """
    from src.earnings import market_reaction as mr
    transcripts = transcripts or {}
    consensus_by_ticker = consensus_by_ticker or {}
    loop = asyncio.get_running_loop()

    def _one(t: str):
        tr = transcripts.get(t) or {}
        cd = tr.get("call_date") or None
        snap = consensus_by_ticker.get(t)
        return mr.fetch_market_reaction(t, call_date=cd, consensus_snap=snap)

    results = await asyncio.gather(
        *[loop.run_in_executor(None, _one, t) for t in tickers],
        return_exceptions=True,
    )
    out: dict[str, Any] = {}
    for t, snap in zip(tickers, results):
        if isinstance(snap, Exception):
            log.warning("market reaction fetch 실패 (%s): %s", t, snap)
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
    market_reaction_by_ticker: dict[str, Any] | None = None,
) -> tuple[str | None, dict]:
    """비교 합성 (Opus, 딥리서치급) + Phase 7B 환각 게이트.

    반환: (text, gate_meta) — gate_meta는 텔레그램 알림·history 영속화용.
    품질 위반(인용 비율 ≥25%) 또는 너무 짧음(<8000자) 시 opus 1회 재합성 (max 2 attempts).
    """
    from src import summarizer
    from src.earnings import quality_gate as qg
    system = _load_prompt("earnings_synthesis")
    user_payload = _build_synthesis_payload(
        transcripts, financials, fiscal_period, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker, market_reaction_by_ticker,
    )
    loop = asyncio.get_running_loop()

    def _call(extra_user: str = ""):
        client = summarizer.get_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_payload + (("\n\n" + extra_user) if extra_user else "")},
        ]
        return summarizer.chat_with_retry(
            client,
            model=_synthesis_model(),
            messages=messages,
            temperature=0.3,
            max_tokens=16000,
            context="earnings-synthesis",
        )

    allowed_tickers = list(transcripts.keys())
    content: str | None = None
    gate_result = qg.GateResult(layer="synthesis")
    undisclosed_result = qg.GateResult(layer="undisclosed_lint")
    length_ok = False
    truncated = False
    trunc_reason = ""
    attempts_done = 0
    final_chars = 0

    MAX_ATTEMPTS = 3
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts_done = attempt
        extra = ""
        if attempt > 1 and (
            gate_result.violations or not length_ok or truncated or not undisclosed_result.passed
        ):
            problems: list[str] = []
            if gate_result.violations:
                problems.append("(a) 위 payload에 실제 존재하는 데이터만 인용 (인용 위반 발견)")
                vio_summary = "\n".join(
                    f"- {v.field}: {v.reason}" for v in gate_result.violations[:8]
                )
                problems.append("인용 위반 목록:\n" + vio_summary)
            if not length_ok:
                problems.append("(b) 길이는 반드시 10000자 이상 — 모든 섹션 최소 분배 준수")
            if truncated:
                problems.append(
                    f"(c) 직전 출력이 잘림 ({trunc_reason}) — §0부터 끝까지 완전 재작성. "
                    "마지막 §10 결론까지 자연스러운 종결(. / 다. / 요.)로 마무리할 것"
                )
            if not undisclosed_result.passed:
                problems.append(
                    "(d) '수치 미공개'·'정량 미공개'·'(미공개)' 문구 사용 금지. "
                    "표 셀에 실제 숫자가 없으면 '—'만 쓰고, 모든 셀 [출처] bracket 의무. "
                    "SEC payload·call verbatim에서 정량 가져와 채워라."
                )
            extra = "다음 검증 실패가 발견됐다. 재작성 규칙:\n\n" + "\n\n".join(problems)
        try:
            content = await loop.run_in_executor(None, _call, extra)
        except Exception:
            log.exception("비교 합성 실패 (attempt=%d)", attempt)
            content = None
            break
        if not content:
            break
        # 검증
        gate_result = qg.verify_synthesis_citations(
            content, user_payload, allowed_tickers=allowed_tickers, threshold=0.25,
        )
        length_ok, final_chars = qg.enforce_min_length(content, min_chars=8000)
        truncated, trunc_reason = qg.looks_truncated(content, expected_sections=8)
        undisclosed_result = qg.verify_no_undisclosed(content)
        if gate_result.passed and length_ok and not truncated and undisclosed_result.passed:
            break

    gate_meta = {
        "attempts": attempts_done,
        "synthesis_chars": final_chars,
        "length_ok": length_ok,
        "truncated": truncated,
        "truncation_reason": trunc_reason,
        "citation_total": gate_result.total_checked,
        "citation_violations": len(gate_result.violations),
        "citation_ratio": gate_result.violation_ratio,
        "citation_passed": gate_result.passed,
        "undisclosed_violations": len(undisclosed_result.violations),
        "undisclosed_passed": undisclosed_result.passed,
        "violations": [
            {"field": v.field, "reason": v.reason, "fragment": v.fragment[:80]}
            for v in gate_result.violations[:10]
        ],
        "undisclosed_samples": [
            {"fragment": v.fragment[:120]}
            for v in undisclosed_result.violations[:5]
        ],
    }
    return (content or None), gate_meta


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


async def _step_multi_perspective(
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
    market_reaction_by_ticker: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Phase 7D — 동일 cached payload + 3개 system prompt (bull/bear/neutral) 병렬 sonnet 합성.

    반환: {"bull": text, "bear": text, "neutral": text} (실패 항목은 빈 문자열).
    """
    from src import summarizer
    user_payload = _build_synthesis_payload(
        transcripts, financials, fiscal_period, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker, market_reaction_by_ticker,
    )
    perspectives = {
        "bull": _load_prompt("earnings_bull"),
        "bear": _load_prompt("earnings_bear"),
        "neutral": _load_prompt("earnings_neutral"),
    }
    loop = asyncio.get_running_loop()
    model = _extract_model()  # sonnet — 비용 절약

    def _call_one(system: str):
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.3,
            max_tokens=4500,
            context="earnings-multi-perspective",
        )

    results = await asyncio.gather(
        *[loop.run_in_executor(None, _call_one, sys_p) if sys_p else asyncio.sleep(0, result=None)
          for sys_p in perspectives.values()],
        return_exceptions=True,
    )
    out: dict[str, str] = {}
    for (name, _), res in zip(perspectives.items(), results):
        if isinstance(res, Exception) or not res:
            log.warning("[multi-perspective] %s 실패", name)
            out[name] = ""
        else:
            out[name] = res
    return out


async def _step_meta_synthesis(perspectives: dict[str, str]) -> str | None:
    """Phase 7D — bull/bear/neutral 3개 보고서를 받아 opus 1회 메타 합성. §9 출력."""
    from src import summarizer
    if not perspectives or not any(perspectives.values()):
        return None
    system = _load_prompt("earnings_meta")
    if not system:
        return None
    user_msg = (
        "다음 3개 보고서는 같은 데이터에서 bull/bear/neutral 시각으로 작성됐다. "
        "schema 그대로 §9 메타 합성을 출력하라.\n\n"
        f"## BULL VIEW\n{perspectives.get('bull') or '(empty)'}\n\n"
        f"## BEAR VIEW\n{perspectives.get('bear') or '(empty)'}\n\n"
        f"## NEUTRAL VIEW\n{perspectives.get('neutral') or '(empty)'}"
    )
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_synthesis_model(),  # opus
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=5500,
            context="earnings-meta-synthesis",
        )
    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("[meta synthesis] 실패")
        return None
    return content or None


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
    market_reaction_by_ticker: dict[str, Any] | None = None,
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
        hyper_walker, market_reaction_by_ticker,
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


# ------------------------------------------------------------------
# Phase 9 — Editor's Pick (시장 기대 vs 진짜 중요한 것, Opus 1회)
# ------------------------------------------------------------------
async def _step_editors_pick(
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
    market_reaction_by_ticker: dict[str, Any] | None = None,
    industry_summary: str = "",
    counter_summary: str = "",
    meta_text: str = "",
) -> str | None:
    """Phase 9 — PM Decision Brief 한 장 (Opus 1회). 1200-2000자 압축 합성.

    합성·counter·meta가 끝난 뒤 호출 — 모든 컨텍스트가 모인 상태에서 PM이 30초에 의사결정할
    Editor's Pick 출력. 텔레그램 메시지로 발송.
    """
    from src import summarizer
    system = _load_prompt("earnings_editors_pick")
    if not system:
        log.warning("earnings_editors_pick.txt 로드 실패 — Editor's Pick 스킵")
        return None
    base_payload = _build_synthesis_payload(
        transcripts, financials, fiscal_period, verify_by_ticker,
        consensus_by_ticker, consensus_delta_by_ticker, diff_by_ticker,
        ecosystem_by_ticker, insider_by_ticker, events_8k_by_ticker, risk_diff_by_ticker,
        hyper_walker, market_reaction_by_ticker,
    )
    # 합성·counter·meta excerpt 첨가 — Editor's Pick은 이 결과의 가장 응축된 결론을 받아야 함
    extras: list[str] = []
    if industry_summary:
        extras.append("## (참고) 비교합성 본문 excerpt (Opus 합성, 3000자):\n" + industry_summary[:3000])
    if counter_summary:
        extras.append("## (참고) Counter-thesis excerpt (Opus, 2000자):\n" + counter_summary[:2000])
    if meta_text:
        extras.append("## (참고) Multi-perspective meta excerpt (Opus, 1500자):\n" + meta_text[:1500])
    user_payload = base_payload + ("\n\n" + "\n\n".join(extras) if extras else "")
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_synthesis_model(),  # Opus
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.3,
            max_tokens=2800,
            context="earnings-editors-pick",
        )
    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("[Phase 9] Editor's Pick 합성 실패")
        return None
    return content or None


# ------------------------------------------------------------------
# Phase 9 — 어닝콜 한글 번역 (Sonnet 1회/종목)
# ------------------------------------------------------------------
async def _step_translate_transcript(ticker: str, tr: dict) -> str | None:
    """Phase 9 — 종목별 extract → 자연스러운 한국어 본문 + 굵게/기울임 강조.

    텔레그램 메시지로 발송 (parse_mode=Markdown). 3500-5500자 목표.
    """
    from src import summarizer
    system = _load_prompt("earnings_transcript_korean")
    if not system:
        log.warning("earnings_transcript_korean.txt 로드 실패 — 번역 스킵 (%s)", ticker)
        return None
    # extract JSON 전체를 LLM에 그대로 (verbatim·인용 정보 보존 위해 트림 최소)
    safe_tr = {
        k: v for k, v in (tr or {}).items()
        if not k.startswith("_") and k not in ("raw_text", "_consensus_snap")
    }
    user_payload = (
        f"# 종목: {ticker}\n"
        f"# 분기: {safe_tr.get('fiscal_period') or '?'}\n"
        f"# Grounded: {safe_tr.get('grounded', False)}\n\n"
        f"## 추출 JSON\n{json.dumps(safe_tr, ensure_ascii=False)[:18000]}"
    )
    loop = asyncio.get_running_loop()

    def _call():
        client = summarizer.get_client()
        return summarizer.chat_with_retry(
            client,
            model=_extract_model(),  # Sonnet — 번역 작업이라 충분
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.2,
            max_tokens=6500,
            context=f"earnings-translate-{ticker}",
        )
    try:
        content = await loop.run_in_executor(None, _call)
    except Exception:
        log.exception("[Phase 9] 한글 번역 실패 (%s)", ticker)
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
    market_reaction_by_ticker: dict[str, Any] | None = None,
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
            hyper_walker, market_reaction_by_ticker,
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
    consensus_by_ticker: dict[str, Any] | None = None,
    consensus_delta_by_ticker: dict[str, list] | None = None,
    market_reaction_by_ticker: dict[str, Any] | None = None,
    counter_summary: str = "",
    meta_text: str = "",
) -> Path | None:
    """PDF 빌드 (blocking → run_in_executor)."""
    from src.earnings import pdf_report
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
            consensus_by_ticker=consensus_by_ticker or {},
            consensus_delta_by_ticker=consensus_delta_by_ticker or {},
            market_reaction_by_ticker=market_reaction_by_ticker or {},
            counter_summary=counter_summary,
            meta_text=meta_text,
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
    market_reaction_by_ticker: dict[str, Any] | None = None,
) -> str:
    """LLM에 넘길 유저 메시지. 종목별 심층추출 + 재무 6년치 + 검증 + 컨센서스 + diff + 생태계 + 인사이더 + 8-K + 10-K risk diff + hyperscaler capex 모자이크 + 시장 반응(Phase 8 Track M)."""
    from src.earnings.sec_edgar import fmt_usd, events_8k_payload_block
    from src.earnings.consensus import consensus_payload_block
    from src.earnings.ecosystem import ecosystem_payload_block
    from src.earnings.sec_edgar import fmt_insider_summary
    from src.earnings.market_reaction import market_reaction_payload_block
    from src.earnings import history as _history
    verify_by_ticker = verify_by_ticker or {}
    consensus_by_ticker = consensus_by_ticker or {}
    consensus_delta_by_ticker = consensus_delta_by_ticker or {}
    diff_by_ticker = diff_by_ticker or {}
    ecosystem_by_ticker = ecosystem_by_ticker or {}
    insider_by_ticker = insider_by_ticker or {}
    events_8k_by_ticker = events_8k_by_ticker or {}
    risk_diff_by_ticker = risk_diff_by_ticker or {}
    market_reaction_by_ticker = market_reaction_by_ticker or {}
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
        # 시장 반응 (Phase 8 Track M) — 주가·alpha·옵션·target revision
        mr_snap = market_reaction_by_ticker.get(t)
        if mr_snap is not None:
            mr_block = market_reaction_payload_block(mr_snap)
            if mr_block:
                parts.append(mr_block)
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
# ------------------------------------------------------------------
# Pre-emptive watch poller (cron job)
# ------------------------------------------------------------------
async def earnings_watch_poll_job(bot: Bot) -> None:
    """orchestrator APScheduler가 호출. watch된 종목의 새 콜 자동 감지·발송.

    흐름:
      1) earnings_watchlist.all_tickers()로 모든 watched ticker 수집.
      2) 각 ticker별 Yahoo `calendarEvents`로 다음 어닝 예정일 확인 → 윈도 안인지 게이팅.
         (예정일 미상이면 안전하게 항상 시도; AV 25/day는 state_store dedup이 보호.)
      3) state_store dedup 키 `earnings_watch:{TICKER}:Q{q}_{year}` — 이미 발송이면 skip.
      4) `transcript_source.fetch_full_transcript` 시도 → grounded + 비어 있지 않으면 새 콜.
      5) fan-out: `subscribers_of(ticker)` 각 chat_id로 풀 분석 파이프라인 1회씩 실행.
      6) 성공 시 state_store에 mark_seen.
    """
    from datetime import datetime as _dt
    from src import state_store, summarizer
    from src.earnings import earnings_watchlist as wl
    from src.earnings import consensus
    from src.earnings import transcript_source as ts

    log.info("[earnings_watch_poll] 시작")
    tickers = sorted(wl.all_tickers())
    if not tickers:
        log.info("[earnings_watch_poll] watched ticker 없음 — skip")
        return

    today = _dt.utcnow().date()
    loop = asyncio.get_running_loop()
    client = summarizer.get_client()

    # 1) 다음 어닝 예정일 일괄 조회 (Yahoo는 cheap)
    cal_dates = await asyncio.gather(
        *[loop.run_in_executor(None, consensus.fetch_next_earnings_date, t) for t in tickers],
        return_exceptions=True,
    )

    fired = 0
    skipped: list[str] = []

    for ticker, raw_date in zip(tickers, cal_dates):
        # 2) calendar gating
        delta = None
        edate = None
        if isinstance(raw_date, str):
            try:
                edate = _dt.strptime(raw_date, "%Y-%m-%d").date()
                delta = (today - edate).days
            except Exception:
                pass

        # 2-1) pre-call 윈도 (D-2 ~ D-1) — pre-call 보고서 자동 발사
        if edate is not None and delta is not None and -2 <= delta <= -1:
            ref_date = edate
            y_pre = ref_date.year
            q_pre = (ref_date.month - 1) // 3 + 1
            from src.earnings import precall as _precall
            if not _precall.exists(ticker, y_pre, q_pre):
                fiscal_label_pre = f"Q{q_pre} {y_pre}"
                subs = wl.subscribers_of(ticker)
                if subs:
                    log.info("[earnings_watch_poll] %s pre-call window 발사 (delta=%s)", ticker, delta)
                    for cid in subs:
                        asyncio.create_task(
                            _watch_fire_pre_call(bot, cid, ticker, y_pre, q_pre, fiscal_label_pre)
                        )

        # 2-2) actual transcript 윈도 (D-1 ~ D+4)만 transcript 시도
        in_window = True
        if delta is not None:
            in_window = -1 <= delta <= 4
        if not in_window:
            skipped.append(f"{ticker}(date={raw_date}, out of window)")
            continue

        # 3) (year, quarter) 추정 — 가장 최근 분기. 예정일이 있으면 그 기준, 없으면 today.
        ref_date = today
        if isinstance(raw_date, str):
            try:
                ref_date = _dt.strptime(raw_date, "%Y-%m-%d").date()
            except Exception:
                pass
        y = ref_date.year
        q = (ref_date.month - 1) // 3 + 1

        # 4) dedup — 같은 (ticker, year, quarter) 이미 발송했으면 skip
        seen_key = f"{ticker}:Q{q}_{y}"
        if seen_key in state_store.seen("earnings_watch"):
            skipped.append(f"{ticker}(이미 발송 Q{q} {y})")
            continue

        # 5) transcript 가용성 체크
        try:
            doc = await loop.run_in_executor(None, ts.fetch_full_transcript, client, ticker, y, q)
        except Exception:
            log.exception("[earnings_watch_poll] transcript fetch 실패 (%s Q%s %s)", ticker, q, y)
            doc = None
        if doc is None or not getattr(doc, "grounded", False) or len(doc.full_text or "") < 2000:
            skipped.append(f"{ticker}(Q{q} {y} transcript 아직 미공개)")
            continue

        # 6) 구독자 fan-out
        subs = wl.subscribers_of(ticker)
        if not subs:
            log.info("[earnings_watch_poll] %s — 구독자 없음 (race), skip", ticker)
            continue
        fiscal_label = f"Q{q} {y}"

        # 즉시 mark_seen — 한 번 fire하면 race 시에도 두 번 안 보내게
        try:
            state_store.mark_seen("earnings_watch", [seen_key])
        except Exception:
            log.exception("[earnings_watch_poll] mark_seen 실패")

        await _safe_send(
            bot, subs[0],
            f"🔔 watch 감지 — {ticker} {fiscal_label} transcript 확보 ({len(doc.full_text):,}자). 자동 풀 분석 시작 …",
        )

        # 구독자별로 풀 파이프라인 실행 (각자에게 보고서 fan-out)
        for cid in subs:
            asyncio.create_task(_watch_fire_pipeline(bot, cid, ticker, fiscal_label, y, q))
        fired += 1

    log.info(
        "[earnings_watch_poll] 완료 — fired=%d skipped=%d (총 %d ticker)",
        fired, len(skipped), len(tickers),
    )
    if skipped and fired == 0:
        log.info("[earnings_watch_poll] skip 사유 sample: %s", skipped[:8])


async def _watch_fire_pre_call(
    bot: Bot, chat_id: str, ticker: str, year: int, quarter: int, fiscal_label: str,
) -> None:
    """watch_poll에서 D-2~D-1 pre-call 자동 발사. precall 캐시 저장 + 발송 1회."""
    from src.earnings import precall
    if precall.exists(ticker, year, quarter):
        return
    await _safe_send(
        bot, chat_id,
        f"🎯 [watch] {ticker} 어닝 D-{abs((_today_utc() - _to_date(fiscal_label, year, quarter)).days) if False else '1~2'} — pre-call 자동 발사 …",
    )
    report, snap = await _step_synthesize_pre_call(ticker, year, quarter, fiscal_label)
    if not report:
        await _safe_send(bot, chat_id, f"⚠️ {ticker} pre-call 합성 실패 — 데이터 부족 가능")
        return
    await send_text_chunked(bot, chat_id, report)
    try:
        precall.save(
            ticker=ticker, year=year, quarter=quarter, fiscal_label=fiscal_label,
            report_text=report, consensus=snap,
        )
        await _safe_send(bot, chat_id, "💾 pre-call 캐시 저장 — 실제 콜 등재 시 자동 비교 합성이 따라옵니다.")
    except Exception:
        log.exception("[watch] pre-call 캐시 저장 실패")


def _today_utc():
    from datetime import datetime as _dt
    return _dt.utcnow().date()


def _to_date(fiscal_label: str, year: int, quarter: int):
    # 정확한 어닝 발표일을 알 수 없어 분기 중간 날짜로 대체 (UI용 표시만)
    from datetime import date as _d
    return _d(year, max(1, min(12, quarter * 3 - 1)), 15)


async def _watch_fire_pipeline(bot: Bot, chat_id: str, ticker: str, fiscal_label: str, year: int, quarter: int) -> None:
    """단일 (ticker, chat_id, period) 풀 분석 — _run_pipeline 재사용 (FakeUpdate)."""
    class _FakeChat:
        def __init__(self, cid: str): self.id = int(cid)

    class _FakeMessage:
        def __init__(self, cid: str, text: str):
            self.text = text
            self.chat = _FakeChat(cid)

        async def reply_text(self, *a, **kw) -> None:
            try:
                await bot.send_message(chat_id=self.chat.id,
                                       text=str(a[0]) if a else (kw.get("text") or ""))
            except Exception:
                log.exception("[earnings_watch_poll] reply 실패")

    class _FakeUpdate:
        def __init__(self, cid: str, text: str):
            self.effective_chat = _FakeChat(cid)
            self.message = _FakeMessage(cid, text)

    class _FakeContext:
        def __init__(self):
            self.bot = bot
            self.args: list[str] = []

    user_text = f"{ticker} {fiscal_label}"
    try:
        await _run_pipeline(_FakeUpdate(chat_id, user_text), _FakeContext(), user_text)
    except Exception:
        log.exception("[earnings_watch_poll] 풀 분석 실패 (%s %s)", ticker, fiscal_label)


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
    ("preearnings", "🎯 콜 발표 전 사전 기대치 (`/preearnings MSFT`)"),
    ("watch", "🔭 종목 어닝 자동 발송 예약 (`/watch MSFT NVDA`)"),
    ("unwatch", "🚫 자동 발송 해제"),
    ("watchlist", "📋 현재 watch 목록 + 다음 어닝 예정"),
    ("backfill", "🔁 longitudinal history 시드 (`/backfill MSFT 6`)"),
    ("credibility", "📈 봇 누적 적중률 (`/credibility NVDA`)"),
    ("query", "🔍 knowledge_db 자유 검색 (`/query SK Hynix`)"),
    ("digest", "📰 watchlist 주간 digest (`/digest 7`)"),
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
    app.add_handler(CommandHandler("preearnings", _cmd_preearnings))
    app.add_handler(CommandHandler("watch", _cmd_watch))
    app.add_handler(CommandHandler("unwatch", _cmd_unwatch))
    app.add_handler(CommandHandler("watchlist", _cmd_watchlist))
    app.add_handler(CommandHandler("backfill", _cmd_backfill))
    app.add_handler(CommandHandler("credibility", _cmd_credibility))
    app.add_handler(CommandHandler("query", _cmd_query))
    app.add_handler(CommandHandler("digest", _cmd_digest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_self_test(app))
    except RuntimeError:
        pass
    return app
