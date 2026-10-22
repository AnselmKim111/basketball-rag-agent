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
from src.model_router import railway_env as _railway_env_mod

# env 픽스처가 가짜로 덮기 전 원본 (실제 probe 경로 검증용)
_REAL_AVAILABLE = _railway_env_mod.available
_REAL_PROBE = _railway_env_mod.probe
_REAL_TOKEN_VALID = _railway_env_mod.token_valid

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
    def __init__(self, available=True, delete_ok=True, upsert_ok=True, token_ok=True):
        self._available = available
        self.delete_ok = delete_ok
        self.upsert_ok = upsert_ok
        self.token_ok = token_ok  # False = 토큰은 있지만 폐기됨 (인증 probe 'invalid')
        self.probe_state: str | None = None  # 지정 시 probe() 반환값 우선 ('unknown' = 일시 장애)
        self.token_checks = 0
        self.deletes: list[tuple[str, list | None]] = []
        self.upserts: list[tuple[str, str, list | None]] = []

    def available(self):
        return self._available

    def probe(self, force=False):
        self.token_checks += 1
        if self.probe_state is not None:
            return self.probe_state
        return "ok" if self.token_ok else "invalid"

    def token_valid(self, force=False):
        return self.probe(force) == "ok"

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
    for attr in ("available", "probe", "token_valid", "delete_variable", "upsert_variable"):
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
    assert all("직접 삭제" not in m for m in said)  # 삭제 다 성공 → 수동 안내 없음
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


@pytest.mark.parametrize("why", ["no_token", "revoked_token"])
@pytest.mark.parametrize("setup", ["send_code", "sign_in", "wait"])
def test_railway_unavailable_does_nothing(env, monkeypatch, setup, why):
    """토큰 없음 + 토큰은 있지만 폐기됨(인증 probe 실패) 둘 다 로그인 흐름 미시작."""
    if why == "no_token":
        env._available = False
    else:
        env.token_ok = False
    monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
    if setup != "send_code":
        _write_state()
    if setup == "sign_in":
        monkeypatch.setenv("TG_LOGIN_CODE", "12345")
    before = _state_file().read_text() if _state_file().exists() else None
    action, said = _run()
    assert action == "railway_unavailable"
    assert FakeClient.instances == []  # 사용자 폰에 코드 요청 없음
    assert env.deletes == [] and env.upserts == []
    assert len(said) == 1 and said[0].startswith(tg_login.RAILWAY_UNAVAILABLE_MSG)
    # 남은 TG_LOGIN_*는 이름으로 수동 삭제 안내 (값은 절대 미포함)
    expected = ["TG_LOGIN_PHONE", "TG_LOGIN_PASSWORD"] + (["TG_LOGIN_CODE"] if setup == "sign_in" else [])
    assert "직접 삭제 필요" in said[0]
    for n in expected:
        assert n in said[0]
    for v in ("right-pw-value", "+821012345678", "12345"):
        assert v not in said[0]
    after = _state_file().read_text() if _state_file().exists() else None
    assert before == after


def test_revoked_token_probe_skipped_when_config_missing(env):
    """설정 자체가 없으면 인증 probe(네트워크)도 안 함."""
    env._available = False
    assert tg_login._railway_ready() is False
    assert env.token_checks == 0
    env._available = True
    assert tg_login._railway_ready() is True
    assert env.token_checks == 1


def test_railway_ready_uses_real_token_probe(monkeypatch):
    """가짜 대신 실제 railway_env.token_valid — 폐기 토큰(GraphQL Not Authorized)이면 False."""
    from types import SimpleNamespace

    from src.model_router import railway_env

    railway_env.reset_token_cache()
    monkeypatch.delenv("RAILWAY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_IDS", raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", "tok-revoked-fake-0000")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid-fake")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid-fake")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", OWN_SID)
    posts = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"errors": [{"message": "Not Authorized"}], "data": None}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            posts.append(json)
            return _Resp()

    monkeypatch.setattr(railway_env, "httpx", SimpleNamespace(Client=_Client))
    try:
        assert railway_env.available() is True  # 설정 존재만 보는 의미는 그대로
        assert tg_login._railway_ready() is False
        assert tg_login._railway_ready() is False
        assert len(posts) == 1  # 프로세스 캐시 — probe 1회
    finally:
        railway_env.reset_token_cache()


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


# ----------------------------------------------------------------------
# Railway env 삭제 실패(토큰 폐기 등) → 종료 메시지에 남은 env 이름 수동 삭제 안내
# ----------------------------------------------------------------------
SECRET_VALUES = ("right-pw-value", "wrong-pw-value", "12345", "99999", "+821012345678",
                 STATE_SESSION)


def _assert_manual_note(said, names, caplog):
    assert said, "알림 없음"
    last = said[-1]
    assert "Railway Variables에서 직접 삭제 필요" in last
    for n in names:
        assert n in last
    for m in said:
        for v in SECRET_VALUES:
            assert v not in m
    warned = [r for r in caplog.records
              if r.levelno == logging.WARNING and "삭제 실패" in r.getMessage()]
    assert {n for n in names if any(n in r.getMessage() for r in warned)} == set(names)


@pytest.mark.parametrize("scenario,expected_action,names", [
    ("send_code_failed", "send_code_failed", ["TG_LOGIN_PASSWORD"]),
    ("send_code_stale", "send_code", ["TG_LOGIN_CODE"]),
    ("code_expired_resent", "code_expired_resent", ["TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"]),
    ("code_expired", "code_expired", ["TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"]),
    ("password_needed", "password_needed", ["TG_LOGIN_CODE"]),
    ("password_wrong", "password_wrong", ["TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"]),
    ("sign_in_failed", "sign_in_failed", ["TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"]),
    ("upsert_failed", "upsert_failed",
     ["TG_LOGIN_PHONE", "TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"]),
    ("signed_in", "signed_in", ["TG_LOGIN_PHONE", "TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"]),
])
def test_failed_deletes_are_named_in_terminal_message(env, monkeypatch, caplog,
                                                      scenario, expected_action, names):
    env.delete_ok = False
    caplog.set_level(logging.INFO, logger="src.tg_login")
    if scenario == "send_code_failed":
        monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
        FakeClient.send_code_exc = RuntimeError("flood")
    elif scenario == "send_code_stale":
        monkeypatch.setenv("TG_LOGIN_CODE", "99999")
    elif scenario in ("code_expired_resent", "code_expired"):
        _sign_in_env(monkeypatch, password="right-pw-value")
        FakeClient.code_exc = PhoneCodeExpiredError("PHONE_CODE_EXPIRED")
        if scenario == "code_expired":
            FakeClient.send_code_exc = RuntimeError("flood")
    elif scenario == "password_needed":
        _sign_in_env(monkeypatch)
        FakeClient.needs_password = True
    elif scenario == "password_wrong":
        _sign_in_env(monkeypatch, password="wrong-pw-value")
        FakeClient.needs_password = True
    elif scenario == "sign_in_failed":
        _sign_in_env(monkeypatch, password="right-pw-value")
        FakeClient.code_exc = PhoneCodeInvalidError("PHONE_CODE_INVALID")
    elif scenario == "upsert_failed":
        _sign_in_env(monkeypatch, password="right-pw-value")
        env.upsert_ok = False
    else:  # signed_in
        _sign_in_env(monkeypatch, password="right-pw-value")
    action, said = _run()
    assert action == expected_action
    _assert_manual_note(said, names, caplog)
    for n in names:  # 삭제 실패 → 프로세스 env도 유지 (다음 부팅 재시도 근거)
        assert os.environ.get(n)


def test_upsert_failed_with_revoked_token_names_password(env, monkeypatch, caplog):
    """사고 재현: 토큰 폐기(삭제·upsert 모두 401) → 세션 로그아웃 + TG_LOGIN_PASSWORD 수동 삭제 안내."""
    env.delete_ok = False
    env.upsert_ok = False
    caplog.set_level(logging.INFO, logger="src.tg_login")
    _sign_in_env(monkeypatch, password="right-pw-value")
    FakeClient.needs_password = True
    action, said = _run()
    assert action == "upsert_failed"
    assert FakeClient.instances[-1].logged_out
    assert len(said) == 1
    assert "TG_LOGIN_PASSWORD" in said[0] and "직접 삭제" in said[0]
    assert "right-pw-value" not in said[0]


def test_partial_delete_failure_names_only_failed(env, monkeypatch, caplog):
    calls = []

    def delete_variable(name, service_ids=None):
        calls.append(name)
        if name == "TG_LOGIN_PASSWORD":
            return False, "0/1 삭제 — sid=svc-own-: HTTP 401 Not Authorized"
        return True, "✅ 1개 서비스 삭제"

    from src.model_router import railway_env
    monkeypatch.setattr(railway_env, "delete_variable", delete_variable)
    caplog.set_level(logging.INFO, logger="src.tg_login")
    _sign_in_env(monkeypatch, password="right-pw-value")
    FakeClient.code_exc = PhoneCodeInvalidError("PHONE_CODE_INVALID")
    action, said = _run()
    assert action == "sign_in_failed"
    assert set(calls) == {"TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"}
    assert said[-1].endswith("직접 삭제 필요: TG_LOGIN_PASSWORD")
    assert "TG_LOGIN_CODE" not in os.environ and os.environ.get("TG_LOGIN_PASSWORD")


def test_cleanup_with_failed_deletes_tells_names(env, monkeypatch):
    env.delete_ok = False
    monkeypatch.setenv("TG_SESSION_STRING", "already-have-session")
    monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
    action, said = _run()
    assert action == "cleanup"
    assert len(said) == 1
    assert "TG_LOGIN_PHONE" in said[0] and "TG_LOGIN_PASSWORD" in said[0]
    assert "직접 삭제" in said[0]
    assert "right-pw-value" not in said[0] and "already-have-session" not in said[0]


def test_manual_note_names_only():
    assert tg_login._manual_note([]) == ""
    note = tg_login._manual_note(["TG_LOGIN_CODE", "TG_LOGIN_PASSWORD"])
    assert note == " ⚠ Railway Variables에서 직접 삭제 필요: TG_LOGIN_CODE, TG_LOGIN_PASSWORD"
    # 실제 알림 경로(redact) 통과 후에도 이름이 남아야 안내가 됨
    from src.log_redact import redact
    assert "TG_LOGIN_PASSWORD" in redact(note)


# ---------------------------------------------------------------- probe 'unknown' (일시 장애)


def _login_envs():
    return {n: os.environ.get(n) for n in tg_login.LOGIN_ENVS}


@pytest.mark.parametrize("setup", ["send_code", "sign_in", "wait"])
def test_railway_probe_unknown_holds_flow_without_manual_note(env, monkeypatch, setup):
    """probe 'unknown'(네트워크 예외·5xx·429) — 무효로 단정하지 않음: 일시 장애 안내만,
    수동 삭제 안내 없음, env·상태파일 그대로 (재배포 시 같은 단계부터 재개)."""
    env.probe_state = "unknown"
    monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
    if setup != "send_code":
        _write_state()
    if setup == "sign_in":
        monkeypatch.setenv("TG_LOGIN_CODE", "12345")
    before_state = _state_file().read_text() if _state_file().exists() else None
    before_env = _login_envs()
    action, said = _run()
    assert action == "railway_unknown"
    assert said == [tg_login.RAILWAY_TRANSIENT_MSG]
    assert said[0] == "⚠ Railway API 일시 장애 — 로그인 흐름 보류, 재배포로 재시도"
    assert "직접 삭제" not in said[0] and "TG_LOGIN_" not in said[0]
    assert not said[0].startswith(tg_login.RAILWAY_UNAVAILABLE_MSG)
    assert FakeClient.instances == []  # 사용자 폰에 코드 요청 없음
    assert env.deletes == [] and env.upserts == []
    assert env.token_checks == 1
    after_state = _state_file().read_text() if _state_file().exists() else None
    assert before_state == after_state
    assert _login_envs() == before_env


def test_railway_probe_unknown_then_ok_resumes_on_redeploy(env):
    """일시 장애 부팅 후 다음 부팅(재배포)에 probe 'ok' → 정상적으로 코드 요청."""
    env.probe_state = "unknown"
    assert _run()[0] == "railway_unknown"
    env.probe_state = None  # 장애 해소
    action, said = _run()
    assert action == "send_code"
    assert len(FakeClient.instances) == 1
    assert _state_file().exists()


def test_railway_state_exception_is_unknown(env, monkeypatch):
    """확인 중 예외 — 판정 불가 → 'unknown' (무효로 단정하지 않음)."""
    def boom(force=False):
        raise RuntimeError("unexpected")

    from src.model_router import railway_env
    monkeypatch.setattr(railway_env, "probe", boom)
    assert tg_login._railway_state() == "unknown"
    assert tg_login._railway_ready() is False
    action, said = _run()
    assert action == "railway_unknown"
    assert said == [tg_login.RAILWAY_TRANSIENT_MSG]


def test_railway_state_values(env, monkeypatch):
    assert tg_login._railway_state() == "ok"
    env.token_ok = False
    assert tg_login._railway_state() == "invalid"
    env.probe_state = "unknown"
    assert tg_login._railway_state() == "unknown"
    env.probe_state = "weird"  # 알 수 없는 값은 확정 판정으로 취급하지 않음
    assert tg_login._railway_state() == "unknown"
    env.probe_state = None
    env._available = False
    assert tg_login._railway_state() == "invalid"
    monkeypatch.delenv("RAILWAY_SERVICE_ID")
    env._available = True
    assert tg_login._railway_state() == "invalid"


def test_cleanup_with_probe_unknown_does_not_claim_invalid(env, monkeypatch):
    """세션 있음 + 잔여 env + 일시 장애 — 삭제 시도 없음, '무효' 단정 없이 재배포 재시도 안내."""
    env.probe_state = "unknown"
    monkeypatch.setenv("TG_SESSION_STRING", "already-have-session")
    monkeypatch.setenv("TG_LOGIN_PASSWORD", "right-pw-value")
    _write_state()
    action, said = _run()
    assert action == "cleanup"
    assert env.deletes == []
    assert not _state_file().exists()  # 상태파일(auth key)은 로컬이라 항상 삭제
    assert len(said) == 1
    assert "일시 장애" in said[0] and "재배포" in said[0]
    assert "토큰 없음/무효" not in said[0]
    assert "TG_LOGIN_PHONE" in said[0] and "TG_LOGIN_PASSWORD" in said[0]
    assert "right-pw-value" not in said[0] and "already-have-session" not in said[0]


class _NetErr(Exception):
    """httpx.ConnectError 상당 (conftest가 httpx를 더미로 바꿔 둠)."""


def _real_probe_env(env, monkeypatch, outcomes: list):
    """env 픽스처의 가짜 available/probe/token_valid를 실제 railway_env로 되돌리고,
    probe HTTP만 가짜로 — outcomes: 예외 인스턴스(raise) 또는 (status, json) 순서대로."""
    from types import SimpleNamespace

    from src.model_router import railway_env

    monkeypatch.setattr(railway_env, "available", _REAL_AVAILABLE)
    monkeypatch.setattr(railway_env, "probe", _REAL_PROBE)
    monkeypatch.setattr(railway_env, "token_valid", _REAL_TOKEN_VALID)
    monkeypatch.delenv("RAILWAY_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", "tok-fake-railway-5555")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid-fake")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid-fake")
    posts: list = []

    class _Resp:
        def __init__(self, status, data):
            self.status_code = status
            self._data = data

        def json(self):
            return self._data

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            posts.append(json)
            o = outcomes[min(len(posts) - 1, len(outcomes) - 1)]
            if isinstance(o, BaseException):
                raise o
            return _Resp(*o)

    monkeypatch.setattr(railway_env, "httpx", SimpleNamespace(Client=_Client))
    railway_env.reset_token_cache()
    return posts


def test_real_probe_network_error_is_transient_notice(env, monkeypatch, caplog):
    """실제 railway_env.probe — 네트워크 예외면 '일시 장애' 안내 (예전: '토큰 없음/무효' +
    TG_LOGIN_PHONE 수동 삭제 안내 → 사용자가 흐름을 버리게 됨). 캐시 안 해 재부팅마다 재확인,
    장애 해소 후 부팅에선 정상 진행."""
    from src.model_router import railway_env

    caplog.set_level(logging.DEBUG)
    posts = _real_probe_env(env, monkeypatch, [
        _NetErr("connect failed tok-fake-railway-5555"),
        _NetErr("connect failed"),
        (200, {"data": {"projectToken": {"projectId": "pid-fake", "environmentId": "eid"}}}),
    ])
    try:
        for _ in range(2):
            action, said = _run()
            assert action == "railway_unknown"
            assert said == [tg_login.RAILWAY_TRANSIENT_MSG]
            assert "직접 삭제" not in said[0]
        assert len(posts) == 2  # 'unknown'은 캐시 안 함 → 부팅마다 재확인
        assert FakeClient.instances == []
        assert env.deletes == [] and env.upserts == []
        assert os.environ.get("TG_LOGIN_PHONE") == "+821012345678"
        assert "tok-fake-railway-5555" not in caplog.text
        # 장애 해소 → 정상 진행 (코드 요청)
        action, said = _run()
        assert action == "send_code"
        assert len(FakeClient.instances) == 1
        assert len(posts) == 3
    finally:
        railway_env.reset_token_cache()


@pytest.mark.parametrize("outcome,expected", [
    ((502, {}), "railway_unknown"),
    ((429, {}), "railway_unknown"),
    ((401, {"errors": [{"message": "Not Authorized"}]}), "railway_unavailable"),
    ((200, {"errors": [{"message": "Not Authorized"}], "data": None}), "railway_unavailable"),
], ids=["502", "429", "401", "200-not-authorized"])
def test_real_probe_status_maps_to_notice(env, monkeypatch, outcome, expected):
    from src.model_router import railway_env

    _real_probe_env(env, monkeypatch, [outcome])
    try:
        action, said = _run()
        assert action == expected
        assert len(said) == 1
        if expected == "railway_unknown":
            assert said == [tg_login.RAILWAY_TRANSIENT_MSG]
        else:  # 폐기 토큰 — 기존 문구 + 수동 삭제 안내 유지
            assert said[0].startswith(tg_login.RAILWAY_UNAVAILABLE_MSG)
            assert "직접 삭제 필요" in said[0] and "TG_LOGIN_PHONE" in said[0]
        assert FakeClient.instances == []
    finally:
        railway_env.reset_token_cache()
