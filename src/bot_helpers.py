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

import asyncio
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


def md_escape(s: str) -> str:
    """레거시 텔레그램 Markdown 특수문자 이스케이프 (구버전 호환용)."""
    if not s:
        return s
    for ch in ("\\", "[", "]", "_", "*", "`"):
        s = s.replace(ch, "\\" + ch)
    return s


def html_escape(s: str) -> str:
    """텔레그램 parse_mode='HTML'용 이스케이프 — & < > 만 처리.
    레거시 Markdown은 한글 종목명·특수문자에 취약해 파싱 깨짐(=메시지 미발송)이 잦음.
    HTML이 훨씬 견고 → 메인 메시지/캡션 링크는 <a href="url">html_escape(text)</a> 사용.
    """
    if not s:
        return s
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_admin_chat_ids(*env_keys: str) -> list[str]:
    """env 여러 개를 합쳐서 진짜 chat_id(숫자 token)만 추출.

    걸러내는 케이스: '*' wildcard, 주석/공백, 음수·0 등 비정상 값.
    여러 봇이 동일 패턴이라 한 곳으로 통합 (screener_bot/us_screener_bot/report_bot
    및 admin_alerts 공통 사용).
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in env_keys:
        raw = os.getenv(key, "") or ""
        for token in raw.split(","):
            m = re.search(r"-?\d{6,}", token)
            if not m:
                continue
            cid = m.group(0)
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
    return out


async def send_channel_photo(bot, chat_id, photo, caption=None, parse_mode=None, attempts: int = 4):
    """채널에 사진 게시 — RetryAfter(flood control) 자동 대기·재시도.

    성공 시 Message, 실패 시 None. 채널은 ~20건/분 한도라 호출측에서 간격(>=3s)도 둘 것.
    """
    import asyncio
    from telegram.error import RetryAfter
    for i in range(attempts):
        try:
            return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption,
                                        parse_mode=parse_mode)
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 5)) + 1
            log.warning("send_channel_photo RetryAfter %ds (try %d/%d)", wait, i + 1, attempts)
            await asyncio.sleep(wait)
        except Exception:
            log.exception("send_channel_photo 실패 (chat_id=%s)", chat_id)
            return None
    return None


# ------------------------------------------------------------------
# 인가 (chat_id allowlist)
# ------------------------------------------------------------------
def allowed_chat_ids(env_key: str) -> set[str]:
    """env에서 콤마 구분 chat_id 목록 → set."""
    raw = os.getenv(env_key, "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_authorized(update: Update, env_key: str) -> bool:
    """update의 chat_id가 env_key의 allowlist에 있는지.

    Sentinel "*" 가 allowlist에 있으면 open mode — 모든 chat_id 허용.
    공개 운영 시 사용 (env에 "*" inject). 봇 username 알면 누구나 사용 가능
    하므로 비용·정보우위 손해 감수 영역.
    """
    if not update.effective_chat:
        return False
    allowed = allowed_chat_ids(env_key)
    if "*" in allowed:
        return True
    return str(update.effective_chat.id) in allowed


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
    # 줄 경계 우선 분할 — 마크다운 링크/단어가 청크 경계에서 깨지지 않게.
    # (한 줄이 MESSAGE_CHUNK보다 길면 그 줄만 하드 분할.)
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > MESSAGE_CHUNK:
            if buf:
                chunks.append(buf); buf = ""
            chunks.append(line[:MESSAGE_CHUNK]); line = line[MESSAGE_CHUNK:]
        add = line if not buf else "\n" + line
        if len(buf) + len(add) > MESSAGE_CHUNK:
            chunks.append(buf); buf = line
        else:
            buf += add
    if buf:
        chunks.append(buf)
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        body = f"[{idx}/{total}]\n{chunk}" if total >= 2 else chunk
        try:
            await bot.send_message(chat_id=chat_id, text=body, parse_mode=parse_mode)
        except Exception:
            # parse_mode(HTML/Markdown) 파싱 실패 등 → plain text로 폴백 재전송 (배달 보장)
            log.warning("send_message 1차 실패(parse_mode=%s) → plain 폴백 (chat_id=%s)",
                        parse_mode, chat_id)
            try:
                await bot.send_message(chat_id=chat_id, text=body)
            except Exception:
                log.exception("send_message 최종 실패 (chat_id=%s)", chat_id)
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


# ------------------------------------------------------------------
# LLM 요약 병렬 호출 — wisereport PIPELINE_LOCK 밖에서 사용
# ------------------------------------------------------------------
# OpenRouter 동시 요약 상한. 너무 크면 429. 5-8 권장.
_LLM_SUMMARY_CONCURRENCY = int(os.environ.get("LLM_SUMMARY_CONCURRENCY", "8"))


async def _summarize_pdfs_parallel(
    paths: list[Path], *, short_summary: bool, label: str,
) -> list[str]:
    """N개 PDF를 asyncio.gather + semaphore로 동시 요약.

    PIPELINE_LOCK 바깥에서 호출 — 같은 chat의 후속 발송 응답성을 위해.
    각 호출은 asyncio.to_thread로 워커 스레드에서 sync OpenRouter SDK 사용
    (OpenRouter SDK는 thread-safe). 실패는 자리에 "(요약 실패: ...)" 문자열로
    들어가 호출자의 zip(paths, summaries) 흐름이 깨지지 않음.

    - paths: 요약할 PDF 경로 리스트 (비어있으면 빈 리스트 반환).
    - short_summary=True → summarizer.summarize_pdf_short (짧음), False → summarize_pdf.
    - label: 실패 로그용 컨텍스트 (예: "산업 인기 5건" / "curator-company").
    """
    if not paths:
        return []
    # 지연 import — bot_helpers 자체는 summarizer를 강제 의존하지 않음.
    from src.summarizer import (
        OpenRouterCreditExhausted,
        get_client,
        summarize_pdf,
        summarize_pdf_short,
    )
    sum_client = get_client()
    fn = summarize_pdf_short if short_summary else summarize_pdf
    sem = asyncio.Semaphore(_LLM_SUMMARY_CONCURRENCY)

    async def _one(p: Path) -> str:
        async with sem:
            try:
                s = await asyncio.to_thread(fn, sum_client, p)
                return s.summary_text
            except OpenRouterCreditExhausted:
                log.warning("[%s] %s 요약 — OpenRouter 한도 초과", label, p.name)
                return "(요약 실패: OpenRouter 토큰 부족)"
            except Exception as e:
                log.exception("[%s] %s 요약 실패", label, p.name)
                return f"(요약 실패: {e!r})"

    return await asyncio.gather(*[_one(p) for p in paths])


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
