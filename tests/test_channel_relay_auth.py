"""channel_relay MTProto — 세션 무효(종료/폐기) 처리 검증 (네트워크·실제 telethon 없음).

사고(2026-10-21 Railway env 유출 후 사용자 세션 종료 예정): `async with client` = start()
→ get_me() None → Telethon 기본 콜백이 input()으로 전화번호를 물음 → EOFError →
20분마다 log.exception. 여기서는 가짜 telethon 모듈로:
  - input() 호출 0회 (start()/async with 미사용)
  - 인가 확인은 GetStateRequest 직접 호출 — is_user_authorized()는 FloodWait 등 RPCError를
    False로 삼켜 정상 세션을 영구 중단시키므로 미사용
  - AuthKeyUnregistered 등(인가 확인·읽기 단계 모두) → 프로세스 단위 중단 플래그
  - FloodWait·기타 RPC 오류 → 일시 오류 (중단·알림 없음)
  - admin 알림은 두 사이클에 걸쳐 1회, 새 세션 생성 도구(scripts/make_tg_session.py) 명시
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


class BadRequestError(RPCError):
    pass


class FloodError(RPCError):
    pass


class FloodWaitError(FloodError):
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class GetStateRequest:
    pass


class FakeClient:
    """Telethon TelegramClient 흉내 — start()/__aenter__는 진짜처럼 input()을 부른다."""

    instances: list["FakeClient"] = []
    raise_on_auth: BaseException | None = None  # GetStateRequest(인가 확인)에서 발생
    raise_on_get: BaseException | None = None
    messages: list = []

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.session = session
        self.kwargs = kwargs
        self.connected = False
        self.disconnect_calls = 0
        self.get_calls = 0
        self.requests: list = []
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, GetStateRequest) and FakeClient.raise_on_auth is not None:
            raise FakeClient.raise_on_auth
        return object()

    async def is_user_authorized(self):
        # 실제 Telethon은 GetStateRequest의 RPCError(FloodWait 포함)를 전부 False로 삼킨다
        raise AssertionError("is_user_authorized() 사용 금지 — 일시 오류를 세션 무효로 오판")

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
    functions = types.ModuleType("telethon.functions")
    for cls in (RPCError, UnauthorizedError, AuthKeyUnregisteredError, SessionRevokedError,
                UserDeactivatedError, AuthKeyError, AuthKeyDuplicatedError, AuthKeyNotFound,
                BadRequestError, FloodError, FloodWaitError):
        setattr(errors, cls.__name__, cls)
    sessions.StringSession = FakeStringSession
    functions.updates = types.SimpleNamespace(GetStateRequest=GetStateRequest)
    tl.TelegramClient = FakeClient
    tl.errors = errors
    tl.sessions = sessions
    tl.functions = functions
    monkeypatch.setitem(sys.modules, "telethon", tl)
    monkeypatch.setitem(sys.modules, "telethon.errors", errors)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    monkeypatch.setitem(sys.modules, "telethon.functions", functions)

    FakeClient.instances = []
    FakeClient.raise_on_auth = None
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
    """종료/폐기된 세션: 인가 확인(GetState)이 401 계열 → 사유는 정확한 예외 클래스명."""
    FakeClient.raise_on_auth = AuthKeyUnregisteredError("AUTH_KEY_UNREGISTERED")
    assert _fetch() == []
    assert relay == []  # input() 미호출
    assert channel_relay.mtproto_disabled_reason() == "AuthKeyUnregisteredError"
    (client,) = FakeClient.instances
    assert [type(r) for r in client.requests] == [GetStateRequest]
    assert client.get_calls == 0
    assert client.disconnect_calls == 1


@pytest.mark.parametrize("stage", ["auth", "get"])
@pytest.mark.parametrize("exc_cls", [
    AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedError, AuthKeyDuplicatedError,
])
def test_dead_session_errors_disable(relay, exc_cls, stage):
    if stage == "auth":
        FakeClient.raise_on_auth = exc_cls("dead")
    else:
        FakeClient.raise_on_get = exc_cls("dead")
    assert _fetch() == []
    assert relay == []
    assert channel_relay.mtproto_disabled_reason() == exc_cls.__name__
    assert FakeClient.instances[0].disconnect_calls == 1


@pytest.mark.parametrize("exc", [FloodWaitError(120), FloodWaitError(3600),
                                 BadRequestError("SOMETHING_INVALID"), RPCError("RPC_CALL_FAIL")],
                         ids=["flood120", "flood3600", "bad-request", "rpc"])
def test_transient_rpc_error_on_auth_check_does_not_disable(relay, exc, caplog):
    """FloodWait(>60초면 Telethon이 재-raise)·기타 RPC 오류는 세션 정상 — 이번 사이클만 폴백.

    이전: is_user_authorized()가 이를 False로 삼켜 정상 세션을 프로세스 수명 동안 중단 + 거짓 알림.
    """
    import logging
    caplog.set_level(logging.WARNING, logger="src.channel_relay")
    FakeClient.raise_on_auth = exc
    assert _fetch() == []
    assert channel_relay.mtproto_disabled_reason() is None
    (client,) = FakeClient.instances
    assert client.get_calls == 0 and client.disconnect_calls == 1
    # 다음 사이클은 다시 MTProto 시도 → 정상 복귀
    FakeClient.raise_on_auth = None
    FakeClient.messages = [types.SimpleNamespace(id=7, message="글", date=None, media=None)]
    assert [p.post_id for p in _fetch()] == ["DSInvResearch/7"]
    if isinstance(exc, FloodWaitError):
        assert f"FloodWait {exc.seconds}초" in caplog.text
        assert "Traceback" not in caplog.text


def test_flood_wait_on_get_messages_does_not_disable(relay):
    FakeClient.raise_on_get = FloodWaitError(300)
    assert _fetch() == []
    assert channel_relay.mtproto_disabled_reason() is None


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
    assert [type(r) for r in client.requests] == [GetStateRequest]  # 인가 확인 1회
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
    FakeClient.raise_on_auth = SessionRevokedError("SESSION_REVOKED")
    bot = _run_job_twice(monkeypatch)
    assert relay == []
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == "123456789"  # 발송 채널(RELAY_CHAT_ID) 아님 — admin
    assert "세션 무효" in text and "SessionRevokedError" in text
    # Railway 토큰 폐기 중엔 컨테이너 TG_LOGIN_* 흐름이 막히므로 PC 도구를 직접 안내
    assert "scripts/make_tg_session.py" in text and "TG_SESSION_STRING" in text
    assert "HANDOFF" not in text
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


def test_job_no_alert_on_long_flood_wait(relay, monkeypatch):
    """60초 넘는 FloodWait은 Telethon이 재-raise — 세션 무효 알림·중단 없이 두 사이클 모두 재시도."""
    FakeClient.raise_on_auth = FloodWaitError(120)
    bot = _run_job_twice(monkeypatch)
    assert bot.sent == []
    assert channel_relay.mtproto_disabled_reason() is None
    assert len(FakeClient.instances) == 2


def test_alert_falls_back_to_allowed_chat_ids(relay, monkeypatch):
    monkeypatch.delenv("MARKET_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "*,987654321")
    assert channel_relay._alert_env_keys() == ("ALLOWED_CHAT_IDS",)
