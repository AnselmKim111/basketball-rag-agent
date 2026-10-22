"""부팅 자격증명 점검(src/cred_check.py) — 2026-10-21 Railway env 유출 후 키 전면 교체 검증용.

HTTP는 전부 가짜(_http_get monkeypatch). 메시지엔 env 이름·✓/✗/–/⚠·fingerprint만,
가짜 키 값은 메시지·로그 어디에도 나오면 안 됨.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types

import pytest

from src import cred_check as cc

OR_KEY = "sk-or-v1-" + "0f" * 32
OPENAI_KEY = "sk-proj-FakeOpenAiKeyFakeOpenAiKey1234567890"
DART_KEY = "fakedartkey0123456789abcdef0123456789abcd"
FINNHUB_KEY = "fakefinnhubkey0123456789"
FMP_KEY = "fakefmpkey0123456789abcdef"
AV_KEY = "FAKEALPHAVANTAGE01"
TG_SESSION = "1" + "A" * 350
RAILWAY_TOKEN = "fake-railway-token-0123456789abcdef"
CHART_KR = "333333333:AAFakeChartKrTokenFakeChartKrToken123"
CHART_US = "444444444:AAFakeChartUsTokenFakeChartUsToken456"
BOT_GOOD = "222222222:AAFakeLiveTokenFakeLiveTokenFakeLive999"
BOT_IDLE = "555555555:AAFakeIdleTokenFakeIdleTokenFakeIdle777"
MGMT_KEY = "sk-or-v1-" + "9e" * 32

ALL_VALUES = (OR_KEY, OPENAI_KEY, DART_KEY, FINNHUB_KEY, FMP_KEY, AV_KEY, TG_SESSION,
              RAILWAY_TOKEN, CHART_KR, CHART_US, BOT_GOOD, BOT_IDLE, MGMT_KEY)

_MANAGED = (
    "SCREENER_CHART_BOT_TOKEN", "US_SCREENER_CHART_BOT_TOKEN", "OPENROUTER_API_KEY",
    "OPENAI_API_KEY", "DART_API_KEY", "FINNHUB_API_KEY", "FMP_API_KEY", "ALPHA_VANTAGE_KEY",
    "TG_SESSION_STRING", "OPENROUTER_MANAGEMENT_KEY", "TELEGRAM_BOT_TOKEN",
    "SCREENER_BOT_TOKEN", "IDEA_BOT_TOKEN", "EARNINGS_BOT_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in _MANAGED:
        monkeypatch.delenv(k, raising=False)
    for k in list(os.environ):
        if "RAILWAY" in k.upper():
            monkeypatch.delenv(k, raising=False)


class FakeBot:
    def __init__(self, token, username):
        self.token = token
        self.username = username


def _route(calls, delay=0.0):
    async def fake_get(url, *, headers=None, params=None, timeout=cc.PROBE_TIMEOUT_S):
        calls.append((url, dict(headers or {}), dict(params or {})))
        if delay:
            await asyncio.sleep(delay)
        if "api.telegram.org" in url:
            if CHART_KR in url:
                return 200, {"ok": True, "result": {"username": "kr_chart_bot"}}
            return 401, {"ok": False, "description": "Unauthorized"}
        if "openrouter.ai/api/v1/key" in url:
            assert headers == {"Authorization": f"Bearer {OR_KEY}"}
            return 200, {"data": {"label": "prod-key", "limit": None, "usage": 1.5}}
        if "api.openai.com" in url:
            return 401, {"error": {"message": f"Incorrect API key provided: {OPENAI_KEY}"}}
        if "opendart.fss.or.kr" in url:
            assert params["crtfc_key"] == DART_KEY and params["corp_code"] == cc.DART_PROBE_CORP_CODE
            return 200, {"status": "010", "message": "등록되지 않은 키입니다."}
        if "finnhub.io" in url:
            assert headers == {"X-Finnhub-Token": FINNHUB_KEY}  # 키는 헤더로만
            assert FINNHUB_KEY not in url and FINNHUB_KEY not in str(params)
            return 200, {"c": 230.1}
        if "financialmodelingprep.com" in url:
            raise OSError(f"connect failed apikey={FMP_KEY}")
        raise AssertionError(f"예상 밖 호출: {url.split('?')[0][:30]}")
    return fake_get


def _set_all(monkeypatch):
    env = {
        "SCREENER_CHART_BOT_TOKEN": CHART_KR, "US_SCREENER_CHART_BOT_TOKEN": CHART_US,
        "OPENROUTER_API_KEY": OR_KEY, "OPENAI_API_KEY": OPENAI_KEY, "DART_API_KEY": DART_KEY,
        "FINNHUB_API_KEY": FINNHUB_KEY, "FMP_API_KEY": FMP_KEY, "ALPHA_VANTAGE_KEY": AV_KEY,
        "TG_SESSION_STRING": TG_SESSION, "RAILWAY_API_TOKEN": RAILWAY_TOKEN,
        "RAILWAY_PROJECT_ID": "proj-1234-not-secret", "OPENROUTER_MANAGEMENT_KEY": MGMT_KEY,
        "SCREENER_BOT_TOKEN": BOT_GOOD, "TELEGRAM_BOT_TOKEN": "111111111:AAFakeRevokedTokenFakeRevokedToken12345",
        "IDEA_BOT_TOKEN": BOT_IDLE,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)


BOT_ENVS = {"company": "TELEGRAM_BOT_TOKEN", "screener": "SCREENER_BOT_TOKEN",
            "idea": "IDEA_BOT_TOKEN", "earnings": "EARNINGS_BOT_TOKEN"}


def _line(text, name):
    return next(ln for ln in text.splitlines() if f" {name}" in ln and ln[:1] in "✓✗–⚠")


def test_full_report_names_marks_no_values(monkeypatch, caplog):
    _set_all(monkeypatch)
    calls: list = []
    monkeypatch.setattr(cc, "_http_get", _route(calls, delay=0.2))

    t0 = time.monotonic()
    text = asyncio.run(cc.run_credential_check(
        {"screener": FakeBot(BOT_GOOD, "kr_screener_bot")}, ["company"],
        bot_token_envs=BOT_ENVS, fail_reasons={"company": "토큰 거부"}))
    elapsed = time.monotonic() - t0

    assert text.startswith("🔐 자격증명 점검")
    # 병렬 — 프로브 7개 × 0.2s 직렬이면 1.4s
    assert len(calls) == 7
    assert elapsed < 0.8, elapsed

    # 텔레그램 봇 — 기동 결과 재사용 (getMe 재호출 없음)
    assert _line(text, "TELEGRAM_BOT_TOKEN").startswith("✗")
    assert "company봇 시작 실패 — 토큰 거부" in text
    good = _line(text, "SCREENER_BOT_TOKEN")
    assert good.startswith("✓") and "@kr_screener_bot" in good and f"fp={cc._fp(BOT_GOOD)}" in good
    idle = _line(text, "IDEA_BOT_TOKEN")
    assert idle.startswith("–") and "미기동" in idle
    assert "EARNINGS_BOT_TOKEN" not in text  # 미설정 봇은 생략
    assert not any(BOT_GOOD in u or BOT_IDLE in u for u, _, _ in calls)  # getMe 중복 호출 없음

    # 차트 봇 getMe
    kr = _line(text, "SCREENER_CHART_BOT_TOKEN")
    assert kr.startswith("✓") and "@kr_chart_bot" in kr and f"fp={cc._fp(CHART_KR)}" in kr
    assert _line(text, "US_SCREENER_CHART_BOT_TOKEN") == "✗ US_SCREENER_CHART_BOT_TOKEN — 토큰 거부 (HTTP 401)"

    # API 키
    orl = _line(text, "OPENROUTER_API_KEY")
    assert orl.startswith("✓") and "label=prod-key" in orl and "한도 없음 ⚠" in orl
    assert f"fp={cc._fp(OR_KEY)}" in orl
    assert _line(text, "OPENAI_API_KEY") == "✗ OPENAI_API_KEY — 키 거부 (HTTP 401)"
    assert _line(text, "DART_API_KEY") == "✗ DART_API_KEY — status 010"
    assert "등록되지 않은" not in text  # 상태 메시지는 생략, 코드만
    assert _line(text, "FINNHUB_API_KEY").startswith("✓")
    assert _line(text, "FMP_API_KEY") == "✗ FMP_API_KEY — 연결 실패 (OSError)"

    # 호출 안 하는 것들 — 존재만
    assert _line(text, "ALPHA_VANTAGE_KEY").startswith("–")
    assert not any("alphavantage" in u for u, _, _ in calls)
    assert _line(text, "TG_SESSION_STRING") == "– TG_SESSION_STRING — 설정됨 — 채널릴레이가 검증"
    assert _line(text, "OPENROUTER_MANAGEMENT_KEY").startswith("–")
    assert _line(text, "RAILWAY_API_TOKEN") == (
        "⚠ RAILWAY_API_TOKEN — 런타임에 Railway 토큰 존재 (유출 시 전체 env 장악 — 불필요하면 삭제 권장)")
    assert "RAILWAY_PROJECT_ID" not in text

    # 값은 어디에도 없음 (메시지·로그)
    for v in ALL_VALUES:
        assert v not in text
        assert v not in caplog.text
    assert "proj-1234-not-secret" not in text


def test_unset_keys_reported_without_network(monkeypatch):
    calls: list = []
    monkeypatch.setattr(cc, "_http_get", _route(calls))
    text = asyncio.run(cc.run_credential_check({}, []))
    assert calls == []
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "DART_API_KEY",
                 "FINNHUB_API_KEY", "FMP_API_KEY"):
        assert f"– {name} — 미설정" in text
    assert "[텔레그램 봇]" not in text
    assert "⚠" not in text


def test_probe_timeout_marks_fail(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
    monkeypatch.setattr(cc, "PROBE_TIMEOUT_S", 0.05)

    async def slow(url, **kw):
        await asyncio.sleep(2)
        return 200, {}

    monkeypatch.setattr(cc, "_http_get", slow)
    t0 = time.monotonic()
    text = asyncio.run(cc.run_credential_check({}, []))
    assert time.monotonic() - t0 < 1.0
    assert _line(text, "OPENROUTER_API_KEY").startswith("✗ OPENROUTER_API_KEY — 시간 초과")


def test_openrouter_limit_and_dart_ok_and_rate_limit(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
    monkeypatch.setenv("DART_API_KEY", DART_KEY)
    monkeypatch.setenv("FINNHUB_API_KEY", FINNHUB_KEY)

    async def fake(url, *, headers=None, params=None, timeout=10.0):
        if "openrouter" in url:
            return 200, {"data": {"label": "x", "limit": 10, "limit_remaining": 2.5}}
        if "opendart" in url:
            return 200, {"status": "000", "corp_name": "삼성전자"}
        return 429, None

    monkeypatch.setattr(cc, "_http_get", fake)
    text = asyncio.run(cc.run_credential_check({}, []))
    orl = _line(text, "OPENROUTER_API_KEY")
    assert "한도 $10.00 (잔여 $2.50)" in orl and "한도 없음" not in orl
    assert _line(text, "DART_API_KEY") == f"✓ DART_API_KEY — fp={cc._fp(DART_KEY)}"
    assert _line(text, "FINNHUB_API_KEY").startswith("– FINNHUB_API_KEY — 요청 한도 초과")


def test_never_raises(monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError(f"boom {OR_KEY}")

    monkeypatch.setattr(cc, "_build", broken)
    text = asyncio.run(cc.run_credential_check({}, []))
    assert text.startswith("🔐 자격증명 점검") and "RuntimeError" in text
    assert OR_KEY not in text


def test_http_get_no_redirect_and_timeout(monkeypatch):
    """실제 _http_get — 리다이렉트 미추종(키 붙은 요청이 다른 호스트로 새지 않게) + timeout."""
    seen: dict = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class Client:
        def __init__(self, **kw):
            seen["client"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            seen["get"] = (url, headers, params)
            return Resp()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=Client))
    status, body = asyncio.run(cc._http_get("https://example.invalid/x", headers={"A": "b"}))
    assert (status, body) == (200, {"ok": True})
    assert seen["client"]["follow_redirects"] is False
    assert seen["client"]["timeout"] == cc.PROBE_TIMEOUT_S
