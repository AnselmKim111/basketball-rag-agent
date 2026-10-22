"""channel_relay MTProto — 세션 무효(종료/폐기) 처리 검증 (네트워크·실제 telethon 없음).

사고(2026-10-21 Railway env 유출 후 사용자 세션 종료 예정): `async with client` = start()
→ get_me() None → Telethon 기본 콜백이 input()으로 전화번호를 물음 → EOFError →
20분마다 log.exception. 여기서는 가짜 telethon 모듈로:
  - input() 호출 0회 (start()/async with 미사용)
  - is_user_authorized False / AuthKeyUnregistered 등 → 프로세스 단위 중단 플래그
  - admin 알림은 두 사이클에 걸쳐 1회
  - 모든 경로에서 disconnect
"""
from __future__ import annotations

import asyncio
import builtins
import sys
import types
from datetime import datetime, timezone

import pytest

from src import channel_relay
from src.tg_login import CLIENT_DEVICE_KWARGS


# ------------------------------------------------------------------
# 가짜 telethon
# ------------------------------------------------------------------
class RPCError(Exception):
    pass


class UnauthorizedError(RPCError):
    pass


class AuthKeyUnregisteredError(UnauthorizedError):
    pass


class SessionRevokedError(UnauthorizedError):
    pass


class UserDeactivatedError(UnauthorizedError):
    pass


class AuthKeyError(RPCError):
    pass


class AuthKeyDuplicatedError(AuthKeyError):
    pass


class AuthKeyNotFound(Exception):
    pass


class FakeClient:
    """Telethon TelegramClient 흉내 — start()/__aenter__는 진짜처럼 input()을 부른다."""

    instances: list["FakeClient"] = []
    authorized = True
    raise_on_get: BaseException | None = None
    messages: list = []

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.session = session
        self.kwargs = kwargs
        self.connected = False
        self.disconnect_calls = 0
        self.get_calls = 0
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return FakeClient.authorized

    async def start(self, *a, **kw):
        # 실제 Telethon: phone=lambda: input('Please enter your phone (or bot token): ')
        input("Please enter your phone (or bot token): ")
        return self

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.disconnect()

    async def get_messages(self, channel, limit=30):
        self.get_calls += 1
        if FakeClient.raise_on_get is not None:
            raise FakeClient.raise_on_get
        return list(FakeClient.messages)

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False


class FakeStringSession:
    def __init__(self, s=None):
        self.s = s


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((str(chat_id), text))


@pytest.fixture
def relay(monkeypatch):
    """가짜 telethon 설치 + 모듈 상태 초기화 + input() 감시."""
    tl = types.ModuleType("telethon")
    errors = types.ModuleType("telethon.errors")
    sessions = types.ModuleType("telethon.sessions")
    for cls in (RPCError, UnauthorizedError, AuthKeyUnregisteredError, SessionRevokedError,
                UserDeactivatedError, AuthKeyError, AuthKeyDuplicatedError, AuthKeyNotFound):
        setattr(errors, cls.__name__, cls)
    sessions.StringSession = FakeStringSession
    tl.TelegramClient = FakeClient
    tl.errors = errors
    tl.sessions = sessions
    monkeypatch.setitem(sys.modules, "telethon", tl)
    monkeypatch.setitem(sys.modules, "telethon.errors", errors)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)

    FakeClient.instances = []
    FakeClient.authorized = True
    FakeClient.raise_on_get = None
    FakeClient.messages = []

    monkeypatch.setattr(channel_relay, "_mtproto_disabled", None)
    monkeypatch.setattr(channel_relay, "_session_alert_sent", False)
    monkeypatch.setattr(channel_relay, "_auth_key_not_found_strikes", 0)

    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "fakehash0000")
    monkeypatch.setenv("TG_SESSION_STRING", "fake-session-string")

    input_calls: list[str] = []

    def _no_input(prompt=""):
        input_calls.append(prompt)
        raise AssertionError("input() 호출 금지 — 컨테이너엔 stdin 없음")

    monkeypatch.setattr(builtins, "input", _no_input)
    return input_calls


def _fetch():
    return asyncio.run(channel_relay.fetch_posts_mtproto("DSInvResearch"))


# ------------------------------------------------------------------
# fetch_posts_mtproto
# ------------------------------------------------------------------
def test_unauthorized_session_disables_without_input(relay):
    FakeClient.authorized = False
    assert _fetch() == []
    assert relay == []  # input() 미호출
    assert channel_relay.mtproto_disabled_reason() == "is_user_authorized=False"
    (client,) = FakeClient.instances
    assert client.get_calls == 0
    assert client.disconnect_calls == 1


@pytest.mark.parametrize("exc_cls", [
    AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedError, AuthKeyDuplicatedError,
])
def test_dead_session_errors_disable(relay, exc_cls):
    FakeClient.raise_on_get = exc_cls("dead")
    assert _fetch() == []
    assert relay == []
    assert channel_relay.mtproto_disabled_reason() == exc_cls.__name__
    assert FakeClient.instances[0].disconnect_calls == 1


def test_disabled_skips_client_entirely(relay):
    FakeClient.raise_on_get = AuthKeyUnregisteredError("dead")
    _fetch()
    assert len(FakeClient.instances) == 1
    # 이후 호출은 클라이언트를 만들지도 않음 (프로세스 재시작까지)
    FakeClient.raise_on_get = None
    assert _fetch() == []
    assert len(FakeClient.instances) == 1


def test_transient_error_does_not_disable(relay):
    FakeClient.raise_on_get = ConnectionError("network down")
    assert _fetch() == []
    assert channel_relay.mtproto_disabled_reason() is None
    assert FakeClient.instances[0].disconnect_calls == 1


def test_auth_key_not_found_disables_only_after_consecutive_strikes(relay):
    FakeClient.raise_on_get = AuthKeyNotFound()
    for _ in range(channel_relay.AUTH_KEY_NOT_FOUND_MAX - 1):
        _fetch()
    assert channel_relay.mtproto_disabled_reason() is None
    _fetch()
    assert "AuthKeyNotFound" in channel_relay.mtproto_disabled_reason()
    assert all(c.disconnect_calls == 1 for c in FakeClient.instances)


def test_auth_key_not_found_strikes_reset_on_success(relay):
    FakeClient.raise_on_get = AuthKeyNotFound()
    for _ in range(channel_relay.AUTH_KEY_NOT_FOUND_MAX - 1):
        _fetch()
    FakeClient.raise_on_get = None
    _fetch()
    FakeClient.raise_on_get = AuthKeyNotFound()
    _fetch()
    assert channel_relay.mtproto_disabled_reason() is None


def test_success_reads_posts_with_device_model(relay):
    t = datetime(2026, 10, 22, tzinfo=timezone.utc)
    FakeClient.messages = [  # Telethon: 최신 → 오래된 순
        types.SimpleNamespace(id=2, message="둘째", date=t, media=None),
        types.SimpleNamespace(id=1, message="첫째", date=t, media=object()),
    ]
    posts = _fetch()
    assert [p.post_id for p in posts] == ["DSInvResearch/1", "DSInvResearch/2"]
    assert posts[0].has_media and not posts[1].has_media
    (client,) = FakeClient.instances
    assert client.kwargs["device_model"] == "IdeaBot relay (Railway)"
    assert client.kwargs == CLIENT_DEVICE_KWARGS
    assert client.disconnect_calls == 1
    assert channel_relay.mtproto_disabled_reason() is None
    assert relay == []


# ------------------------------------------------------------------
# channel_relay_job — admin 알림 1회
# ------------------------------------------------------------------
def _run_job_twice(monkeypatch):
    import httpx

    def _no_preview(*a, **kw):
        raise RuntimeError("preview off")

    monkeypatch.setattr(httpx, "get", _no_preview, raising=False)
    monkeypatch.setenv("RELAY_CHAT_ID", "-1009999999999")
    monkeypatch.setenv("MARKET_ALLOWED_CHAT_IDS", "123456789")
    bot = FakeBot()

    async def _go():
        await channel_relay.channel_relay_job(bot)
        await channel_relay.channel_relay_job(bot)

    asyncio.run(_go())
    return bot


def test_job_alerts_admin_once_across_two_cycles(relay, monkeypatch):
    FakeClient.authorized = False
    bot = _run_job_twice(monkeypatch)
    assert relay == []
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == "123456789"  # 발송 채널(RELAY_CHAT_ID) 아님 — admin
    assert "세션 무효" in text and "HANDOFF" in text
    assert "fake-session-string" not in text
    assert len(FakeClient.instances) == 1  # 두 번째 사이클은 MTProto 시도 안 함
    assert FakeClient.instances[0].disconnect_calls == 1


def test_job_alert_on_auth_key_unregistered(relay, monkeypatch):
    FakeClient.raise_on_get = AuthKeyUnregisteredError("AUTH_KEY_UNREGISTERED")
    bot = _run_job_twice(monkeypatch)
    assert len(bot.sent) == 1
    assert "AuthKeyUnregisteredError" in bot.sent[0][1]
    assert relay == []


def test_job_no_alert_when_session_healthy_but_transient_error(relay, monkeypatch):
    FakeClient.raise_on_get = ConnectionError("blip")
    bot = _run_job_twice(monkeypatch)
    assert bot.sent == []
    assert channel_relay.mtproto_disabled_reason() is None


def test_alert_falls_back_to_allowed_chat_ids(relay, monkeypatch):
    monkeypatch.delenv("MARKET_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "*,987654321")
    assert channel_relay._alert_env_keys() == ("ALLOWED_CHAT_IDS",)
