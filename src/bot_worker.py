"""Telegram bot worker: 종목명/티커 받아서 wisereport 파이프라인 실행.

배포처(Railway/Fly.io 등) 에서 24/7 실행. 텔레그램으로 명령 받음.

명령 형식:
  /report 기업명 티커 [개수]   (예: /report 삼성전자 005930)
  /report 기업명 티커 5         (상위 5개만)
  또는 그냥 텍스트로: "기업명 티커"  (예: 삼성전자 005930)

환경변수 (전부 필수):
  TELEGRAM_BOT_TOKEN   - @BotFather에서 받은 봇 토큰
  ALLOWED_CHAT_IDS     - 콤마 구분 chat_id 화이트리스트 (예: 1813560888)
  WISEREPORT_ID, WISEREPORT_PW
  OPENROUTER_API_KEY, OPENROUTER_MODEL
  TELEGRAM_CHAT_ID 또는 TELEGRAM_USERNAME
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

REPO_ROOT = Path(__file__).resolve().parent.parent
HELP_TEXT = (
    "📊 *wisereport 자동 분석 봇*\n\n"
    "*사용법:*\n"
    "  `/report 기업명 티커 [개수]`\n"
    "  예: `/report 삼성전자 005930`\n"
    "  예: `/report 카카오 035720 5` (상위 5개)\n\n"
    "*간편 입력:* 슬래시 없이 텍스트로도 가능\n"
    "  예: `삼성전자 005930`\n"
    "  예: `현대차 005380 3`\n\n"
    "*명령:*\n"
    "  /start, /help — 이 도움말\n"
    "  /report — 리포트 작업 시작\n"
    "  /status — 현재 작업 상태\n\n"
    "_작업 1건당 약 8-15분 소요됩니다._"
)


def get_allowed_ids() -> set[str]:
    raw = os.getenv("ALLOWED_CHAT_IDS", "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_authorized(update: Update) -> bool:
    if not update.effective_chat:
        return False
    return str(update.effective_chat.id) in get_allowed_ids()


# ------------------------------------------------------------------
# 작업 큐 - 한 번에 하나만 실행 (wisereport 동시 로그인 충돌 방지)
# ------------------------------------------------------------------
TASK_LOCK = asyncio.Lock()
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
        return
    parsed = _parse_args(context.args)
    if not parsed:
        await update.message.reply_text(
            "사용법: `/report 기업명 6자리티커 [개수]`\n예: `/report 삼성전자 005930`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await _enqueue(update, *parsed)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    text = (update.message.text or "").strip()
    parts = text.split()
    parsed = _parse_args(parts)
    if not parsed:
        await update.message.reply_text(
            "잘 모르겠어요. `/help` 또는 `기업명 6자리티커` 형식으로 보내주세요.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await _enqueue(update, *parsed)


async def _enqueue(
    update: Update, name: str, ticker: str, top: int
) -> None:
    """작업 큐에 추가. 다른 작업이 진행중이면 대기 안내."""
    chat_id = update.effective_chat.id
    if TASK_LOCK.locked() and CURRENT_TASK is not None:
        await update.message.reply_text(
            f"⏳ 다른 작업 진행중: *{CURRENT_TASK['name']}*\n"
            f"끝나면 곧장 처리: *{name}* ({ticker}, top {top})",
            parse_mode=ParseMode.MARKDOWN,
        )

    asyncio.create_task(_run_pipeline(update, name, ticker, top))


async def _run_pipeline(
    update: Update, name: str, ticker: str, top: int
) -> None:
    """Lock 잡고 파이프라인 subprocess 실행."""
    global CURRENT_TASK
    async with TASK_LOCK:
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
