"""신규 봇 모듈에서 재사용하는 공용 헬퍼.

기존 봇(CompanyBot/IndustryBot/MarketBot/GlobalBot)은 자신의 모듈에 동등한
private 헬퍼(_send_text 등)가 이미 있어 여기를 import하지 않아도 동작.
새 봇(예: 아이디어봇)은 이 모듈만 import해 작성한다 → category_bots.py를
수정할 필요 없음 = 머지 충돌 없음.

설계 원칙:
  - 모든 함수는 graceful (예외를 raise하지 않음 — 봇 프로세스를 죽이지 않게).
  - 외부 자원(텔레그램 API, 파일 시스템) 한 호출 단위로 try/except.
  - PIPELINE_LOCK은 각 봇이 직접 잡는다 (이 헬퍼는 lock 모름).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable

from telegram import Bot, Update

log = logging.getLogger(__name__)

# 메시지 분할 임계 (텔레그램 4096 char limit, 안전 마진 4000)
MESSAGE_CHUNK = 4000
# 파일 업로드 한도 (텔레그램 봇 API 50MB)
MAX_DOC_MB = 49


# ------------------------------------------------------------------
# 인가 (chat_id allowlist)
# ------------------------------------------------------------------
def allowed_chat_ids(env_key: str) -> set[str]:
    """env에서 콤마 구분 chat_id 목록 → set."""
    raw = os.getenv(env_key, "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_authorized(update: Update, env_key: str) -> bool:
    """update의 chat_id가 env_key의 allowlist에 있는지."""
    if not update.effective_chat:
        return False
    return str(update.effective_chat.id) in allowed_chat_ids(env_key)


# ------------------------------------------------------------------
# 메시지 발송 (텔레그램 PTB Bot)
# ------------------------------------------------------------------
async def send_text_chunked(
    bot: Bot, chat_id: str | int, text: str, parse_mode: str | None = None,
) -> None:
    """긴 텍스트를 4000자 단위로 쪼개서 발송. 2개 이상 청크면 `[1/N]` 헤더로
    페이징 표시 (사용자가 흐름 파악). 발송 실패는 로그만.
    """
    if not text:
        return
    # 청크 미리 계산 — 페이징 헤더에 총 개수 표기 위해.
    chunks: list[str] = []
    rest = text
    while rest:
        chunks.append(rest[:MESSAGE_CHUNK])
        rest = rest[MESSAGE_CHUNK:]
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        body = f"[{idx}/{total}]\n{chunk}" if total >= 2 else chunk
        try:
            await bot.send_message(chat_id=chat_id, text=body, parse_mode=parse_mode)
        except Exception:
            log.exception("send_message 실패 (chat_id=%s)", chat_id)
            break


async def deny_message(update: Update, bot_label: str = "이 봇") -> None:
    """비인가 chat_id에 일관된 거부 메시지. 모든 핸들러의 silent return 대신 호출.

    봇 라벨을 받아 어떤 봇이 거부했는지 명시 (사용자가 다른 봇 사용 중일 수 있음).
    update.message가 없으면 silent.
    """
    if not update.message or not update.effective_chat:
        return
    try:
        await update.message.reply_text(
            f"🔒 {bot_label}은 인가된 사용자만 이용 가능합니다.\n"
            f"chat_id `{update.effective_chat.id}` 를 운영자에게 전달하세요.",
            parse_mode="Markdown",
        )
    except Exception:
        log.exception("deny_message 발송 실패 (chat_id=%s)", update.effective_chat.id)


async def send_pdf(
    bot: Bot, chat_id: str | int, path: Path, caption: str = "",
) -> None:
    """PDF 발송. 50MB 초과나 발송 실패 시 로그만."""
    try:
        size_mb = path.stat().st_size / 1024 / 1024
    except Exception:
        log.exception("파일 stat 실패: %s", path)
        return
    if size_mb > MAX_DOC_MB:
        log.warning("파일 너무 큼 (%.1fMB) 스킵: %s", size_mb, path.name)
        return
    try:
        with path.open("rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=path.name,
                caption=caption[:1024],
            )
    except Exception:
        log.exception("send_document 실패: %s", path.name)


# ------------------------------------------------------------------
# 파일/경로 유틸
# ------------------------------------------------------------------
def safe_dirname(name: str) -> str:
    """파일시스템 안전한 디렉터리명. 빈/공백이면 'category'."""
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name or "")
    return name.strip().strip(".") or "category"


def download_root_for(subdir: str = "") -> Path:
    """공용 다운로드 루트. DOWNLOAD_DIR env 우선, 기본 ./downloads."""
    base = Path(os.environ.get("DOWNLOAD_DIR", "./downloads"))
    return base / safe_dirname(subdir) if subdir else base


# ------------------------------------------------------------------
# 환경 진단 (로그용)
# ------------------------------------------------------------------
class MissingWisereportCreds(RuntimeError):
    """WISEREPORT_ID/PW 환경변수 미설정."""


def wisereport_creds() -> tuple[str, str]:
    """(WISEREPORT_ID, WISEREPORT_PW). 미설정이면 MissingWisereportCreds raise.

    `os.environ["..."]` 직접 접근은 KeyError로 봇 프로세스에 traceback이 그대로
    노출되므로, 호출자가 잡아 사용자 친화적 메시지로 변환할 수 있게 typed error로 감쌈.
    """
    uid = os.environ.get("WISEREPORT_ID")
    pw = os.environ.get("WISEREPORT_PW")
    if not uid or not pw:
        missing = [k for k, v in (("WISEREPORT_ID", uid), ("WISEREPORT_PW", pw)) if not v]
        raise MissingWisereportCreds(f"환경변수 미설정: {', '.join(missing)}")
    return uid, pw


def diag_env_keys(prefixes: Iterable[str]) -> dict[str, str]:
    """주어진 prefix가 들어간 env key를 마스킹된 형태로 반환 (로깅 안전)."""
    out: dict[str, str] = {}
    for k in sorted(os.environ.keys()):
        if not any(p in k.upper() for p in prefixes):
            continue
        v = os.environ[k]
        if any(s in k.upper() for s in ("TOKEN", "PW", "API_KEY", "SECRET")):
            out[k] = f"<set, len={len(v)}>"
        else:
            out[k] = v[:60]
    return out
