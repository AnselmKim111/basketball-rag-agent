"""Telegram Bot API로 PDF + 텍스트 요약 발송.

사전 준비 (꼭 필요):
  1) @BotFather에서 봇 생성 → bot token 받기 (TELEGRAM_BOT_TOKEN)
  2) 본인 텔레그램에서 그 봇과 대화 시작 (`/start`)
  3) https://api.telegram.org/bot<TOKEN>/getUpdates 로 chat_id 확인
     혹은 그냥 .env의 TELEGRAM_USERNAME에 @allymax를 설정하면 자동 검색

봇 토큰 없이는 텔레그램 발송이 불가능합니다 (Telegram의 보안 정책).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def resolve_chat_id(token: str, username: str) -> str | None:
    """getUpdates에서 username과 일치하는 chat_id를 찾기.

    이 동작은 봇이 이미 해당 사용자와 대화를 시작했어야 작동.
    """
    username = username.lstrip("@").lower()
    try:
        r = httpx.get(
            f"{TELEGRAM_API}/bot{token}/getUpdates",
            timeout=15.0,
            verify=False,
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
    except Exception as e:
        log.warning("getUpdates 실패: %s", e)
        return None

    for upd in reversed(updates):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if (chat.get("username") or "").lower() == username:
            return str(chat.get("id"))
    return None


def get_chat_id() -> str | None:
    """env에서 chat_id 직접 지정 → 없으면 username으로 룩업."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if chat_id:
        return chat_id
    username = os.getenv("TELEGRAM_USERNAME")
    if not (token and username):
        return None
    return resolve_chat_id(token, username)


def send_message(token: str, chat_id: str, text: str) -> bool:
    """텍스트 메시지 발송. 4096자 초과 시 자동 분할."""
    chunks = []
    while text:
        chunks.append(text[:4000])
        text = text[4000:]
    for chunk in chunks:
        try:
            r = httpx.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=30.0,
                verify=False,
            )
            r.raise_for_status()
        except Exception:
            log.exception("텔레그램 텍스트 발송 실패")
            return False
    return True


def send_document(token: str, chat_id: str, path: Path, caption: str = "") -> bool:
    """파일 1개 발송. 50MB 초과는 봇 API 한계로 못 보냄."""
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > 49:
        log.warning("파일이 너무 큽니다 (%.1fMB), 스킵: %s", size_mb, path.name)
        return False
    try:
        with path.open("rb") as f:
            r = httpx.post(
                f"{TELEGRAM_API}/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (path.name, f, "application/pdf")},
                timeout=120.0,
                verify=False,
            )
            r.raise_for_status()
        return True
    except Exception:
        log.exception("텔레그램 파일 발송 실패: %s", path.name)
        return False


def send_all(
    pdf_paths: list[Path],
    summary_text_path: Path,
    company_name: str,
    summary_combined_text: str,
) -> bool:
    """
    PDF 10개 + 종합 요약 텍스트 + 종합 리포트 파일을 모두 발송.
    환경변수 TELEGRAM_BOT_TOKEN 필수, TELEGRAM_CHAT_ID 또는 TELEGRAM_USERNAME 중 하나 필수.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error(
            "TELEGRAM_BOT_TOKEN 미설정. @BotFather로 봇을 만들고 토큰을 .env에 넣어주세요."
        )
        return False
    chat_id = get_chat_id()
    if not chat_id:
        log.error(
            "Telegram chat_id를 찾지 못했습니다. .env에 TELEGRAM_CHAT_ID를 직접 설정하거나 "
            "TELEGRAM_USERNAME=@yourusername 후 봇과 먼저 대화(/start)해 주세요."
        )
        return False

    log.info("텔레그램 발송 시작 (chat_id=%s)", chat_id)

    # 1) 헤더 메시지 + 종합 요약 본문
    intro = f"📊 {company_name} 리포트 분석 ({len(pdf_paths)}건)\n\n[종합 분석]\n\n"
    if not send_message(token, chat_id, intro + summary_combined_text):
        return False

    # 2) 종합 리포트 텍스트 파일
    if summary_text_path.exists():
        send_document(
            token, chat_id, summary_text_path,
            caption=f"{company_name} 종합 리포트 (개별 요약 포함)",
        )

    # 3) 원본 PDF 10건
    for i, pdf in enumerate(pdf_paths, start=1):
        send_document(
            token, chat_id, pdf,
            caption=f"[{i}/{len(pdf_paths)}] {pdf.name}",
        )

    log.info("텔레그램 발송 완료")
    return True
