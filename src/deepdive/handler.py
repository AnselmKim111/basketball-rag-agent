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
    "사용법: `/deepdive <티커6자리 또는 종목명>`\n"
    "예: `/deepdive 005930`\n"
    "예: `/deepdive 삼성전자`\n"
    "예: `/deepdive HD현대중공업`\n\n"
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
    if not args:
        await update.message.reply_text(
            "사용법: `/deepdive 005930` 또는 `/deepdive 삼성전자`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    # 다단어 종목명 지원 (예: "/deepdive HD 현대중공업")
    query = " ".join(args).strip()
    asyncio.create_task(_run(update, context, query))


async def _run(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    """파이프라인 실행. query는 ticker(6자리) 또는 종목명. 모든 단계 try/except."""
    chat_id = update.effective_chat.id
    bot = context.bot

    if PIPELINE_LOCK.locked():
        running = bot_worker.CURRENT_TASK['name'] if bot_worker.CURRENT_TASK else "다른 작업"
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏳ 진행중: *{running}*\n끝나면 처리: deepdive {query}",
            parse_mode=ParseMode.MARKDOWN,
        )

    async with PIPELINE_LOCK:
        bot_worker.CURRENT_TASK = {"name": f"deepdive {query}", "ticker": query, "top": 0}
        try:
            # ticker(6자리 숫자) vs 종목명 자동 감지
            ticker = await _resolve_ticker(bot, chat_id, query)
            if not ticker:
                return  # _resolve_ticker가 안내 메시지 발송
            await _execute(bot, chat_id, ticker)
        except Exception:
            log.exception("deepdive 파이프라인 최상위 예외 — 무시하고 봇 유지")
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ 예상치 못한 오류 발생 (query={query}). 로그 확인 필요.",
                )
            except Exception:
                pass
        finally:
            bot_worker.CURRENT_TASK = None


async def _resolve_ticker(bot, chat_id: int, query: str) -> Optional[str]:
    """ticker(6자리 숫자) → 그대로, 아니면 DART 종목명 lookup → ticker."""
    q = query.strip()
    if re.match(r"^\d{6}$", q):
        return q

    # 종목명으로 lookup
    try:
        from src.deepdive import dart_client
    except Exception:
        log.exception("dart_client import 실패")
        await bot.send_message(chat_id=chat_id, text="❌ DART 모듈 로드 실패")
        return None

    loop = asyncio.get_running_loop()
    try:
        ticker = await loop.run_in_executor(None, dart_client.lookup_ticker_by_name, q)
    except Exception as e:
        log.exception("종목명 lookup 실패")
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ 종목명 검색 실패: {type(e).__name__}: {str(e)[:200]}",
        )
        return None

    if not ticker:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❓ '{q}'에 해당하는 상장사를 찾지 못했습니다.\n"
                "다른 표현으로 시도하거나 6자리 티커로 입력해주세요.\n"
                "예: `/deepdive 005930`"
            ),
        )
        return None

    # 매칭된 회사명 안내
    name = dart_client._CORP_NAME_CACHE.get(ticker, "")
    await bot.send_message(
        chat_id=chat_id,
        text=f"🔎 '{q}' → *{name}* ({ticker})",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ticker


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

    # 2.5) wisereport 보조 컨텍스트 (실패해도 무시).
    #      wctx는 이후 step 5의 forward consensus 산출에도 재사용되므로 outer 스코프에 보관.
    wctx = None  # WisereportContext | None
    wisereport_ctx_text: Optional[str] = None
    try:
        from src.deepdive import wisereport_context
        wctx = await loop.run_in_executor(
            None, wisereport_context.collect_for_ticker,
            ticker, download_root / "wisereport",
        )
        wisereport_ctx_text = _format_wisereport_context(wctx)
        if wisereport_ctx_text:
            log.info(
                "wisereport 컨텍스트 주입 준비: 기업 %d / 산업 %d",
                len(wctx.company_texts), len(wctx.industry_texts),
            )
    except Exception:
        log.exception("wisereport 컨텍스트 수집 단계 실패 — 빈 컨텍스트로 진행")

    # 3) 업의 본질 요약 (wisereport 컨텍스트 주입)
    if report_pdf:
        try:
            await _summarize_and_send(
                bot, chat_id,
                pdf_path=report_pdf,
                prompt_name="deepdive_business",
                header="🏢 *업의 본질*",
                prefer_section="사업의 내용",
                extra_context=wisereport_ctx_text,
            )
        except Exception as e:
            log.exception("업의 본질 요약 실패")
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ 업의 본질 요약 실패: {type(e).__name__}: {str(e)[:300]}",
            )

    # 4) IR자료: 최근 5년 IR 후보 중 PDF 첨부 있는 첫 번째 자동 선택.
    #    PDF 못 찾으면 wisereport 컨텍스트만으로 투자포인트 작성 (graceful fallback).
    ir_pair = await loop.run_in_executor(
        None, dart_client.fetch_latest_ir_with_pdf, corp_code, download_root,
    )
    if ir_pair:
        ir_meta, ir_pdf = ir_pair
        try:
            await _summarize_and_send(
                bot, chat_id,
                pdf_path=ir_pdf,
                prompt_name="deepdive_ir",
                header=f"💡 *핵심 투자 포인트* ({ir_meta.report_nm})",
                prefer_section=None,  # IR자료는 전체 본문 사용
                extra_context=wisereport_ctx_text,  # 보조 컨텍스트로도 같이
            )
        except Exception as e:
            log.exception("IR 요약 실패")
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ IR 요약 실패: {type(e).__name__}: {str(e)[:300]}",
            )
    elif wisereport_ctx_text:
        await bot.send_message(
            chat_id=chat_id,
            text="ℹ️ DART IR PDF 미발견 — wisereport 분석가 리포트로 투자 포인트 작성",
        )
        try:
            await _summarize_and_send(
                bot, chat_id,
                pdf_path=None,
                prompt_name="deepdive_ir_fallback",
                header="💡 *핵심 투자 포인트* (외부 분석 기반)",
                prefer_section=None,
                extra_context=wisereport_ctx_text,
            )
        except Exception as e:
            log.exception("IR 폴백 요약 실패")
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ IR 폴백 요약 실패: {type(e).__name__}: {str(e)[:300]}",
            )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text="ℹ️ DART IR PDF 미발견 + wisereport 컨텍스트도 없음 — 투자포인트 스킵",
        )

    # 5) 분기별 재무 + 차트
    try:
        fin = await loop.run_in_executor(None, dart_client.fetch_quarterly_financials, corp_code, 3)
    except Exception:
        log.exception("재무 조회 실패")
        fin = None

    # 5-bis) 정식 분기보고서가 아직 DART에 없는 최신 분기를 잠정실적공시로 보강.
    if fin is not None:
        try:
            preliminary = await loop.run_in_executor(
                None, dart_client.fetch_latest_preliminary_quarter,
                corp_code, download_root / "preliminary",
            )
            if preliminary:
                yq, rev, op, net = preliminary
                if yq not in fin.revenue_qoq:
                    fin.revenue_qoq[yq] = rev
                    fin.op_profit_qoq[yq] = op
                    fin.net_profit_qoq[yq] = net
                    log.info("잠정실적 보강: %s = 매출 %d원, 영업익 %d원, 순익 %d원", yq, rev, op, net)
                else:
                    log.info("잠정실적 분기(%s)는 이미 정식 보고서에 있어 스킵", yq)
        except Exception:
            log.exception("잠정실적 보강 단계 실패 — 무시")

    if fin and fin.revenue_qoq:
        try:
            from src.deepdive import forward_consensus, chart
            # wisereport 기업 리포트(이미 다운로드된) 텍스트들로부터 LLM이 forward 산출
            forward = await loop.run_in_executor(None, forward_consensus.from_wisereport, wctx)
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
                fwd_info = (
                    f" + Forward {len(forward)}분기 (wisereport 컨센서스)"
                    if forward else " (Forward 미수집)"
                )
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=png_bytes,
                    caption=f"📈 {corp_name} 분기별 재무 (회사 전체){fwd_info}",
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


def _format_wisereport_context(wctx) -> Optional[str]:
    """WisereportContext → LLM 주입용 단일 문자열. 비어있으면 None."""
    if wctx is None:
        return None
    parts: list[str] = []
    for title, text in zip(wctx.industry_titles, wctx.industry_texts):
        parts.append(f"[산업리포트] {title}\n{text}")
    for title, text in zip(wctx.company_titles, wctx.company_texts):
        parts.append(f"[기업리포트] {title}\n{text}")
    return "\n\n---\n\n".join(parts) if parts else None


async def _summarize_and_send(
    bot, chat_id: int, pdf_path: Optional[Path], prompt_name: str, header: str,
    prefer_section: Optional[str] = None,
    extra_context: Optional[str] = None,
) -> None:
    """공통 헬퍼: DART 보고서 (PDF 또는 XML/HTML) → 텍스트 추출 → LLM 요약 → 발송.

    pdf_path=None 이면 PDF 텍스트 단계는 스킵하고 extra_context만으로 요약.
    extra_context: 시스템 프롬프트에 추가로 주입할 보조 자료 (예: wisereport 리포트 텍스트).
    내부에서 모든 예외를 catch해 문자열로 반환 — _summarize_and_send 자체는
    최대한 raise하지 않게 (상위 핸들러의 try/except 의존도 줄임).
    """
    from src.deepdive import prompts as prompt_loader
    from src.deepdive import dart_client
    from src import summarizer

    loop = asyncio.get_running_loop()

    def _do_summary() -> str:
        # 1) 텍스트 추출 (pdf_path가 있을 때만)
        text = ""
        if pdf_path is not None:
            try:
                text = dart_client.extract_doc_text(
                    pdf_path, max_chars=80_000,
                    prefer_section=prefer_section or "",
                )
            except Exception as e:
                log.exception("extract_doc_text 호출 자체 예외")
                return f"(텍스트 추출 예외: {type(e).__name__}: {str(e)[:200]})"
            if not text.strip() and not extra_context:
                return f"(보고서 텍스트 추출 결과 빈 문자열 - file={pdf_path.name})"

        if not text.strip() and not extra_context:
            return "(요약할 텍스트와 컨텍스트가 모두 비어있음)"

        # 2) 프롬프트 로드 (fallback 포함, 거의 raise 안 함)
        try:
            sys_prompt = prompt_loader.load(prompt_name)
        except Exception as e:
            log.exception("prompt 로드 실패")
            return f"(프롬프트 로드 실패: {type(e).__name__}: {str(e)[:200]})"

        # 3) OpenRouter 클라이언트
        try:
            client = summarizer.get_client()
        except Exception as e:
            log.exception("OpenRouter client 초기화 실패")
            return f"(LLM 초기화 실패: {type(e).__name__}: {str(e)[:200]})"

        # 4) LLM 호출 - 모든 예외 catch
        if pdf_path is not None and text.strip():
            user_msg = (
                f"파일: {pdf_path.name}\n"
                "1000자 이내로 시스템 프롬프트 형식대로 요약해주세요.\n\n"
                f"<doc_text>\n{text}\n</doc_text>"
            )
        else:
            user_msg = (
                "회사가 직접 발표한 IR PDF가 없어 외부 분석가/산업 리포트로 작성합니다.\n"
                "1000자 이내로 시스템 프롬프트 형식대로 작성해주세요."
            )
        if extra_context:
            user_msg += f"\n\n<wisereport_context>\n{extra_context}\n</wisereport_context>"
        try:
            resp = client.chat.completions.create(
                model=summarizer.DEFAULT_MODEL,
                max_tokens=1500,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
        except summarizer.APIStatusError as e:
            if summarizer._is_credit_error(e):
                return "(요약 실패: OpenRouter 토큰 부족)"
            return f"(요약 실패 APIStatus: {e!r})"
        except Exception as e:
            log.exception("LLM 호출 실패 (non-APIStatusError)")
            return f"(요약 실패: {type(e).__name__}: {str(e)[:200]})"

        try:
            return summarizer._trim_to_chars(
                resp.choices[0].message.content or "", summarizer.MAX_SUMMARY_CHARS_SHORT,
            )
        except Exception as e:
            log.exception("응답 trim 실패")
            return f"(응답 처리 실패: {type(e).__name__})"

    try:
        summary = await loop.run_in_executor(None, _do_summary)
    except Exception as e:
        log.exception("run_in_executor 자체 실패")
        summary = f"(executor 실패: {type(e).__name__}: {str(e)[:200]})"

    full = f"{header}\n\n{summary}"
    # 4096 char limit handling
    while full:
        chunk, full = full[:4000], full[4000:]
        try:
            await bot.send_message(chat_id=chat_id, text=chunk)
        except Exception:
            log.exception("텔레그램 발송 실패 (chunk)")
            break
