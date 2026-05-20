"""N종목 측면 비교 → 한 편의 통합 비교 리포트 + PDF.

흐름 (사용자 결정: "완전 비교"):
  1. 각 종목에 deep_research.run() 순차 실행 (PIPELINE_LOCK 직렬화)
     → 각 종목별 통합 마크다운 리포트 텍스트 확보 (raw 데이터도 보관)
  2. claude-sonnet으로 비교 합성 — aspect (capex 등) 중심
  3. 마크다운 → PDF 변환 (Playwright)
  4. PDF + 각 종목 raw 참고 PDF 일괄 발송

비용 ~$0.15 × N (research) + $0.10-0.15 (합성) + 0 (PDF).
시간 8-15분 × N + 2-3분 (합성+PDF).
2종목 ≈ 30-40분, $0.45. 3종목 ≈ 50-60분, $0.65.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import Bot

from src.bot_helpers import send_pdf, send_text_chunked
from src.pipeline_lock import PIPELINE_LOCK
from src.summarizer import OpenRouterCreditExhausted, chat_with_retry, get_client

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

SYNTHESIS_MODEL_ENV = "IDEA_SYNTHESIS_MODEL"  # deep_research와 동일 — claude-sonnet-4.5
DEFAULT_SYNTHESIS_MODEL = "anthropic/claude-sonnet-4.5"

COMPARE_PROMPT = """당신은 헤지펀드 PM 입장에서 종목 N개를 특정 측면으로 비교한다.
경쟁자(다른 최고 투자자)보다 빠르게 우열을 짚어내는 게 목표.

[비교 측면]
{aspect}

[종목별 raw 통합 리포트]
{reports_block}

작성 요구사항 (마크다운, 7000-10000자):

# 📊 {names_joined} — {aspect} 비교 분석

## Executive Summary
3-5줄. {aspect} 관점에서 우열 + 시나리오별 매력도.

## 1. {aspect} 핵심 수치 비교
- 표 형식 (각 종목 행, 핵심 수치 컬럼)
- 최근 3-5년 추이 (절대값 + YoY)
- 출처 명시 ([DART 사업보고서], [<broker> 리포트], [web])

## 2. 추세·전략 비교
각 종목 1-2 문단:
- {aspect} 절대 규모 + 추세 방향
- 전략적 의도 (확장·유지·축소 사이클)
- 자금 조달 (현금 vs 차입 vs 증자)
- 효율성·ROI 평가

## 3. 비즈니스 모델 차이 (간략)
{aspect} 패턴 차이가 비즈니스 모델 차이에서 오는 부분 (1-2 문단).

## 4. 시장·정책 영향
{aspect}에 영향 줄 외생 변수 (보조금·금리·산업 사이클 등) 종목별 노출도.

## 5. 시나리오별 매력도
- 시나리오 A (예: AI 인프라 호황): 어느 종목 더 유리, 왜
- 시나리오 B (예: 금리 상승·자금 비용 증가): 어느 종목 더 방어적
- 시나리오 C (선택): 그 외 핵심 시나리오

## 6. 리스크·카운터-thesis (각 종목 1-2개)

## 7. 결론
1-2 문단. {aspect} 관점에서 어느 종목이 어떤 상황·기간에서 매력. 모니터링 트리거.

[규칙]
- 모든 수치·주장에 출처 명시
- 추상 표현 금지 ("성장 기대"·"좋아 보임")
- 종목별 raw 리포트에 명시된 정보만 사용. 임의 추정 금지.
- 표 사용 적극적 (마크다운 | --- | 문법). 시각적 비교 우선.
- 결론 부분이 사용자에게 가장 가치 — 결단력 있게.
"""


@dataclass
class CompareInput:
    name: str
    ticker: str
    report_md: str            # deep_research 합성 결과 텍스트
    research_pdfs: list = field(default_factory=list)  # 참고 PDF 경로들
    chart_png: Optional[bytes] = None
    error: str = ""


def _build_reports_block(inputs: list[CompareInput]) -> str:
    parts = []
    for i, r in enumerate(inputs, start=1):
        if r.error:
            parts.append(f"[종목 {i}] {r.name} ({r.ticker}) — 데이터 수집 실패: {r.error}")
            continue
        parts.append(f"[종목 {i}] {r.name} ({r.ticker})\n{r.report_md[:18000]}")
    return "\n\n========================================\n\n".join(parts)


async def _run_deep_research_capture(bot: Bot, chat_id: str, name: str, ticker: str) -> CompareInput:
    """deep_research.run을 호출하지만, 사용자에게 발송하지 않고 결과만 capture.

    구현 방침: deep_research 모듈 변경 안 함. 대신 발송 단계를 우회하기 위해
    수집·합성 단계만 직접 호출 (private 함수 _collect_dart, _collect_wisereport,
    _collect_web, _synthesize 재사용).
    """
    out = CompareInput(name=name, ticker=ticker, report_md="")
    try:
        from src.deepdive import dart_client
        from src import deep_research
    except Exception as e:
        out.error = f"모듈 import 실패: {e}"
        return out

    loop = asyncio.get_running_loop()
    pair = await loop.run_in_executor(None, dart_client.get_corp_code, ticker)
    if not pair:
        out.error = "DART corp_code 매칭 실패"
        return out
    corp_code, corp_name = pair
    out.name = corp_name  # 정확한 이름으로 갱신

    download_root = Path(os.environ.get("DOWNLOAD_DIR", "./downloads")) / "compare" / ticker

    # PIPELINE_LOCK은 호출자(run_compare)가 보유. 여기서 lock 미사용 — 중첩 방지.
    try:
        dart_task = asyncio.create_task(
            deep_research._collect_dart(ticker, corp_code, corp_name, download_root / "dart")
        )
        wise_task = asyncio.create_task(
            deep_research._collect_wisereport(ticker, download_root / "wisereport")
        )
        web_task = asyncio.create_task(
            deep_research._collect_web(corp_name, ticker)
        )
        dart_data, wise_data, web_data = await asyncio.gather(
            dart_task, wise_task, web_task, return_exceptions=False,
        )
    except Exception as e:
        out.error = f"Stage 1 수집 실패: {e}"
        return out

    if dart_data.error and wise_data.error and web_data.error:
        out.error = "3축 모두 실패"
        return out

    try:
        out.report_md = await deep_research._synthesize(
            corp_name, ticker, dart_data, wise_data, web_data,
        )
    except Exception as e:
        log.exception("compare: %s synthesis 실패 — 폴백", ticker)
        out.report_md = f"# {corp_name} ({ticker})\n\n합성 실패. raw DART 비즈니스:\n{dart_data.business_text[:3000]}"

    # 참고 PDF 수집
    if dart_data.business_pdf:
        out.research_pdfs.append(("DART 사업보고서", dart_data.business_pdf))
    if dart_data.ir_pdf:
        out.research_pdfs.append(("DART IR자료", dart_data.ir_pdf))
    for r in wise_data.reports:
        p = r.get("path")
        if p:
            out.research_pdfs.append((f"증권사: {r.get('title','')[:60]}", p))
    out.chart_png = dart_data.chart_png
    return out


async def _synthesize_compare(
    names: list[str], aspect: str, inputs: list[CompareInput],
) -> str:
    aspect_clean = (aspect or "전반").strip()
    prompt = COMPARE_PROMPT.format(
        aspect=aspect_clean,
        names_joined=" vs ".join(names),
        reports_block=_build_reports_block(inputs),
    )
    log.info("compare synthesis 호출: aspect=%s, prompt %d chars, N=%d",
             aspect_clean, len(prompt), len(inputs))
    client = get_client()
    model = os.getenv(SYNTHESIS_MODEL_ENV) or DEFAULT_SYNTHESIS_MODEL
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(
            None,
            lambda: chat_with_retry(
                client,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8000,
                model=model,
                temperature=0.3,
                context=f"compare ({','.join(names[:3])})",
            ),
        )
        if text and len(text) > 500:
            return text.strip()
    except OpenRouterCreditExhausted:
        log.warning("OpenRouter 크레딧 부족 — compare 합성 폴백")
    except Exception:
        log.exception("compare 합성 실패 — 폴백")

    # 폴백: 각 종목 리포트 concat
    parts = [f"# {' vs '.join(names)} — {aspect_clean} 비교 (합성 실패 폴백)"]
    for r in inputs:
        parts.append(f"\n## {r.name} ({r.ticker})\n\n{r.report_md[:3000]}")
    return "\n".join(parts)


async def run_compare(
    bot: Bot, chat_id: str,
    pairs: list[tuple[str, str]],  # [(name, ticker), ...]
    aspect: str,
) -> None:
    """N종목 완전 비교 + PDF 발송.

    pairs는 intent_router.resolve_tickers의 결과. 각 종목당 8-15분 + 비교 합성
    2-3분 + PDF 30초. PIPELINE_LOCK으로 wisereport·DART 직렬화.
    """
    if not pairs:
        await send_text_chunked(bot, chat_id, "❌ 비교할 종목이 없습니다.")
        return
    if len(pairs) > 5:
        await send_text_chunked(bot, chat_id, "❌ 비교는 최대 5종목까지 지원합니다.")
        return

    names = [n for n, _ in pairs]
    tickers = [t for _, t in pairs]
    aspect_clean = (aspect or "전반").strip()
    started = datetime.now(KST)

    if PIPELINE_LOCK.locked():
        await send_text_chunked(
            bot, chat_id,
            f"⏳ 다른 작업 진행 중 — 끝나면 *{' vs '.join(names)}* {aspect_clean} 비교 시작",
            parse_mode="Markdown",
        )
    await send_text_chunked(
        bot, chat_id,
        f"📊 *{' vs '.join(names)}* — *{aspect_clean}* 비교 분석 시작\n"
        f"_각 종목 통합 리서치 → AI 비교 합성 → PDF — 약 {15 * len(pairs)}분 예상_",
        parse_mode="Markdown",
    )

    async with PIPELINE_LOCK:
        # Stage 1: 각 종목 deep_research 순차 (wisereport 세션 충돌 방지)
        inputs: list[CompareInput] = []
        for i, (name, ticker) in enumerate(pairs, start=1):
            log.info("compare [%d/%d] start: %s (%s)", i, len(pairs), name, ticker)
            await send_text_chunked(
                bot, chat_id,
                f"🔍 [{i}/{len(pairs)}] {name} ({ticker}) 데이터 수집·합성 중...",
            )
            inp = await _run_deep_research_capture(bot, chat_id, name, ticker)
            inputs.append(inp)
            log.info("compare [%d/%d] done: err=%r len=%d",
                     i, len(pairs), inp.error or "OK", len(inp.report_md))

        ok_count = sum(1 for r in inputs if not r.error)
        if ok_count < 2:
            await send_text_chunked(
                bot, chat_id,
                f"❌ 비교 불가 — 정상 수집된 종목이 {ok_count}개뿐.\n"
                + "\n".join(f"  {r.name}: {r.error or 'OK'}" for r in inputs),
            )
            return

        # Stage 2: 비교 합성
        await send_text_chunked(
            bot, chat_id, f"🧠 AI 비교 합성 중... ({aspect_clean} 관점)"
        )
        report_md = await _synthesize_compare(names, aspect_clean, inputs)

        # Stage 3: PDF 변환
        from src.pdf_export import markdown_to_pdf
        pdf_dir = Path(os.environ.get("DOWNLOAD_DIR", "./downloads")) / "compare" / "_pdf"
        pdf_path = pdf_dir / f"compare_{'_'.join(tickers)}_{aspect_clean[:20]}.pdf"
        pdf_result = await markdown_to_pdf(
            report_md, pdf_path,
            title=f"{' vs '.join(names)} — {aspect_clean} 비교",
        )

        elapsed = int((datetime.now(KST) - started).total_seconds() / 60)

        # Stage 4: 발송 — PDF + 마크다운 본문 (둘 다)
        if pdf_result:
            try:
                await send_pdf(
                    bot, chat_id, pdf_result,
                    caption=f"📊 {' vs '.join(names)} — {aspect_clean} 비교 분석 ({elapsed}분)",
                )
            except Exception:
                log.exception("compare PDF 발송 실패")
                await send_text_chunked(bot, chat_id, "⚠️ PDF 발송 실패 — 마크다운으로 대체 발송")

        # 본문 마크다운도 텔레그램에 (PDF + 본문 둘 다 제공 — 사용자 결정)
        await send_text_chunked(bot, chat_id, report_md, parse_mode="Markdown")

        # 참고 자료 PDF (각 종목 raw)
        await send_text_chunked(bot, chat_id, "📎 참고 자료 (각 종목 raw)")
        for inp in inputs:
            if inp.error:
                continue
            if inp.chart_png:
                try:
                    await bot.send_photo(
                        chat_id=chat_id, photo=inp.chart_png,
                        caption=f"📊 {inp.name} 분기 재무 추이",
                    )
                except Exception:
                    log.exception("chart 발송 실패")
            for label, p in inp.research_pdfs[:6]:  # 종목당 최대 6 PDF
                try:
                    await send_pdf(bot, chat_id, p, caption=f"📎 [{inp.name}] {label}")
                except Exception:
                    log.exception("참고 PDF 발송 실패: %s", p)

        # 완료 ack
        await send_text_chunked(
            bot, chat_id,
            f"✅ {' vs '.join(names)} {aspect_clean} 비교 완료 ({elapsed}분)",
        )
