"""공개 텔레그램 채널 → 시황봇 릴레이.

목적: t.me/DSInvResearch 같은 공개 채널에 특정 저자(예: "DS투자전략 양형모")의
글이 올라오면 3시간 이내에 시황봇 chat으로 원문 전달.

방식 (듀얼 백엔드)
----
봇은 남의 채널에 멤버로 못 들어가므로 Bot API로는 채널 글을 못 읽는다.
1) **MTProto (권장·기본)**: TG_API_ID/TG_API_HASH/TG_SESSION_STRING 3종이 있으면
   Telethon 사용자 세션으로 채널 메시지를 직접 읽음. DSInvResearch처럼 웹
   프리뷰가 꺼진 채널도 동작. 세션 생성: `python scripts/make_tg_session.py`.
2) **웹 프리뷰 폴백**: 위 env 없으면 https://t.me/s/<channel> HTML 파싱.
   ⚠️ DSInvResearch는 프리뷰 비활성이라 이 경로로는 0건 — MTProto 필수.

- 폴링 간격: orchestrator cron 20분 → "3시간 이내" 여유 충족.
- dedup: state_store `relay_<channel>` 키에 post id 영속 (재배포에도 유지).
- 세션 무효(사용자가 종료/폐기, 2026-10-21 유출 사고 후속): `start()`/`async with` 금지 —
  Telethon이 input()으로 전화번호를 물어 이벤트 루프를 막는다. connect → is_user_authorized
  False 또는 AuthKeyUnregistered 등 → 프로세스 재시작(새 세션 배포)까지 MTProto 중단 +
  admin 알림 1회. 그동안 웹 프리뷰 폴백.
- 첫 가동(dedup 비어있음): 최근 RELAY_FIRST_RUN_WINDOW_H(기본 3시간) 내 글만
  발송하고 나머지는 조용히 seen 처리 — 과거 글 폭격 방지.

env
---
RELAY_CHANNEL          — 채널 핸들 (기본 "DSInvResearch")
RELAY_AUTHOR_KEYWORD   — 글 앞부분에서 찾을 저자 마커 (기본 "양형모")
RELAY_CHAT_ID          — 발송 대상 (기본 MARKET_CHAT_ID)
RELAY_HEAD_CHARS       — 저자 마커 탐색 범위 (기본 300자)
TG_API_ID / TG_API_HASH / TG_SESSION_STRING — MTProto 백엔드 (my.telegram.org)

이미지·표는 원문 링크로 대체 (텍스트 전문 + t.me 링크 첨부).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telegram import Bot

from src import state_store
from src.bot_helpers import KST, send_text_chunked

log = logging.getLogger(__name__)

PREVIEW_URL = "https://t.me/s/{channel}"
DEFAULT_CHANNEL = "DSInvResearch"
DEFAULT_AUTHOR_KEYWORD = "양형모"
FIRST_RUN_WINDOW_H = int(os.environ.get("RELAY_FIRST_RUN_WINDOW_H", "3"))

# 세션이 죽었다는 확정 신호 — 재시도해도 소용없음 (UnauthorizedError = 401 계열 전체)
_DEAD_SESSION_ERRORS = (
    "AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedError",
    "UserDeactivatedBanError", "AuthKeyDuplicatedError", "UnauthorizedError",
)
# 서버가 auth key를 모름(-404) — 일시적일 수도 있어 연속 N회일 때만 무효 처리
AUTH_KEY_NOT_FOUND_MAX = 3
SESSION_ALERT_TITLE = "🔑 텔레그램 사용자 세션 무효(종료/폐기됨) — 채널 릴레이 MTProto 중단"

# 프로세스 단위 상태 — 새 세션은 env 교체 + 재배포로만 들어오므로 재시작 시 초기화가 맞다.
_mtproto_disabled: str | None = None   # 중단 사유 (None = 정상)
_session_alert_sent = False
_auth_key_not_found_strikes = 0


@dataclass
class ChannelPost:
    post_id: str        # "DSInvResearch/1234"
    text: str           # 본문 (br → \n)
    posted_at: datetime | None  # aware UTC (t.me datetime attr)
    has_media: bool = False

    @property
    def url(self) -> str:
        return f"https://t.me/{self.post_id}"


def parse_preview_html(html: str) -> list[ChannelPost]:
    """t.me/s/<channel> HTML → 게시글 리스트 (오래된 것 → 최신 순).

    구조: div.tgme_widget_message[data-post="<ch>/<id>"] 안에
      .tgme_widget_message_text (본문), time[datetime] (게시 시각),
      .tgme_widget_message_photo_wrap (미디어 존재 신호).
    파싱 실패 게시글은 스킵 (graceful).
    """
    from bs4 import BeautifulSoup
    posts: list[ChannelPost] = []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        log.exception("channel_relay HTML 파싱 실패")
        return posts
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            post_id = msg.get("data-post", "").strip()
            if not post_id:
                continue
            text_el = msg.select_one(".tgme_widget_message_text")
            # 이미지-only 게시글은 text_el 없음 — 저자 판별 불가라 스킵 대상이지만
            # id는 seen 처리해야 하므로 빈 텍스트로 보존.
            text = ""
            if text_el:
                for br in text_el.find_all("br"):
                    br.replace_with("\n")
                text = text_el.get_text().strip()
            posted_at = None
            time_el = msg.select_one("time[datetime]")
            if time_el:
                try:
                    posted_at = datetime.fromisoformat(
                        time_el["datetime"].replace("Z", "+00:00")
                    )
                except (ValueError, KeyError):
                    pass
            has_media = bool(
                msg.select_one(".tgme_widget_message_photo_wrap")
                or msg.select_one(".tgme_widget_message_document")
            )
            posts.append(ChannelPost(
                post_id=post_id, text=text,
                posted_at=posted_at, has_media=has_media,
            ))
        except Exception:
            log.exception("channel_relay 게시글 1건 파싱 실패 — 스킵")
    return posts


def is_author_post(post: ChannelPost, keyword: str, head_chars: int = 300) -> bool:
    """글 앞 head_chars 안에 저자 마커가 있으면 True.

    사용자 관찰: "글의 앞 두 세 문장에서 'DS투자전략 양형모' 처럼 알려줌".
    """
    if not post.text or not keyword:
        return False
    return keyword in post.text[:head_chars]


def _mtproto_creds() -> tuple[int, str, str] | None:
    """TG_API_ID/TG_API_HASH/TG_SESSION_STRING 3종 모두 있으면 반환, 아니면 None."""
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_SESSION_STRING", "").strip()
    if api_id.isdigit() and api_hash and session:
        return int(api_id), api_hash, session
    return None


def mtproto_disabled_reason() -> str | None:
    """세션 무효로 MTProto가 중단됐으면 사유, 아니면 None."""
    return _mtproto_disabled


def _disable_mtproto(reason: str) -> None:
    global _mtproto_disabled
    if _mtproto_disabled is None:
        _mtproto_disabled = reason
        log.error("channel_relay: 텔레그램 사용자 세션 무효(%s) — 프로세스 재시작까지 MTProto 중단, "
                  "새 세션 필요", reason)


def _dead_session_errors(tl_errors) -> tuple[type, ...]:
    out = []
    for name in _DEAD_SESSION_ERRORS:
        cls = getattr(tl_errors, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            out.append(cls)
    return tuple(out)


async def fetch_posts_mtproto(channel: str, limit: int = 30) -> list[ChannelPost]:
    """Telethon 사용자 세션으로 채널 최근 메시지 읽기 (프리뷰 꺼진 채널도 OK).

    호출당 connect→읽기→disconnect. 20분 간격이라 flood limit 안전.
    creds 없거나 실패 시 빈 리스트 — 호출자가 폴백 판단.
    세션 무효면 mtproto_disabled_reason()에 사유가 남고 이후 호출은 즉시 빈 리스트.
    """
    global _auth_key_not_found_strikes
    if _mtproto_disabled:
        return []
    creds = _mtproto_creds()
    if not creds:
        return []
    try:
        from telethon import TelegramClient
        from telethon import errors as tl_errors
        from telethon.sessions import StringSession
    except ImportError:
        log.warning("channel_relay: telethon 미설치 — 웹 프리뷰 폴백")
        return []
    from src.tg_login import CLIENT_DEVICE_KWARGS

    dead_errors = _dead_session_errors(tl_errors)
    key_not_found = getattr(tl_errors, "AuthKeyNotFound", None)
    api_id, api_hash, session = creds
    posts: list[ChannelPost] = []
    client = None
    try:
        client = TelegramClient(StringSession(session), api_id, api_hash,
                                **CLIENT_DEVICE_KWARGS)
        # start()/async with 금지: 세션 무효 시 Telethon 기본 콜백이 input()으로 전화번호를
        # 물어 이벤트 루프 스레드를 막는다 → connect + 인가 확인만.
        await client.connect()
        if not await client.is_user_authorized():
            _disable_mtproto("is_user_authorized=False")
            return []
        msgs = await client.get_messages(channel, limit=limit)
        _auth_key_not_found_strikes = 0
        for m in reversed(msgs or []):  # 오래된 것 → 최신 순
            text = (getattr(m, "message", None) or "").strip()
            posts.append(ChannelPost(
                post_id=f"{channel}/{m.id}",
                text=text,
                posted_at=getattr(m, "date", None),
                has_media=bool(getattr(m, "media", None)),
            ))
    except dead_errors as e:
        _disable_mtproto(type(e).__name__)
        return []
    except Exception as e:
        if isinstance(key_not_found, type) and isinstance(e, key_not_found):
            _auth_key_not_found_strikes += 1
            log.warning("channel_relay: 서버가 세션 auth key를 모름 (%d/%d회 연속)",
                        _auth_key_not_found_strikes, AUTH_KEY_NOT_FOUND_MAX)
            if _auth_key_not_found_strikes >= AUTH_KEY_NOT_FOUND_MAX:
                _disable_mtproto(f"AuthKeyNotFound {_auth_key_not_found_strikes}회 연속")
            return []
        log.exception("channel_relay: MTProto 읽기 실패 (%s)", channel)
        return []
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:
                log.warning("channel_relay: MTProto disconnect 실패: %s", type(e).__name__)
    return posts


def _alert_env_keys() -> tuple[str, ...]:
    """세션 무효 알림 대상 — 시황봇 admin(MARKET_ALLOWED_CHAT_IDS) 우선, 없으면 ALLOWED_CHAT_IDS.

    RELAY_CHAT_ID/MARKET_CHAT_ID는 발송 채널(다른 구독자 포함 가능)이라 제외.
    """
    from src.bot_helpers import parse_admin_chat_ids
    for key in ("MARKET_ALLOWED_CHAT_IDS", "ALLOWED_CHAT_IDS"):
        if parse_admin_chat_ids(key):
            return (key,)
    return ()


async def _alert_session_invalid(bot: Bot) -> None:
    """세션 무효 admin 알림 — 프로세스당 1회 (실패해도 재시도 안 함: 20분마다 스팸 방지)."""
    global _session_alert_sent
    if _session_alert_sent or not _mtproto_disabled:
        return
    _session_alert_sent = True
    try:
        from src.admin_alerts import alert_admin
        await alert_admin(
            bot, _alert_env_keys(), SESSION_ALERT_TITLE,
            f"새 세션 필요 (HANDOFF 절차) — 사유: {_mtproto_disabled}. "
            "재배포 전까지 웹 프리뷰 폴백.",
        )
    except Exception:
        log.exception("channel_relay: 세션 무효 알림 실패")


def _format_relay(post: ChannelPost, channel: str, keyword: str) -> str:
    header = f"📨 [{channel}] {keyword} 신규 글"
    if post.has_media:
        header += "  (원문에 이미지/첨부 있음)"
    return f"{header}\n{'━' * 20}\n{post.text}\n\n🔗 {post.url}"


async def channel_relay_job(bot: Bot) -> None:
    """orchestrator cron (20분). 신규 저자 글 → RELAY_CHAT_ID(기본 MARKET_CHAT_ID) 발송."""
    channel = os.environ.get("RELAY_CHANNEL", DEFAULT_CHANNEL).strip().lstrip("@")
    keyword = os.environ.get("RELAY_AUTHOR_KEYWORD", DEFAULT_AUTHOR_KEYWORD).strip()
    chat_id = os.environ.get("RELAY_CHAT_ID") or os.environ.get("MARKET_CHAT_ID")
    head_chars = int(os.environ.get("RELAY_HEAD_CHARS", "300"))
    if not chat_id:
        log.warning("channel_relay: RELAY_CHAT_ID/MARKET_CHAT_ID 미설정 — skip")
        return

    import asyncio

    # 백엔드 1순위: MTProto (프리뷰 꺼진 채널도 읽힘)
    posts = await fetch_posts_mtproto(channel)
    backend = "mtproto"
    if _mtproto_disabled:
        await _alert_session_invalid(bot)

    if not posts:
        # 폴백: 웹 프리뷰 스크래핑
        backend = "preview"
        import httpx
        loop = asyncio.get_running_loop()

        def _fetch() -> str:
            resp = httpx.get(
                PREVIEW_URL.format(channel=channel),
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
                timeout=30.0, follow_redirects=True,
            )
            resp.raise_for_status()
            return resp.text

        try:
            html = await loop.run_in_executor(None, _fetch)
        except Exception:
            log.exception("channel_relay: t.me/s/%s fetch 실패 (다음 사이클 재시도)", channel)
            return
        posts = parse_preview_html(html)

    if not posts:
        log.warning(
            "channel_relay: 게시글 0건 (backend=%s) — 웹 프리뷰 비활성 채널이면 "
            "TG_API_ID/TG_API_HASH/TG_SESSION_STRING 설정 필요 (scripts/make_tg_session.py)",
            backend,
        )
        return

    dedup_key = f"relay_{channel}"
    seen_ids = state_store.seen(dedup_key)
    first_run = not seen_ids
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(hours=FIRST_RUN_WINDOW_H)

    sent = 0
    for post in posts:  # 오래된 것부터 — 발송 순서 보존
        if post.post_id in seen_ids:
            continue
        fresh_enough = post.posted_at is None or post.posted_at >= window_start
        # 첫 가동에는 최근 window 내 글만 발송 (과거 히스토리 폭격 방지).
        if is_author_post(post, keyword, head_chars) and (not first_run or fresh_enough):
            try:
                await send_text_chunked(bot, chat_id, _format_relay(post, channel, keyword))
                sent += 1
            except Exception:
                log.exception("channel_relay 발송 실패: %s", post.post_id)
                continue  # seen 처리 안 함 — 다음 사이클 재시도
        try:
            state_store.mark_seen(dedup_key, [post.post_id], cap=2000)
        except Exception:
            log.exception("channel_relay dedup 갱신 실패: %s", post.post_id)

    log.info(
        "channel_relay [%s→%s backend=%s]: parsed=%d, sent=%d%s",
        channel, chat_id, backend, len(posts), sent,
        " (first-run)" if first_run else "",
    )
