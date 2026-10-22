"""tg_login — env 상태 머신(decide) + 종료 경로별 정리(run_boot_login) 검증."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import types

import pytest

from src import tg_login

CREDS = {"TG_API_ID": "123", "TG_API_HASH": "abc"}


def test_noop_without_anything():
    assert tg_login.decide({}, state_exists=False) == "noop"
    assert tg_login.decide(CREDS, state_exists=False) == "noop"


def test_session_present_is_noop_or_cleanup():
    assert tg_login.decide({"TG_SESSION_STRING": "s"}, False) == "noop"
    assert tg_login.decide({"TG_SESSION_STRING": "s", "TG_LOGIN_CODE": "1"}, False) == "cleanup"
    assert tg_login.decide({"TG_SESSION_STRING": "s"}, True) == "cleanup"


def test_phone_without_creds():
    assert tg_login.decide({"TG_LOGIN_PHONE": "+82"}, False) == "missing_creds"


def test_send_code_then_wait_then_sign_in():
    env = {**CREDS, "TG_LOGIN_PHONE": "+82"}
    assert tg_login.decide(env, False) == "send_code"
    assert tg_login.decide(env, True) == "wait"
    assert tg_login.decide({**env, "TG_LOGIN_CODE": "12345"}, True) == "sign_in"


def test_code_without_state_resends():
    """상태파일 없이 코드만 있으면 phone_code_hash가 없으므로 새로 코드 요청."""
    env = {**CREDS, "TG_LOGIN_PHONE": "+82", "TG_LOGIN_CODE": "1"}
    assert tg_login.decide(env, False) == "send_code"


def test_state_path_prefers_volume(monkeypatch):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")
    assert str(tg_login._state_path()) == "/data/tg_login_state.json"


# ======================================================================
# run_boot_login — 종료 경로별 정리 (2026-10-21 Railway env 유출 사고 후속)
#   - 모든 종료 경로: TG_LOGIN_CODE/PASSWORD 삭제(전화번호는 성공 시만) + 상태파일 삭제
#   - Railway 변경은 자기 서비스(RAILWAY_SERVICE_ID)만
#   - Railway API 불가면 로그인 흐름 미시작
# 가짜 telethon·railway_env 사용 (네트워크 없음).
# ======================================================================
OWN_SID = "svc-own-0001"
OTHER_SID = "svc-report-0002"
STATE_SESSION = "auth-key-from-state-file"


class RPCError(Exception):
    pass


class SessionPasswordNeededError(RPCError):
    pass


class PasswordHashInvalidError(RPCError):
    pass


class PhoneCodeExpiredError(RPCError):
    pass


class PhoneCodeInvalidError(RPCError):
    pass


class FakeStringSession:
    def __init__(self, s=None):
        self.s = s or "fresh-unauthorized-key"

    def save(self):
        return self.s


class FakeClient:
    instances: list["FakeClient"] = []
    send_code_exc: BaseException | None = None
    code_exc: BaseException | None = None
    needs_password = False
    correct_password = "right-pw-value"
    log_out_ok = True

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.session = session
        self.kwargs = kwargs
        self.disconnects = 0
        self.logged_out = False
        FakeClient.instances.append(self)

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnects += 1

    async def send_code_request(self, phone):
        if FakeClient.send_code_exc is not None:
            raise FakeClient.send_code_exc
        n = len(FakeClient.instances)
        self.session.s = f"new-temp-key-{n}"
        return types.SimpleNamespace(phone_code_hash=f"new-hash-{n}")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is None:
            if FakeClient.code_exc is not None:
                raise FakeClient.code_exc
            if FakeClient.needs_password:
                raise SessionPasswordNeededError("SESSION_PASSWORD_NEEDED")
            return types.SimpleNamespace(id=42, first_name="Alice", username="alice_priv")
        if password != FakeClient.correct_password:
            raise PasswordHashInvalidError("PASSWORD_HASH_INVALID")
        return types.SimpleNamespace(id=42, first_name="Alice", username="alice_priv")

    async def get_me(self):
        return types.SimpleNamespace(id=42, first_name="Alice", username="alice_priv")

    async def log_out(self):
        self.logged_out = True
        return FakeClient.log_out_ok


class FakeRailway:
    def __init__(self, available=True, delete_ok=True, upsert_ok=True):
        self._available = available
        self.delete_ok = delete_ok
        self.upsert_ok = upsert_ok
        self.deletes: list[tuple[str, list | None]] = []
        self.upserts: list[tuple[str, str, list | None]] = []

    def available(self):
        return self._available

    def delete_variable(self, name, service_ids=None):
        self.deletes.append((name, service_ids))
        return (True, "✅ 1개 서비스 삭제") if self.delete_ok else (False, "❌ HTTP 401")

    def upsert_variable(self, name, value, service_ids=None):
        self.upserts.append((name, value, service_ids))
        return (True, "✅ 1개 서비스 upsert 성공") if self.upsert_ok else (False, "❌ 전부 실패")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """가짜 telethon + 가짜 railway_env + 격리된 env/상태파일 경로."""
    from src.model_router import railway_env

    tl = types.ModuleType("telethon")
    errors = types.ModuleType("telethon.errors")
    sessions = types.ModuleType("telethon.sessions")
    for cls in (RPCError, SessionPasswordNeededError, PasswordHashInvalidError,
                PhoneCodeExpiredError, PhoneCodeInvalidError):
        setattr(errors, cls.__name__, cls)
    sessions.StringSession = FakeStringSession
    tl.TelegramClient = FakeClient
    tl.errors = errors
    tl.sessions = sessions
    monkeypatch.setitem(sys.modules, "telethon", tl)
    monkeypatch.setitem(sys.modules, "telethon.errors", errors)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    FakeClient.instances = []
    FakeClient.send_code_exc = None
    FakeClient.code_exc = None
    FakeClient.needs_password = False
    FakeClient.log_out_ok = True

    fake = FakeRailway()
    for attr in ("available", "delete_variable", "upsert_variable"):
        monkeypatch.setattr(railway_env, attr, getattr(fake, attr))

    for k in ("TG_SESSION_STRING", "RAILWAY_VOLUME_MOUNT_PATH", *tg_login.LOGIN_ENVS):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TG_API_ID", "123456")
    monkeypatch.setenv("TG_API_HASH", "fake-api-hash-abc")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", OWN_SID)
    monkeypatch.setenv("RAILWAY_SERVICE_IDS", f"{OWN_SID},{OTHER_SID}")
    monkeypatch.setenv("TG_LOGIN_PHONE", "+821012345678")
    return fake


def _state_file():
    return tg_login._state_path()


def _write_state():
    p = _state_file()
    p.write_text(json.dumps({"phone": "+821012345678", "phone_code_hash": "old-hash",
                             "session": STATE_SESSION}))
    return p


def _run():
    said: list[str] = []

    async def notify(msg):
        said.append(msg)

    action = asyncio.run(tg_login.run_boot_login(notify))
    return action, said


def _deleted(fake):
    assert all(sids == [OWN_SID] for _, sids in fake.deletes), fake.deletes
    return {n for n, _ in fake.deletes}


def _sign_in_env(monkeypatch, password=None):
    _write_state()
    monkeypatch.setenv("TG_LOGIN_CODE", "12345")
    if password is not None:
        monkeypatch.setenv("TG_LOGIN_PASSWORD", password)


def test_success_deletes_all_login_envs_and_upserts_own_service_only(env, monkeypatch, caplog):
    _sign_in_env(monkeypatch, password="right-pw-value")
    FakeClient.needs_password = True
    caplog.set_level(logging.INFO, logger="src.tg_login")
    action, said = _run()
    assert action == "signed_in"
    assert _deleted(env) == {"TG_LOGIN_PHONE", "TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"}
    assert env.upserts == [("TG_SESSION_STRING", STATE_SESSION, [OWN_SID])]
    assert not _state_file().exists()
    for n in tg_login.LOGIN_ENVS:
        assert n not in os.environ
    assert all(STATE_SESSION not in m for m in said)
    # 계정 이름·@username 로그 금지
    assert "Alice" not in caplog.text and "alice_priv" not in caplog.text
    assert FakeClient.instances[0].kwargs["device_model"] == "IdeaBot relay (Railway)"


def test_send_code_keeps_state_and_drops_stale_code(env, monkeypatch):
    monkeypatch.setenv("TG_LOGIN_CODE", "99999")  # 이전 시도 잔여 — 새 hash와 안 맞음
    monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
    action, said = _run()
    assert action == "send_code"
    assert _state_file().exists()  # 다음 코드 대기 — 유지
    assert _deleted(env) == {"TG_LOGIN_CODE"}
    assert "TG_LOGIN_CODE" not in os.environ
    assert os.environ.get("TG_LOGIN_PHONE")
    assert env.upserts == []
    assert FakeClient.instances[0].kwargs == tg_login.CLIENT_DEVICE_KWARGS
    assert len(said) == 1 and "코드를 보냈습니다" in said[0]


def test_send_code_failure_cleans_up(env, monkeypatch):
    monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
    FakeClient.send_code_exc = RuntimeError("flood")
    action, _ = _run()
    assert action == "send_code_failed"
    assert _deleted(env) == {"TG_LOGIN_PASSWORD"}
    assert not _state_file().exists()
    assert os.environ.get("TG_LOGIN_PHONE")


def test_code_expired_resends_and_keeps_new_state(env, monkeypatch):
    _sign_in_env(monkeypatch, password="right-pw-value")
    FakeClient.code_exc = PhoneCodeExpiredError("PHONE_CODE_EXPIRED")
    action, said = _run()
    assert action == "code_expired_resent"
    assert _deleted(env) == {"TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"}
    state = json.loads(_state_file().read_text())  # 새 코드 대기 상태파일
    assert state["phone_code_hash"].startswith("new-hash-")
    assert state["session"] != STATE_SESSION
    assert env.upserts == []
    assert "TG_LOGIN_PHONE" in os.environ


def test_code_expired_resend_failure_unlinks_state(env, monkeypatch):
    _sign_in_env(monkeypatch)
    FakeClient.code_exc = PhoneCodeExpiredError("PHONE_CODE_EXPIRED")
    FakeClient.send_code_exc = RuntimeError("flood")
    action, _ = _run()
    assert action == "code_expired"
    assert _deleted(env) == {"TG_LOGIN_CODE"}
    assert not _state_file().exists()


def test_password_needed(env, monkeypatch):
    _sign_in_env(monkeypatch)  # 비밀번호 env 없음
    FakeClient.needs_password = True
    action, said = _run()
    assert action == "password_needed"
    assert _deleted(env) == {"TG_LOGIN_CODE"}
    assert not _state_file().exists()
    assert len(said) == 1 and "필요" in said[0] and "틀렸" not in said[0]
    assert env.upserts == []


def test_password_wrong(env, monkeypatch):
    _sign_in_env(monkeypatch, password="wrong-pw-value")
    FakeClient.needs_password = True
    action, said = _run()
    assert action == "password_wrong"
    assert _deleted(env) == {"TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"}
    assert "TG_LOGIN_PASSWORD" not in os.environ
    assert not _state_file().exists()
    assert len(said) == 1 and "틀렸" in said[0]
    assert all("wrong-pw-value" not in m for m in said)


@pytest.mark.parametrize("exc", [PhoneCodeInvalidError("PHONE_CODE_INVALID"),
                                 RuntimeError("boom")])
def test_sign_in_failed_and_exception(env, monkeypatch, exc):
    _sign_in_env(monkeypatch, password="right-pw-value")
    FakeClient.code_exc = exc
    action, _ = _run()
    assert action == "sign_in_failed"
    assert _deleted(env) == {"TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"}
    assert not _state_file().exists()
    assert "TG_LOGIN_PHONE" in os.environ
    assert env.upserts == []


def test_upsert_failure_logs_out_session(env, monkeypatch):
    _sign_in_env(monkeypatch)
    env.upsert_ok = False
    action, said = _run()
    assert action == "upsert_failed"
    assert not _state_file().exists()
    assert FakeClient.instances[-1].logged_out
    assert FakeClient.instances[-1].session.s == STATE_SESSION
    assert all(STATE_SESSION not in m for m in said)


def test_failed_delete_keeps_process_env(env, monkeypatch):
    _sign_in_env(monkeypatch)
    env.delete_ok = False
    FakeClient.code_exc = PhoneCodeInvalidError("PHONE_CODE_INVALID")
    _run()
    assert ("TG_LOGIN_CODE", [OWN_SID]) in env.deletes
    assert os.environ.get("TG_LOGIN_CODE") == "12345"  # Railway 삭제 실패 → pop 안 함


@pytest.mark.parametrize("setup", ["send_code", "sign_in", "wait"])
def test_railway_unavailable_does_nothing(env, monkeypatch, setup):
    env._available = False
    if setup != "send_code":
        _write_state()
    if setup == "sign_in":
        monkeypatch.setenv("TG_LOGIN_CODE", "12345")
    before = _state_file().read_text() if _state_file().exists() else None
    action, said = _run()
    assert action == "railway_unavailable"
    assert FakeClient.instances == []  # 사용자 폰에 코드 요청 없음
    assert env.deletes == [] and env.upserts == []
    assert said == [tg_login.RAILWAY_UNAVAILABLE_MSG]
    after = _state_file().read_text() if _state_file().exists() else None
    assert before == after


def test_missing_own_service_id_counts_as_unavailable(env, monkeypatch):
    monkeypatch.delenv("RAILWAY_SERVICE_ID")
    action, said = _run()
    assert action == "railway_unavailable"
    assert FakeClient.instances == []
    assert env.upserts == [] and env.deletes == []


def test_cleanup_deletes_leftovers_from_own_service(env, monkeypatch):
    monkeypatch.setenv("TG_SESSION_STRING", "already-have-session")
    monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
    _write_state()
    action, said = _run()
    assert action == "cleanup"
    assert _deleted(env) == {"TG_LOGIN_PHONE", "TG_LOGIN_PASSWORD"}
    assert not _state_file().exists()
    assert said == []


def test_cleanup_without_railway_unlinks_and_tells_names_only(env, monkeypatch):
    env._available = False
    monkeypatch.setenv("TG_SESSION_STRING", "already-have-session")
    monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
    _write_state()
    action, said = _run()
    assert action == "cleanup"
    assert env.deletes == []
    assert not _state_file().exists()
    assert len(said) == 1 and "TG_LOGIN_PASSWORD" in said[0]
    assert "right-pw-value" not in said[0]


def test_say_is_redacted(env, monkeypatch):
    FakeClient.send_code_exc = RuntimeError("bad phone +821012345678")
    action, said = _run()
    assert action == "send_code_failed"
    assert said and "+821012345678" not in said[0]


def test_session_upsert_refuses_fan_out_on_legacy_railway_env(env, monkeypatch):
    calls = []

    def legacy_upsert(name, value):  # service_ids 미지원 구버전
        calls.append(name)
        return True, "ok"

    ok, msg = tg_login._railway_call(legacy_upsert, "TG_SESSION_STRING", "v",
                                     allow_fan_out=False)
    assert not ok and calls == []
    # 대상이 자기 서비스뿐이면 구버전이라도 허용
    monkeypatch.setenv("RAILWAY_SERVICE_IDS", OWN_SID)
    ok, _ = tg_login._railway_call(legacy_upsert, "TG_SESSION_STRING", "v",
                                   allow_fan_out=False)
    assert ok and calls == ["TG_SESSION_STRING"]
