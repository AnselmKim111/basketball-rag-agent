"""종목 통합 딥리서치 — DART + wisereport + 웹 → 한 편의 마크다운 리포트.

5단계 파이프라인:
  Stage 0 (verify)    : ticker → corp_code/name 확보, 시작 ack
  Stage 1 (collect)   : 3축 병렬 데이터 수집 (asyncio.gather)
                          A. DART (사업보고서/IR/분기재무/차트)
                          B. wisereport (종목 + 산업 리포트)
                          C. perplexity 웹 리서치 (최근 90일)
  Stage 2 (synthesize): claude-sonnet으로 통합 마크다운 리포트 합성
  Stage 3 (deliver)   : 리포트 + 차트 + 모든 PDF 일괄 발송 (중간 ack 없음)

비용 ~$0.15 (perplexity $0.02 + sonnet synthesis $0.13). 시간 8-15분.

격리:
  - 기존 deepdive/handler._execute, bot_worker._run_combined, category_bots는
    수정 안 함. 그들은 명시 명령 (/deepdive, /report) 그대로 동작.
  - 각 축 try/except — 한 축 실패해도 다른 축 진행 + 합성 단계로 폴백.
  - 합성 LLM 실패 → fallback로 raw 텍스트 concat 발송.
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
from src.summarizer import (
    OpenRouterCreditExhausted,
    chat_with_retry,
    get_client,
)

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 모델 — IdeaBot synthesis tier 재사용 (claude-sonnet-4.5)
SYNTHESIS_MODEL_ENV = "IDEA_SYNTHESIS_MODEL"
DEFAULT_SYNTHESIS_MODEL = "anthropic/claude-sonnet-4.5"

# 웹 리서치 — perplexity sonar-pro (idea_bot과 동일 키)
RESEARCH_MODEL_ENV = "IDEA_RESEARCH_MODEL"
DEFAULT_RESEARCH_MODEL = "perplexity/sonar-pro"

# DART 텍스트 추출 cap (사업보고서 큼 — 절제 필요)
DART_TEXT_CAP = 20_000
WISEREPORT_TEXT_CAP = 12_000

PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "deep_research.txt"

# 합성 프롬프트 누락 시 하드코딩 폴백 (절대 실패 안 하게).
DEFAULT_PROMPT = """당신은 한국 주식 기업 분석 전문가다. 아래 3축 raw 데이터를 종합해 통합 딥리서치 리포트를 작성하라.

종목: {name} ({ticker})

[DART 사업의 내용]
{dart_business}

[DART IR자료]
{dart_ir}

[DART 분기 재무]
{dart_financial}

[증권사 리포트]
{broker_reports}

[웹 리서치]
{web_research}

마크다운 8000자 내외, 출처 명시, Executive Summary / 업의 본질 / 핵심 투자 포인트 / 분기 실적 / 최근 이슈 / 시장 컨센서스 / 리스크 / 투자 결론 8개 섹션."""


# ------------------------------------------------------------------
# 데이터 클래스
# ------------------------------------------------------------------
@dataclass
class DartData:
    business_text: str = ""              # 사업의 내용
    ir_text: str = ""                     # IR자료
    financial_summary: str = ""          # 분기재무 텍스트 요약
    chart_png: Optional[bytes] = None
    business_pdf: Optional[Path] = None
    ir_pdf: Optional[Path] = None
    error: str = ""                       # 비어있으면 정상


@dataclass
class WisereportData:
    reports: list[dict] = field(default_factory=list)  # [{title, text, path}, ...]
    industry_titles: list[str] = field(default_factory=list)
    industry_paths: list = field(default_factory=list)
    error: str = ""


@dataclass
class WebData:
    text: str = ""
    error: str = ""


# ------------------------------------------------------------------
# Stage 1A: DART 데이터 수집
# ------------------------------------------------------------------
def _format_financial(fin) -> str:
    """fin (FinancialMetrics) → 텍스트 표 (LLM 입력용). None/빈 → 빈 문자열."""
    if not fin or not getattr(fin, "revenue_qoq", None):
        return ""
    quarters = sorted(fin.revenue_qoq.keys())[-12:]
    lines = ["분기  | 매출(백만원) | 영업이익 | 순이익"]
    for q in quarters:
        rev = fin.revenue_qoq.get(q, 0)
        op = fin.op_profit_qoq.get(q, 0)
        net = fin.net_profit_qoq.get(q, 0)
        lines.append(f"{q} | {rev:>14,} | {op:>10,} | {net:>10,}")
    return "\n".join(lines)


def _collect_dart_blocking(ticker: str, corp_code: str, corp_name: str, target_dir: Path) -> DartData:
    """blocking — DART 모든 데이터 한 번에 수집."""
    out = DartData()
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        from src.deepdive import dart_client, chart, forward_consensus
        from src import summarizer
    except Exception as e:
        out.error = f"DART 모듈 import 실패: {e}"
        return out

    # 1) 사업보고서
    try:
        report = dart_client.fetch_latest_business_report(corp_code)
        if report:
            pdf = dart_client.download_report_archive(report.rcept_no, target_dir)
            if pdf:
                out.business_pdf = pdf
                txt = summarizer._extract_pdf_text(pdf, max_chars=DART_TEXT_CAP)
                # "사업의 내용" 섹션이 있으면 그 부분만, 없으면 전체
                section = dart_client.extract_section(txt, "사업의 내용") if hasattr(dart_client, "extract_section") else None
                out.business_text = section or txt
    except Exception:
        log.exception("DART 사업보고서 수집 실패")

    # 2) IR자료
    try:
        ir_pair = dart_client.fetch_latest_ir_with_pdf(corp_code, target_dir)
        if ir_pair:
            ir_meta, ir_pdf = ir_pair
            if ir_pdf:
                out.ir_pdf = ir_pdf
                from src import summarizer
                out.ir_text = summarizer._extract_pdf_text(ir_pdf, max_chars=DART_TEXT_CAP)
    except Exception:
        log.exception("DART IR 수집 실패")

    # 3) 분기 재무
    try:
        fin = dart_client.fetch_quarterly_financials(corp_code, 3)
        if fin:
            # 잠정실적 보강
            try:
                preliminary = dart_client.fetch_latest_preliminary_quarter(
                    corp_code, target_dir / "preliminary",
                )
                if preliminary:
                    yq, rev, op, net = preliminary
                    if yq not in fin.revenue_qoq:
                        fin.revenue_qoq[yq] = rev
                        fin.op_profit_qoq[yq] = op
                        fin.net_profit_qoq[yq] = net
            except Exception:
                log.exception("잠정실적 보강 실패 (무시)")
            out.financial_summary = _format_financial(fin)
            # 차트 PNG
            try:
                forward = forward_consensus.from_wisereport(None)  # wisereport context는 별도 수집
                out.chart_png = chart.build(
                    corp_name,
                    fin.revenue_qoq,
                    fin.op_profit_qoq,
                    fin.net_profit_qoq,
                    None,
                    forward,
                )
            except Exception:
                log.exception("차트 생성 실패 (무시)")
    except Exception:
        log.exception("DART 재무 수집 실패")

    if not (out.business_text or out.ir_text or out.financial_summary):
        out.error = "DART 데이터 모두 미수집"
    return out


async def _collect_dart(ticker: str, corp_code: str, corp_name: str, target_dir: Path) -> DartData:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _collect_dart_blocking, ticker, corp_code, corp_name, target_dir)


# ------------------------------------------------------------------
# Stage 1B: wisereport 데이터 수집 (기존 wisereport_context 모듈 재사용)
# ------------------------------------------------------------------
def _collect_wisereport_blocking(ticker: str, target_dir: Path) -> WisereportData:
    out = WisereportData()
    try:
        from src.deepdive import wisereport_context
    except Exception as e:
        out.error = f"wisereport_context import 실패: {e}"
        return out

    try:
        wctx = wisereport_context.collect_for_ticker(
            ticker, target_dir, max_chars_per_report=WISEREPORT_TEXT_CAP,
        )
    except Exception:
        log.exception("wisereport 수집 실패")
        out.error = "wisereport 수집 예외"
        return out

    # 기업 리포트 번들 (text + path + title)
    for title, text, path in zip(
        wctx.company_titles, wctx.company_texts, wctx.company_paths,
    ):
        out.reports.append({"title": title, "text": text, "path": path})
    out.industry_titles = list(wctx.industry_titles)
    out.industry_paths = list(wctx.industry_paths)
    if not out.reports and not out.industry_titles:
        out.error = "wisereport 데이터 없음"
    return out


async def _collect_wisereport(ticker: str, target_dir: Path) -> WisereportData:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _collect_wisereport_blocking, ticker, target_dir)


# ------------------------------------------------------------------
# Stage 1C: 웹 딥리서치 (perplexity sonar-pro)
# ------------------------------------------------------------------
WEB_PROMPT = """다음 한국 종목의 최근 90일 내 핵심 이슈를 조사해 6-10개 bullet으로 정리하라.

종목: {name} ({ticker})

수집 항목:
1. 실적 발표·가이던스·잠정공시 (수치 인용)
2. 시장 컨센서스 — 증권사별 목표주가·매수의견 분포 (가능하면 표)
3. 산업·정책 영향 (예: 보조금, 규제, 수출 동향)
4. 경쟁사·고객사 동향 (공급계약, M&A 등)
5. 기술·제품 출시·로드맵
6. 주요 뉴스·이슈

규칙:
- 각 bullet 1-2줄, 출처 URL 또는 매체명 명시
- 추측·일반론 금지 — 검색 결과에 없는 내용 적지 말 것
- 한국어 출력, bullet 형식 (마크다운)
- 출력 1500-2500자"""


async def _collect_web(name: str, ticker: str) -> WebData:
    out = WebData()
    try:
        client = get_client()
    except Exception as e:
        out.error = f"OpenRouter client 초기화 실패: {e}"
        return out
    model = os.getenv(RESEARCH_MODEL_ENV) or DEFAULT_RESEARCH_MODEL
    prompt = WEB_PROMPT.format(name=name, ticker=ticker)
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(
            None,
            lambda: chat_with_retry(
                client,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2500,
                model=model,
                temperature=0.2,
                context=f"deep_research web ({ticker})",
            ),
        )
        if text:
            out.text = text.strip()
        else:
            out.error = "웹 리서치 응답 비어있음"
    except OpenRouterCreditExhausted:
        out.error = "OpenRouter 크레딧 부족"
    except Exception as e:
        out.error = f"웹 리서치 예외: {type(e).__name__}: {str(e)[:120]}"
        log.exception("deep_research web 실패")
    return out


# ------------------------------------------------------------------
# Stage 2: 합성
# ------------------------------------------------------------------
def _load_prompt() -> str:
    try:
        if PROMPT_FILE.exists():
            return PROMPT_FILE.read_text(encoding="utf-8")
    except Exception:
        log.exception("deep_research 프롬프트 파일 로드 실패")
    return DEFAULT_PROMPT


def _build_broker_reports_block(reports: list[dict]) -> str:
    if not reports:
        return "(증권사 리포트 없음)"
    parts = []
    for i, r in enumerate(reports, start=1):
        parts.append(f"[리포트 {i}] {r['title']}\n{r['text'][:8000]}")
    return "\n\n---\n\n".join(parts)


async def _synthesize(
    name: str, ticker: str, dart: DartData, wise: WisereportData, web: WebData,
) -> str:
    """3축 raw → 통합 마크다운 리포트. 실패 시 폴백 raw concat."""
    template = _load_prompt()
    prompt = template.format(
        name=name,
        ticker=ticker,
        dart_business=dart.business_text or "(사업보고서 데이터 없음)",
        dart_ir=dart.ir_text or "(IR자료 없음)",
        dart_financial=dart.financial_summary or "(분기 재무 없음)",
        broker_reports=_build_broker_reports_block(wise.reports),
        web_research=web.text or "(웹 리서치 없음)",
    )
    log.info("deep_research synthesis 호출 [%s]: prompt %d chars", ticker, len(prompt))
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
                context=f"deep_research synthesis ({ticker})",
            ),
        )
        if text and len(text) > 500:
            return text.strip()
    except OpenRouterCreditExhausted:
        log.warning("OpenRouter 크레딧 부족 — synthesis 폴백")
    except Exception:
        log.exception("synthesis 실패 — 폴백")

    # 폴백: 각 축 raw concat
    parts = [f"# 📋 {name} ({ticker}) — 딥리서치 (합성 실패 폴백)"]
    if dart.business_text:
        parts.append(f"\n## DART 사업의 내용\n\n{dart.business_text[:4000]}")
    if dart.ir_text:
        parts.append(f"\n## DART IR자료\n\n{dart.ir_text[:4000]}")
    if dart.financial_summary:
        parts.append(f"\n## 분기 재무\n\n```\n{dart.financial_summary}\n```")
    if wise.reports:
        for r in wise.reports[:3]:
            parts.append(f"\n## 증권사: {r['title']}\n\n{r['text'][:3000]}")
    if web.text:
        parts.append(f"\n## 웹 리서치\n\n{web.text}")
    return "\n".join(parts)


# ------------------------------------------------------------------
# Stage 3: 발송
# ------------------------------------------------------------------
async def _deliver(
    bot: Bot, chat_id: str, name: str, ticker: str,
    report_md: str, dart: DartData, wise: WisereportData,
    elapsed_min: int,
) -> None:
    # 1) 본문 (Markdown 청크 분할 — bot_helpers.send_text_chunked 자동 페이징)
    await send_text_chunked(bot, chat_id, report_md, parse_mode="Markdown")

    # 2) 차트 PNG
    if dart.chart_png:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=dart.chart_png,
                caption=f"📊 {name} 분기 재무 추이",
            )
        except Exception:
            log.exception("차트 발송 실패 (무시)")

    # 3) 참고 PDF 묶음
    if dart.business_pdf:
        await send_pdf(bot, chat_id, dart.business_pdf, caption="📎 DART 사업보고서")
    if dart.ir_pdf:
        await send_pdf(bot, chat_id, dart.ir_pdf, caption="📎 DART IR자료")
    for i, r in enumerate(wise.reports, start=1):
        p = r.get("path")
        if p:
            try:
                await send_pdf(
                    bot, chat_id, p,
                    caption=f"📎 증권사 리포트 {i}: {r.get('title', '')[:80]}",
                )
            except Exception:
                log.exception("증권사 PDF 발송 실패 (무시): %s", p)
    for i, p in enumerate(wise.industry_paths or [], start=1):
        try:
            await send_pdf(bot, chat_id, p, caption=f"📎 산업 리포트 {i}")
        except Exception:
            log.exception("산업 PDF 발송 실패 (무시): %s", p)

    # 4) 완료 ack
    await send_text_chunked(
        bot, chat_id, f"✅ 딥리서치 완료 ({elapsed_min}분)",
    )


# ------------------------------------------------------------------
# Main: run
# ------------------------------------------------------------------
async def run(bot: Bot, chat_id: str, query: str) -> None:
    """종목 통합 딥리서치 실행. query는 6자리 ticker 또는 종목명.

    PIPELINE_LOCK 안에서 wisereport·DART 작업 직렬화. 각 단계 graceful.
    """
    # Stage 0: ticker resolve
    try:
        from src.deepdive import dart_client
    except Exception:
        log.exception("dart_client import 실패")
        await send_text_chunked(bot, chat_id, "❌ DART 모듈 로드 실패 — DART_API_KEY 확인 필요")
        return

    loop = asyncio.get_running_loop()
    q = query.strip()
    if q.isdigit() and len(q) == 6:
        ticker = q
    else:
        try:
            t = await loop.run_in_executor(None, dart_client.lookup_ticker_by_name, q)
            if not t:
                await send_text_chunked(bot, chat_id, f"❓ '{q}' 종목 매칭 실패 (6자리 ticker 또는 정확한 종목명 필요)")
                return
            ticker = t
        except Exception:
            log.exception("ticker lookup 실패")
            await send_text_chunked(bot, chat_id, f"❌ ticker 매칭 실패: {q}")
            return

    pair = await loop.run_in_executor(None, dart_client.get_corp_code, ticker)
    if not pair:
        await send_text_chunked(bot, chat_id, f"❓ ticker {ticker}에 해당하는 DART 기업 없음")
        return
    corp_code, corp_name = pair

    started = datetime.now(KST)
    if PIPELINE_LOCK.locked():
        await send_text_chunked(
            bot, chat_id,
            f"⏳ 다른 작업 진행 중 — 끝나면 *{corp_name}* ({ticker}) 딥리서치 시작",
            parse_mode="Markdown",
        )

    await send_text_chunked(
        bot, chat_id,
        f"📋 *{corp_name}* ({ticker}) 통합 딥리서치 시작\n"
        f"_3축 데이터 수집 + AI 합성 — 약 8-15분 소요_",
        parse_mode="Markdown",
    )

    download_root = Path(os.environ.get("DOWNLOAD_DIR", "./downloads")) / "deep_research" / ticker

    async with PIPELINE_LOCK:
        # Stage 1: 3축 병렬 수집
        dart_task = asyncio.create_task(_collect_dart(ticker, corp_code, corp_name, download_root / "dart"))
        wise_task = asyncio.create_task(_collect_wisereport(ticker, download_root / "wisereport"))
        web_task = asyncio.create_task(_collect_web(corp_name, ticker))
        try:
            dart_data, wise_data, web_data = await asyncio.gather(
                dart_task, wise_task, web_task, return_exceptions=False,
            )
        except Exception:
            log.exception("Stage 1 병렬 수집 최상위 예외")
            await send_text_chunked(bot, chat_id, "❌ 데이터 수집 단계 실패 — 로그 확인")
            return

        log.info(
            "deep_research [%s] Stage 1 완료: dart_err=%r wise_err=%r web_err=%r reports=%d",
            ticker, dart_data.error or "OK", wise_data.error or "OK", web_data.error or "OK",
            len(wise_data.reports),
        )

        # 모두 실패 시 종료
        if dart_data.error and wise_data.error and web_data.error:
            await send_text_chunked(
                bot, chat_id,
                f"❌ 모든 데이터 축 실패\nDART: {dart_data.error}\nwisereport: {wise_data.error}\nweb: {web_data.error}",
            )
            return

        # Stage 2: 합성
        try:
            report_md = await _synthesize(corp_name, ticker, dart_data, wise_data, web_data)
        except Exception:
            log.exception("Stage 2 synthesis 최상위 예외")
            report_md = f"# {corp_name} ({ticker}) — 합성 실패\n\n원본 데이터 발송 폴백."

        elapsed = int((datetime.now(KST) - started).total_seconds() / 60)

        # Stage 3: 발송
        try:
            await _deliver(bot, chat_id, corp_name, ticker, report_md, dart_data, wise_data, elapsed)
        except Exception:
            log.exception("Stage 3 발송 최상위 예외")
            await send_text_chunked(bot, chat_id, "⚠️ 일부 발송 실패 — 로그 확인")
