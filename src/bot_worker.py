"""CompanyBot — wisereport 종목 리포트 + DART 심층분석.

배포처(Railway 등) 에서 24/7 실행. 텔레그램으로 명령 받음.

지원 입력:
  /report 종목명                 — 자동 ticker 매핑 (DART 회사목록)
  /report 종목명 6자리티커 [개수] — 명시적 티커 지정
  /deepdive 종목명 또는 티커     — 심층분석 (deepdive.handler에서 처리)
  슬래시 없는 텍스트(종목명/티커) — /deepdive 후 /report 자동 순차 실행

환경변수 (전부 필수):
  TELEGRAM_BOT_TOKEN   — @BotFather 봇 토큰
  ALLOWED_CHAT_IDS     — 콤마 구분 chat_id 화이트리스트
  WISEREPORT_ID, WISEREPORT_PW
  OPENROUTER_API_KEY, OPENROUTER_MODEL
  DART_API_KEY         — /deepdive용 (없으면 deepdive 핸들러 등록 안 됨)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.bot_helpers import (
    allowed_chat_ids,
    deny_message,
    is_authorized as _bh_is_authorized,
)
from src.pipeline_lock import PIPELINE_LOCK


def _log_task_exception(task: asyncio.Task) -> None:
    """create_task done callback — 미캐치 예외를 silent drop하지 않게 로깅."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logging.exception("백그라운드 태스크 실패: %s", task.get_name(), exc_info=exc)

# CompanyBot용 chat_id allowlist 환경변수
_ALLOWED_ENV = "ALLOWED_CHAT_IDS"

REPO_ROOT = Path(__file__).resolve().parent.parent
HELP_TEXT = (
    "📊 *wisereport 자동 분석 봇*\n\n"
    "*🚀 추천 — 통합 딥리서치 (한 편의 아름다운 리포트):*\n"
    "  종목명/티커만 입력 → 자동으로 통합 분석\n"
    "  예: `삼성전자` / `005930` / `피에스케이`\n"
    "  또는 `/research 삼성전자`\n"
    "  → DART(공시·IR·재무) + 증권사 리포트 + 웹 딥리서치 90일\n"
    "  → claude-sonnet 합성 → 8개 섹션 통합 리포트 + 차트 + 참고 PDF\n"
    "  ⏱️ 약 8-15분\n\n"
    "*🎯 AI 큐레이션 (오늘의 주도주):*\n"
    "  `/curate` — Top 10 종목 리포트 + 각 선별 이유 (5-15분)\n\n"
    "*분리 발송 (부분만 받고 싶을 때):*\n"
    "  `/report 종목명 [6자리티커] [개수]` — 증권사 리포트만\n"
    "  `/deepdive <티커 또는 종목명>` — DART만 (사업보고서·IR·재무차트)\n\n"
    "*명령어 (/ 입력 시 자동완성 메뉴):*\n"
    "  /research, /curate, /report, /deepdive, /status, /help"
)


def get_allowed_ids() -> set[str]:
    """deepdive.handler가 import해 사용 중 — 호환을 위해 유지."""
    return allowed_chat_ids(_ALLOWED_ENV)


def is_authorized(update: Update) -> bool:
    """deepdive.handler가 import해 사용 중 — 호환을 위해 유지."""
    return _bh_is_authorized(update, _ALLOWED_ENV)


# ------------------------------------------------------------------
# 작업 큐 - 한 번에 하나만 실행 (wisereport 동시 로그인 충돌 방지)
# orchestrator의 모든 봇이 PIPELINE_LOCK을 공유하므로 카테고리 봇과도 직렬화됨.
# ------------------------------------------------------------------
CURRENT_TASK: dict | None = None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text(
            "이 봇은 인가된 사용자만 사용 가능합니다.\n"
            f"chat_id `{update.effective_chat.id}` 를 운영자에게 알려주세요.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_message(update, "종목봇")
        return
    if CURRENT_TASK is None:
        await update.message.reply_text("✅ 대기 중. 명령 보낼 수 있습니다.")
    else:
        await update.message.reply_text(
            f"⏳ 진행중: {CURRENT_TASK['name']} ({CURRENT_TASK['ticker']}, top {CURRENT_TASK['top']})"
        )


def _parse_args(parts: list[str]) -> tuple[str, str, int] | None:
    """parts = ['삼성전자', '005930'] 또는 ['삼성전자', '005930', '5']."""
    if len(parts) < 2:
        return None
    if not re.match(r"^\d{6}$", parts[1]):
        return None
    name = parts[0]
    ticker = parts[1]
    top = 10
    if len(parts) >= 3:
        try:
            top = int(parts[2])
            if not (1 <= top <= 30):
                return None
        except ValueError:
            return None
    return name, ticker, top


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_message(update, "종목봇")
        return
    args = list(context.args or [])
    parsed = _parse_args(args) or await _parse_args_with_lookup(update, args)
    if not parsed:
        await update.message.reply_text(
            "사용법:\n"
            "  `/report 종목명` (자동 ticker 매핑)\n"
            "  `/report 종목명 6자리티커 [개수]`\n"
            "예: `/report 피에스케이` / `/report 삼성전자 005930 5`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await _enqueue(update, *parsed)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """슬래시 없는 자유 입력: 종목명/티커만 보내면 /deepdive + /report 둘 다 실행.

    명시적으로 /report 또는 /deepdive 명령을 쓰면 각각만 실행 (이 핸들러는
    filters.TEXT & ~filters.COMMAND 로 등록되어 슬래시 명령은 안 들어옴).
    """
    if not is_authorized(update):
        await deny_message(update, "종목봇")
        return
    text = (update.message.text or "").strip()
    parts = text.split()
    parsed = _parse_args(parts) or await _parse_args_with_lookup(update, parts)
    if not parsed:
        await update.message.reply_text(
            "잘 모르겠어요. `/help` 또는 `종목명` (자동 매핑) / `종목명 6자리티커` 형식으로 보내주세요.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await _enqueue_combined(update, context, *parsed)


async def _enqueue_combined(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    name: str,
    ticker: str,
    top: int,
) -> None:
    """텍스트 입력 한 번 → /deepdive 후 /report 순차 실행.

    각 단계 사이에 PIPELINE_LOCK이 release되므로 다른 봇/명령이 끼어들 수 있음
    (의도된 동작 — 큐 fairness 유지). 같은 사용자의 다음 텍스트도 같은 방식으로
    뒤에 줄 섬.
    """
    if PIPELINE_LOCK.locked():
        running = CURRENT_TASK['name'] if CURRENT_TASK else "다른 작업"
        await update.message.reply_text(
            f"⏳ 진행중: *{running}*\n"
            f"끝나면 처리: *{name}* ({ticker}) — /deepdive + /report",
            parse_mode=ParseMode.MARKDOWN,
        )
    task = asyncio.create_task(
        _run_combined(update, context, name, ticker, top),
        name=f"combined:{ticker}",
    )
    task.add_done_callback(_log_task_exception)


async def _run_combined(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    name: str,
    ticker: str,
    top: int,
) -> None:
    """종목명 자유 텍스트 진입점 — 통합 딥리서치 실행.

    이전엔 deepdive + report 순차 (25분, 메시지 ~25개). 이제 deep_research 모듈이
    DART + wisereport + 웹 3축 병렬 수집 → 한 편의 통합 마크다운 리포트로 합성 →
    리포트 + 차트 + 모든 PDF 일괄 발송 (~10-15분, 메시지 ~12-13개).

    /deepdive와 /report 명시 호출은 그대로 동작 — 부분만 받고 싶을 때 사용.
    """
    chat_id = str(update.effective_chat.id)
    bot = context.bot
    try:
        from src.deep_research import run as deep_research_run
    except Exception:
        logging.exception("deep_research 모듈 로드 실패 — 폴백: /report만 실행")
        try:
            await update.message.reply_text(
                f"⚠️ 딥리서치 모듈 로드 실패 — /report만 실행으로 폴백"
            )
            await _run_pipeline(update, name, ticker, top)
        except Exception:
            logging.exception("폴백 _run_pipeline 실패")
        return

    try:
        await deep_research_run(bot, chat_id, ticker)
    except Exception:
        logging.exception("deep_research 실행 최상위 예외")
        try:
            await update.message.reply_text(
                f"❌ {name} ({ticker}) 딥리서치 실행 실패 — 로그 확인"
            )
        except Exception:
            pass


async def _parse_args_with_lookup(
    update: Update, parts: list[str]
) -> tuple[str, str, int] | None:
    """ticker 없이 종목명만 들어왔을 때 DART 회사목록에서 자동 매핑.

    parts 끝에 1~30 정수가 있으면 top으로 분리, 나머지는 모두 종목명으로 합침.
    예) ['피에스케이']             → 피에스케이 lookup, top=10
        ['HD', '현대중공업']        → 'HD 현대중공업' → 정규화 매칭
        ['삼성전자', '5']           → 삼성전자 lookup, top=5
    """
    if not parts:
        return None

    name_parts = list(parts)
    top = 10
    # 끝이 1~30 정수면 top
    if len(name_parts) >= 2 and re.match(r"^\d{1,2}$", name_parts[-1]):
        try:
            t = int(name_parts[-1])
            if 1 <= t <= 30:
                top = t
                name_parts = name_parts[:-1]
        except ValueError:
            pass

    if not name_parts:
        return None
    name_query = " ".join(name_parts).strip()
    if not name_query or re.match(r"^\d{6}$", name_query):
        # 6자리 숫자만 있으면 _parse_args가 이미 처리; 여기는 종목명만.
        return None

    try:
        from src.deepdive import dart_client
    except Exception:
        logging.exception("dart_client import 실패 — 종목명 lookup 비활성")
        return None

    loop = asyncio.get_running_loop()
    try:
        ticker = await loop.run_in_executor(None, dart_client.lookup_ticker_by_name, name_query)
    except Exception:
        logging.exception("종목명 lookup 실패: %s", name_query)
        return None
    if not ticker:
        return None

    matched_name = dart_client._CORP_NAME_CACHE.get(ticker, name_query)
    try:
        await update.message.reply_text(
            f"🔎 '{name_query}' → *{matched_name}* ({ticker})",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass
    return matched_name, ticker, top


async def _enqueue(
    update: Update, name: str, ticker: str, top: int
) -> None:
    """작업 큐에 추가. 다른 작업이 진행중이면 대기 안내."""
    if PIPELINE_LOCK.locked():
        running = CURRENT_TASK['name'] if CURRENT_TASK else "다른 작업"
        await update.message.reply_text(
            f"⏳ 진행중: *{running}*\n끝나면 처리: *{name}* ({ticker}, top {top})",
            parse_mode=ParseMode.MARKDOWN,
        )

    task = asyncio.create_task(
        _run_pipeline(update, name, ticker, top),
        name=f"pipeline:{ticker}",
    )
    task.add_done_callback(_log_task_exception)


async def _run_pipeline(
    update: Update, name: str, ticker: str, top: int
) -> None:
    """Lock 잡고 파이프라인 subprocess 실행. PIPELINE_LOCK은 카테고리 봇과 공유."""
    global CURRENT_TASK
    async with PIPELINE_LOCK:
        CURRENT_TASK = {"name": name, "ticker": ticker, "top": top}
        try:
            ack = await update.message.reply_text(
                f"📥 *{name}* ({ticker}) 작업 시작\n"
                f"PDF {top}건 다운로드 → 요약 → 발송\n"
                f"⏱️ 약 8-15분 소요",
                parse_mode=ParseMode.MARKDOWN,
            )

            cmd = [
                sys.executable,
                "-m",
                "src.main",
                name,
                "--ticker",
                ticker,
                "--top",
                str(top),
            ]
            logging.info("실행: %s", " ".join(cmd))

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(REPO_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
            stdout_bytes, _ = await proc.communicate()
            output = stdout_bytes.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                await ack.reply_text(
                    f"✅ *{name}* ({ticker}) 완료",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                tail = output.splitlines()[-25:]
                err_block = "\n".join(tail)[-3500:]
                await ack.reply_text(
                    f"❌ *{name}* ({ticker}) 실패 (exit {proc.returncode})\n\n"
                    f"```\n{err_block}\n```",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception:
            logging.exception("파이프라인 실행 중 예외")
            try:
                await update.message.reply_text(
                    f"❌ {name} 처리 중 예외 발생. 봇 로그 확인 필요."
                )
            except Exception:
                pass
        finally:
            CURRENT_TASK = None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled error: %s", context.error)


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """종목 통합 딥리서치 — DART + wisereport + 웹 3축 → 한 편의 통합 리포트.

    `/research <ticker 또는 종목명>`. 자유 텍스트(슬래시 없이 종목명만) 입력해도
    같은 동작 (text_handler가 _run_combined → deep_research.run 호출).
    """
    if not is_authorized(update):
        await deny_message(update, "종목봇")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "사용법: `/research 005930` 또는 `/research 삼성전자`\n"
            "_(슬래시 없이 종목명만 입력해도 동일 동작)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    query = " ".join(args).strip()
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    try:
        from src.deep_research import run as deep_research_run
    except Exception:
        logging.exception("deep_research 모듈 로드 실패")
        await update.message.reply_text("❌ 딥리서치 모듈 로드 실패 — 로그 확인")
        return
    asyncio.create_task(
        deep_research_run(bot, chat_id, query),
        name=f"research:{query}",
    ).add_done_callback(_log_task_exception)


async def cmd_curate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """종목 도메인 AI 큐레이션 — 주도주·학습가치 종목 선별 + Top N 리포트.

    `/curate [N]`. COMPANY_MODE 사용 (company 카테고리만, 산업·증권사 분포 균형).
    """
    if not is_authorized(update):
        await deny_message(update, "종목봇")
        return
    args = context.args or []
    n = 10
    if args:
        try:
            v = int(args[0])
            n = max(1, min(15, v))
        except (ValueError, TypeError):
            pass
    bot = context.bot
    chat_id = str(update.effective_chat.id)
    try:
        from src.curator import COMPANY_MODE, run_curated
    except Exception:
        logging.exception("curator 모듈 로드 실패 (company)")
        await update.message.reply_text("❌ 큐레이션 모듈 로드 실패")
        return
    await run_curated(bot, chat_id, n=n, mode=COMPANY_MODE)


COMPANY_COMMANDS = [
    ("research", "📋 통합 딥리서치 — DART+증권사+웹 → 한 편의 리포트 (8-15분)"),
    ("curate", "🎯 AI 큐레이션 — 주도주·학습가치 종목 Top N 선별"),
    ("report", "특정 종목 리포트 다운 + 요약 (분리 발송)"),
    ("deepdive", "DART 사업보고서·IR·재무차트 심층분석 (분리 발송)"),
    ("status", "현재 작업 진행 상태"),
    ("deephelp", "deepdive 상세 도움말"),
    ("help", "전체 도움말"),
]


def build_company_app(token: str) -> Application:
    """orchestrator가 import해 사용. CompanyBot의 Application을 반환.

    setMyCommands는 orchestrator가 app.initialize() 후 BotSpec.commands로 직접
    호출 (manual lifecycle에서 .post_init이 자동 호출 안 되므로).
    """
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("curate", cmd_curate))
    # --- deepdive (격리: 실패해도 기존 봇 정상) ---
    try:
        from src.deepdive.handler import register as register_deepdive
        register_deepdive(app)
    except Exception:
        logging.exception("deepdive 핸들러 등록 실패 — 기능 비활성화, 기존 봇은 정상 가동")
    # ----------------------------------------------
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        # 진단: 컨테이너에 어떤 환경변수가 inject 되었는지 출력
        all_keys = sorted(os.environ.keys())
        relevant = {
            k: (
                f"<set, len={len(os.environ[k])}>"
                if k in (
                    "TELEGRAM_BOT_TOKEN",
                    "WISEREPORT_PW",
                    "OPENROUTER_API_KEY",
                )
                else os.environ[k][:50]
            )
            for k in all_keys
            if any(
                kw in k.upper()
                for kw in ("TELEGRAM", "WISE", "OPEN", "ALLOWED", "CHAT")
            )
        }
        print("=" * 60, flush=True)
        print("DIAG: TELEGRAM_BOT_TOKEN 못 찾음", flush=True)
        print(f"DIAG: 전체 env key 개수 = {len(all_keys)}", flush=True)
        print(f"DIAG: 관련 env vars = {relevant}", flush=True)
        print(f"DIAG: 모든 env key 목록 = {all_keys}", flush=True)
        print("=" * 60, flush=True)
        raise SystemExit("TELEGRAM_BOT_TOKEN 환경변수 필수")

    allowed = get_allowed_ids()
    if not allowed:
        raise SystemExit(
            "ALLOWED_CHAT_IDS 환경변수 필수 (보안: 인가된 chat_id만 사용 가능). "
            "본인 chat_id를 콤마 구분으로 입력하세요. 예: 1813560888"
        )
    logging.info("인가 chat_id: %s", allowed)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    logging.info("봇 시작 — polling 모드")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
