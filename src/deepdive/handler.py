"""/deepdive 텔레그램 핸들러.

진입점: register(app) — bot_worker.build_company_app()가 try/except로 호출.
격리:
  - register() 첫 줄에서 DART_API_KEY 미설정/DEEPDIVE_ENABLED=0이면 즉시 return
  - 무거운 import (matplotlib, dart_fss)는 함수 본문 내부에서만
  - 모든 외부 호출에 try/except — 예외가 봇 프로세스로 절대 propagate 안 함
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from src import bot_worker  # is_authorized, CURRENT_TASK 재사용
from src.pipeline_lock import PIPELINE_LOCK

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


HELP_TEXT = (
    "🔍 *심층분석 (deepdive)*\n\n"
    "사용법: `/deepdive <티커6자리>`\n"
    "예: `/deepdive 005930` (삼성전자)\n\n"
    "*분석 항목:*\n"
    "  1️⃣ DART 사업보고서 → 업의 본질 (1000자)\n"
    "  2️⃣ DART IR자료 → 핵심 투자 포인트 (1000자)\n"
    "  3️⃣ 분기별 매출·영업이익·순이익 차트 (3년 + Forward)\n"
    "  4️⃣ 사업보고서 PDF 원본\n\n"
    "_⏱️ 약 5-10분 소요_\n"
    "_프롬프트 수정: GitHub `prompts/deepdive_*.txt` 편집 후 push_"
)


def register(app: Application) -> None:
    """orchestrator → build_company_app → 여기서 호출.

    환경변수 가드:
      - DART_API_KEY 없으면 등록 안 함
      - DEEPDIVE_ENABLED=0 이면 등록 안 함 (kill switch)
    """
    if os.getenv("DEEPDIVE_ENABLED", "1").strip() == "0":
        log.warning("DEEPDIVE_ENABLED=0 → /deepdive 핸들러 등록 안 함")
        return
    if not os.getenv("DART_API_KEY"):
        log.warning("DART_API_KEY 미설정 → /deepdive 핸들러 등록 안 함")
        return
    app.add_handler(CommandHandler("deepdive", _cmd_deepdive))
    app.add_handler(CommandHandler("deephelp", _cmd_help))
    log.info("/deepdive 핸들러 등록 OK")


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not bot_worker.is_authorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def _cmd_deepdive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not bot_worker.is_authorized(update):
        return
    args = context.args or []
    ticker = args[0].strip() if args else ""
    if not re.match(r"^\d{6}$", ticker):
        await update.message.reply_text(
            "사용법: `/deepdive 005930` (6자리 티커)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    asyncio.create_task(_run(update, context, ticker))


async def _run(update: Update, context: ContextTypes.DEFAULT_TYPE, ticker: str) -> None:
    """파이프라인 실행. 모든 단계 try/except, 봇 프로세스로 예외 전파 안 함."""
    chat_id = update.effective_chat.id
    bot = context.bot

    if PIPELINE_LOCK.locked():
        running = bot_worker.CURRENT_TASK['name'] if bot_worker.CURRENT_TASK else "다른 작업"
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏳ 진행중: *{running}*\n끝나면 처리: deepdive {ticker}",
            parse_mode=ParseMode.MARKDOWN,
        )

    async with PIPELINE_LOCK:
        bot_worker.CURRENT_TASK = {"name": f"deepdive {ticker}", "ticker": ticker, "top": 0}
        try:
            await _execute(bot, chat_id, ticker)
        except Exception:
            log.exception("deepdive 파이프라인 최상위 예외 — 무시하고 봇 유지")
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ 예상치 못한 오류 발생 (ticker={ticker}). 로그 확인 필요.",
                )
            except Exception:
                pass
        finally:
            bot_worker.CURRENT_TASK = None


async def _execute(bot, chat_id: int, ticker: str) -> None:
    loop = asyncio.get_running_loop()

    # 1) corp_code lookup
    try:
        from src.deepdive import dart_client
    except Exception:
        log.exception("dart_client import 실패")
        await bot.send_message(chat_id=chat_id, text="❌ DART 모듈 로드 실패")
        return

    pair = await loop.run_in_executor(None, dart_client.get_corp_code, ticker)
    if not pair:
        await bot.send_message(chat_id=chat_id, text=f"❓ 티커 {ticker}에 해당하는 DART 기업을 찾지 못했습니다.")
        return
    corp_code, corp_name = pair

    today = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    await bot.send_message(
        chat_id=chat_id,
        text=f"📊 *{corp_name}* ({ticker}) 심층분석 시작\n_{today}_\n⏱️ 5-10분 소요",
        parse_mode=ParseMode.MARKDOWN,
    )

    download_root = Path(os.environ.get("DOWNLOAD_DIR", "./downloads")) / "deepdive" / ticker
    download_root.mkdir(parents=True, exist_ok=True)

    # 2) 사업보고서 메타 + PDF
    report = await loop.run_in_executor(None, dart_client.fetch_latest_business_report, corp_code)
    report_pdf: Optional[Path] = None
    if report:
        report_pdf = await loop.run_in_executor(
            None, dart_client.download_report_archive, report.rcept_no, download_root,
        )
    if not report_pdf:
        await bot.send_message(chat_id=chat_id, text="ℹ️ 사업보고서 PDF 다운로드 실패 — 업의 본질 단계 스킵")

    # 3) 업의 본질 요약
    if report_pdf:
        try:
            await _summarize_and_send(
                bot, chat_id,
                pdf_path=report_pdf,
                prompt_name="deepdive_business",
                header="🏢 *업의 본질*",
            )
        except Exception:
            log.exception("업의 본질 요약 실패")
            await bot.send_message(chat_id=chat_id, text="⚠️ 업의 본질 요약 실패 (계속 진행)")

    # 4) IR자료 메타 + PDF + 핵심 투자포인트 요약
    ir_meta = await loop.run_in_executor(None, dart_client.fetch_latest_ir_doc, corp_code)
    if not ir_meta:
        await bot.send_message(chat_id=chat_id, text="ℹ️ 최근 IR 공시 없음 — 투자포인트 단계 스킵")
    else:
        ir_pdf = await loop.run_in_executor(
            None, dart_client.download_report_archive, ir_meta.rcept_no, download_root,
        )
        if ir_pdf:
            try:
                await _summarize_and_send(
                    bot, chat_id,
                    pdf_path=ir_pdf,
                    prompt_name="deepdive_ir",
                    header=f"💡 *핵심 투자 포인트* ({ir_meta.report_nm})",
                )
            except Exception:
                log.exception("IR 요약 실패")
                await bot.send_message(chat_id=chat_id, text="⚠️ IR 요약 실패 (계속 진행)")

    # 5) 분기별 재무 + 차트
    try:
        fin = await loop.run_in_executor(None, dart_client.fetch_quarterly_financials, corp_code, 3)
    except Exception:
        log.exception("재무 조회 실패")
        fin = None

    if fin and fin.revenue_qoq:
        try:
            from src.deepdive import forward_consensus, chart
            forward = await loop.run_in_executor(None, forward_consensus.fetch, ticker)
            png_bytes = await loop.run_in_executor(
                None, chart.build,
                corp_name,
                fin.revenue_qoq,
                fin.op_profit_qoq,
                fin.net_profit_qoq,
                None,  # segment_revenue (MVP: 회사 전체로 폴백)
                forward or None,
            )
            if png_bytes:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=png_bytes,
                    caption=f"📈 {corp_name} 분기별 재무 (회사 전체)",
                )
            else:
                await bot.send_message(chat_id=chat_id, text="⚠️ 차트 생성 실패 (계속 진행)")
        except Exception:
            log.exception("차트 단계 실패")
            await bot.send_message(chat_id=chat_id, text="⚠️ 차트 단계 실패 (계속 진행)")
    else:
        await bot.send_message(chat_id=chat_id, text="ℹ️ 분기별 재무 데이터 없음 — 차트 스킵")

    # 6) 사업보고서 PDF 원본 발송
    if report_pdf and report_pdf.exists():
        try:
            size_mb = report_pdf.stat().st_size / 1024 / 1024
            if size_mb > 49:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"ℹ️ 사업보고서 PDF가 너무 큼 ({size_mb:.1f}MB) — 원본 발송 스킵",
                )
            else:
                with report_pdf.open("rb") as f:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=report_pdf.name,
                        caption=f"📄 {corp_name} {report.report_nm}",
                    )
        except Exception:
            log.exception("PDF 발송 실패")

    await bot.send_message(chat_id=chat_id, text=f"✅ *{corp_name}* 심층분석 완료", parse_mode=ParseMode.MARKDOWN)


async def _summarize_and_send(
    bot, chat_id: int, pdf_path: Path, prompt_name: str, header: str
) -> None:
    """공통 헬퍼: PDF → 텍스트 추출 → LLM 요약 → 텔레그램 발송."""
    from src.deepdive import prompts as prompt_loader
    from src import summarizer

    loop = asyncio.get_running_loop()

    def _do_summary() -> str:
        text = summarizer._extract_pdf_text(pdf_path, max_chars=80_000)
        if not text.strip():
            return "(PDF 텍스트 추출 실패)"
        sys_prompt = prompt_loader.load(prompt_name)
        client = summarizer.get_client()
        try:
            resp = client.chat.completions.create(
                model=summarizer.DEFAULT_MODEL,
                max_tokens=1500,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": (
                        f"PDF: {pdf_path.name}\n"
                        "1000자 이내로 시스템 프롬프트 형식대로 요약해주세요.\n\n"
                        f"<pdf_text>\n{text}\n</pdf_text>"
                    )},
                ],
            )
        except summarizer.APIStatusError as e:
            if summarizer._is_credit_error(e):
                return "(요약 실패: OpenRouter 토큰 부족)"
            return f"(요약 실패: {e!r})"
        return summarizer._trim_to_chars(
            resp.choices[0].message.content or "", summarizer.MAX_SUMMARY_CHARS_SHORT,
        )

    summary = await loop.run_in_executor(None, _do_summary)

    full = f"{header}\n\n{summary}"
    # 4096 char limit handling
    while full:
        chunk, full = full[:4000], full[4000:]
        await bot.send_message(chat_id=chat_id, text=chunk)
