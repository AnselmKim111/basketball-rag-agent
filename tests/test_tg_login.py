"""tg_login.decide — env 상태 머신 검증."""
from __future__ import annotations

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
