"""summarizer 401 인증 breaker — 2026-10-21 OpenRouter 키 disable 사고 재발 방지.

키가 죽으면(401 'User not found.') 재시도·fallback·health 기록 없이 즉시
OpenRouterAuthError, 이후 cooldown 동안 네트워크 호출 0, 관리자 알림은 장애당 1회.
OpenAI client는 mock — 네트워크 미사용.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from src import summarizer as sm


class _StatusErr(sm.APIStatusError):
    """실제 openai/conftest 더미 어느 쪽이든 동작하는 APIStatusError."""

    def __init__(self, status: int, msg: str = "err") -> None:
        Exception.__init__(self, msg)
        self.status_code = status
        self.body = {"error": {"message": msg, "code": status}}


class _FakeClient:
    """behaviors: 호출 순서별 결과 (Exception이면 raise, str이면 content). 마지막 반복."""

    def __init__(self, *behaviors) -> None:
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        with self._lock:
            self.calls.append(kw)
            b = self.behaviors[min(len(self.calls) - 1, len(self.behaviors) - 1)]
        if isinstance(b, Exception):
            raise b
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=b))])


MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    sm.reset_auth_breaker()
    sm.set_auth_alert_hook(None)
    clock = [1000.0]
    monkeypatch.setattr(sm, "_clock", lambda: clock[0])
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    health: list[tuple[str, bool]] = []
    monkeypatch.setattr(sm, "_record_health", lambda m, ok, el: health.append((m, ok)))
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_AUTH_COOLDOWN_S", raising=False)
    yield SimpleNamespace(clock=clock, health=health)
    sm.reset_auth_breaker()
    sm.set_auth_alert_hook(None)


def _call(client, **kw):
    return sm.chat_with_retry(client, messages=MSGS, max_tokens=10,
                              model="x/primary", context="t", **kw)


def test_401_single_call_no_retry_no_fallback_no_health(_env):
    client = _FakeClient(_StatusErr(401, "User not found."))
    with pytest.raises(sm.OpenRouterAuthError) as ei:
        _call(client, max_attempts=3, fallback_model="y/fallback")
    assert len(client.calls) == 1, "401은 재시도·fallback 금지"
    assert client.calls[0]["model"] == "x/primary"
    assert _env.health == [], "401은 모델 health 실패로 집계 금지"
    # 기존 핸들러(OpenRouterCreditExhausted catch)가 그대로 단락
    assert isinstance(ei.value, sm.OpenRouterCreditExhausted)
    assert sm.auth_breaker_open()


def test_hook_called_once_across_multiple_calls(_env):
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    client = _FakeClient(_StatusErr(401, "User not found."))
    for _ in range(5):
        with pytest.raises(sm.OpenRouterAuthError):
            _call(client)
    assert len(client.calls) == 1, "breaker 열린 뒤엔 네트워크 호출 0"
    assert len(alerts) == 1
    assert "401" in alerts[0] and "OPENROUTER_API_KEY" in alerts[0] and "15분" in alerts[0]


def test_hook_once_under_concurrency(_env):
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    client = _FakeClient(_StatusErr(401))
    errs: list[BaseException] = []

    def _worker():
        try:
            _call(client)
        except BaseException as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=_worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(errs) == 8 and all(isinstance(e, sm.OpenRouterAuthError) for e in errs)
    assert len(alerts) == 1


def test_breaker_open_zero_network(_env):
    sm.trip_auth_breaker(_StatusErr(401))
    client = _FakeClient("never")
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    with pytest.raises(sm.OpenRouterAuthError):
        sm.chat_with_chain(client, models=["a/1", "b/2"], messages=MSGS, max_tokens=10)
    assert client.calls == []
    assert _env.health == []


def test_chain_stops_on_401(_env):
    client = _FakeClient(_StatusErr(401))
    with pytest.raises(sm.OpenRouterAuthError):
        sm.chat_with_chain(client, models=["a/1", "b/2", "c/3"], messages=MSGS, max_tokens=10)
    assert len(client.calls) == 1, "chain 다음 모델로 넘어가지 않음 (같은 키)"


def test_cooldown_expiry_probes_again_and_success_closes(_env):
    client = _FakeClient(_StatusErr(401), "ok content")
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    _env.clock[0] += 899
    assert sm.auth_breaker_open()
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert len(client.calls) == 1
    _env.clock[0] += 2  # 901s — cooldown 만료 → half-open
    assert not sm.auth_breaker_open()
    assert _call(client) == "ok content"
    assert len(client.calls) == 2
    assert not sm.auth_breaker_open()


def test_retrip_after_expiry_does_not_spam_alert(_env):
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    client = _FakeClient(_StatusErr(401))
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    _env.clock[0] += 901
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)  # 재확인 → 또 401 → 재차단
    assert len(client.calls) == 2
    assert sm.auth_breaker_open()
    assert len(alerts) == 1, "같은 장애의 재차단은 재알림 안 함"
    # 장애가 AUTH_REALERT_S 이상 이어지면 리마인더 1회
    _env.clock[0] += sm.AUTH_REALERT_S
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert len(alerts) == 2


def test_success_then_new_incident_alerts_again(_env):
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    client = _FakeClient(_StatusErr(401), "ok", _StatusErr(401))
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    _env.clock[0] += 901
    assert _call(client) == "ok"
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert len(alerts) == 2


def test_cooldown_env_override(_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_AUTH_COOLDOWN_S", "60")
    sm.trip_auth_breaker()
    _env.clock[0] += 59
    assert sm.auth_breaker_open()
    _env.clock[0] += 2
    assert not sm.auth_breaker_open()


def test_hook_exception_swallowed(_env):
    def _boom(msg):
        raise RuntimeError("telegram down")

    sm.set_auth_alert_hook(_boom)
    client = _FakeClient(_StatusErr(401))
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)


def test_tripped_within(_env):
    assert not sm.auth_breaker_tripped_within(7200)
    sm.trip_auth_breaker()
    _env.clock[0] += 7000
    assert sm.auth_breaker_tripped_within(7200)
    _env.clock[0] += 300
    assert not sm.auth_breaker_tripped_within(7200)


def test_402_is_credit_error_any_message(_env):
    client = _FakeClient(_StatusErr(402, "Payment Required"))
    with pytest.raises(sm.OpenRouterCreditExhausted) as ei:
        _call(client)
    assert not isinstance(ei.value, sm.OpenRouterAuthError)
    assert len(client.calls) == 1
    assert not sm.auth_breaker_open(), "402는 키 무효 아님 — breaker 대상 아님"
    assert sm._is_credit_error(_StatusErr(402, "whatever"))


def test_403_key_limit_is_credit_not_auth(_env):
    e = _StatusErr(403, "Key limit exceeded")
    assert sm._is_credit_error(e)
    assert not sm._is_auth_error(e)
    client = _FakeClient(e)
    with pytest.raises(sm.OpenRouterCreditExhausted) as ei:
        _call(client)
    assert not isinstance(ei.value, sm.OpenRouterAuthError)
    assert not sm.auth_breaker_open()


def test_non_auth_errors_still_retried(_env):
    client = _FakeClient(_StatusErr(500, "upstream"), "recovered")
    assert _call(client) == "recovered"
    assert len(client.calls) == 2
    assert _env.health == [("x/primary", False), ("x/primary", True)]
    assert not sm.auth_breaker_open()


def test_raise_for_openrouter_error_helper(_env):
    sm.raise_for_openrouter_error(_StatusErr(500))  # no-op
    with pytest.raises(sm.OpenRouterCreditExhausted):
        sm.raise_for_openrouter_error(_StatusErr(402))
    assert not sm.auth_breaker_open()
    with pytest.raises(sm.OpenRouterAuthError):
        sm.raise_for_openrouter_error(_StatusErr(401))
    assert sm.auth_breaker_open()
    with pytest.raises(sm.OpenRouterAuthError):
        sm.raise_if_auth_breaker_open("direct")


def test_summarize_pdf_propagates_auth_error(_env, monkeypatch, tmp_path):
    """summarize_* 래퍼도 401을 삼키지 않음 — bot_helpers·main이 credit 경로로 처리."""
    monkeypatch.setattr(sm, "_extract_pdf_text", lambda p, max_chars=0: "본문")
    client = _FakeClient(_StatusErr(401))
    with pytest.raises(sm.OpenRouterCreditExhausted):
        sm.summarize_pdf(client, tmp_path / "a.pdf")
    assert len(client.calls) == 1


def test_hook_registered_after_trip_still_alerted(_env):
    """hook 없이 차단된 장애는 알림 슬롯을 소모하지 않음 — 나중에 등록된 hook이 받음."""
    client = _FakeClient(_StatusErr(401))
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    _env.clock[0] += 901
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert len(alerts) == 1
