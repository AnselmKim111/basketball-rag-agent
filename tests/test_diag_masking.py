"""부팅 diag의 이름별 마스킹 — 리뷰 security/SEC-4.

값 길이(16자)만 보고 fingerprint(무솔트 sha256 8자리)를 찍으면 사람이 고른 16자+ 비번
(WISEREPORT_PW 등)도 `<set len=N fp=...>`가 부팅마다 Railway 로그에 남아, 로그 열람자가
사전 대입으로 비번을 확인할 수 있다. → fp는 무작위 자격증명 이름(KEY/TOKEN/SECRET/SESSION/HASH)
에만, 비밀번호·계정 ID류는 `<set>`(길이도 생략), 미분류 이름은 `<set len=N>`.
모든 값은 가짜.
"""
from __future__ import annotations

import hashlib

import pytest

from src.bot_helpers import _diag_masked, diag_env_keys

LONG_PW = "summer2024summer!"  # 가짜 — 사람이 고른 16자+ 비번 형태
LONG_RANDOM = "sk-or-v1-" + "c3" * 24


def _fp(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()[:8]


@pytest.mark.parametrize("name", [
    "WISEREPORT_PW", "TG_LOGIN_PASSWORD", "SOME_PASSWD", "DB_PASS", "TG_PHONE",
    "TG_OTP", "TG_LOGIN_CODE", "WISEREPORT_ID", "TELEGRAM_API_ID", "SOME_USER_IDS",
    # 이름에 KEY/TOKEN이 섞여도 비밀번호류 marker가 이기면 fp 없음
    "WISEREPORT_PW_KEY", "TOKEN_PASSWORD",
])
@pytest.mark.parametrize("value", [LONG_PW, "short-pw1"])
def test_password_class_names_print_set_only(name, value):
    assert _diag_masked(name, value) == "<set>"


@pytest.mark.parametrize("name", [
    "OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "SCREENER_BOT_TOKEN",
    "RAILWAY_PROJECT_ACCESS_TOKEN", "SOME_CLIENT_SECRET", "TG_SESSION_STRING",
    "TELEGRAM_API_HASH",
])
def test_random_credential_names_keep_fp(name):
    assert _diag_masked(name, LONG_RANDOM) == (
        f"<set len={len(LONG_RANDOM)} fp={_fp(LONG_RANDOM)}>"
    )


def test_short_credential_value_has_no_fp():
    v = "FAKEAV012345"  # 16자 미만 — fp 전수 대입 방지
    assert _diag_masked("ALPHA_VANTAGE_KEY", v) == f"<set len={len(v)}>"


@pytest.mark.parametrize("name", ["WISE_FOO", "SOME_NEW_THING", "SCREENER_MIN_MARKET_CAP"])
def test_unknown_names_len_only_no_fp(name):
    assert _diag_masked(name, LONG_PW) == f"<set len={len(LONG_PW)}>"


def test_diag_env_keys_long_password_has_no_len_or_fp(monkeypatch):
    """회귀: 17자 WISEREPORT_PW가 `<set len=17 fp=...>`로 찍혀 사전 대입 확인 가능했음."""
    monkeypatch.setenv("WISEREPORT_PW", LONG_PW)
    monkeypatch.setenv("WISEREPORT_ID", "fakewiseuser0001")
    monkeypatch.setenv("WISE_FOO", LONG_PW)
    monkeypatch.setenv("OPENROUTER_API_KEY", LONG_RANDOM)
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    diag = diag_env_keys(("WISE", "OPEN"))
    assert diag["WISEREPORT_PW"] == "<set>"
    assert diag["WISEREPORT_ID"] == "<set>"
    assert diag["WISE_FOO"] == f"<set len={len(LONG_PW)}>"
    assert diag["OPENROUTER_API_KEY"] == f"<set len={len(LONG_RANDOM)} fp={_fp(LONG_RANDOM)}>"
    assert diag["OPENROUTER_MODEL"] == "deepseek/deepseek-v4-flash"  # allowlist 원문 유지
    text = repr(diag)
    assert LONG_PW not in text and LONG_RANDOM not in text
    assert _fp(LONG_PW) not in text, "비번 fingerprint는 어디에도 찍히면 안 됨"


def test_plain_allowlist_id_names_unaffected(monkeypatch):
    """*_ID 마스킹은 allowlist 밖만 — Railway id·chat id는 원문 그대로."""
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc-0000-1111")
    monkeypatch.setenv("SCREENER_CHAT_ID", "-1001234567890")
    diag = diag_env_keys(("RAILWAY_SERVICE", "SCREENER_CHAT"))
    assert diag["RAILWAY_SERVICE_ID"] == "svc-0000-1111"
    assert diag["SCREENER_CHAT_ID"] == "-1001234567890"
