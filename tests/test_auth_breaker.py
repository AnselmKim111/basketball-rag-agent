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


TEST_KEY = "sk-or-v1-TESTONLY-not-a-real-key-0000"
_REAL_KEY_HTTP_GET = sm._auth_key_http_get  # autouse fixture가 stub으로 바꾸기 전 원본


class _KeyedClient(_FakeClient):
    """실제 OpenAI client처럼 base_url·api_key 보유 → half-open 키 재확인(GET /key) 대상."""

    base_url = "https://openrouter.ai/api/v1/"
    api_key = TEST_KEY


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
    # half-open 키 재확인(GET /key) — 실제 네트워크 금지. result: int status 또는 raise할 예외
    key = SimpleNamespace(calls=[], result=200)

    def _key_get(url, k, timeout):
        key.calls.append((url, k, timeout))
        if isinstance(key.result, BaseException):
            raise key.result
        return key.result

    monkeypatch.setattr(sm, "_auth_key_http_get", _key_get)
    yield SimpleNamespace(clock=clock, health=health, key=key)
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


# ------------------------------------------------------------------
# half-open single-flight — cooldown 만료 후 키 재확인(GET /key)은 1건만 (sim_b_halfopen: 8→1)
# ------------------------------------------------------------------
class _KeyGate:
    """_auth_key_http_get 대체 — 진입 즉시 카운트 후 release까지 대기 (재확인 진행 중 재현)."""

    def __init__(self, result: object = 401) -> None:
        self.result = result  # int status 또는 raise할 예외
        self.calls: list[tuple] = []
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.inside = threading.Event()
        self.probe_saw_open: list[bool] = []

    def __call__(self, url, key, timeout):
        with self._lock:
            self.calls.append((url, key, timeout))
        self.probe_saw_open.append(sm.auth_breaker_open())  # probe 자신은 차단 아님
        self.inside.set()
        assert self.release.wait(5), "테스트 데드락"
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _burst(client, gate: _KeyGate, n: int = 8):
    """n스레드 동시 호출 — 키 재확인 1건이 진행 중인 동안 나머지 n-1건이 끝나야 함."""
    errs: list[BaseException] = []
    oks: list[str] = []
    lock = threading.Lock()
    others_done = threading.Event()

    def _worker():
        try:
            r = _call(client, max_attempts=1)
            with lock:
                oks.append(r)
        except BaseException as e:  # noqa: BLE001
            with lock:
                errs.append(e)
                if len(errs) >= n - 1:
                    others_done.set()

    gate.release.clear()
    gate.inside.clear()
    ts = [threading.Thread(target=_worker) for _ in range(n)]
    for t in ts:
        t.start()
    assert gate.inside.wait(5), "probe가 키 재확인에 도달하지 않음"
    assert others_done.wait(5), "재확인 진행 중 나머지 호출이 즉시 차단되지 않음"
    # 재확인 진행 중: 다른 스레드(메인)에는 차단으로 보임
    assert sm.auth_breaker_open()
    with pytest.raises(sm.OpenRouterAuthError):
        sm.raise_if_auth_breaker_open("direct")
    gate.release.set()
    for t in ts:
        t.join(5)
    return errs, oks


def test_half_open_single_flight_one_call_per_window(_env, monkeypatch):
    """키가 계속 죽어 있으면 창마다 키 재확인 정확히 1건, 실제 LLM 호출 0건."""
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    gate = _KeyGate(401)
    monkeypatch.setattr(sm, "_auth_key_http_get", gate)
    client = _KeyedClient("ok")
    sm.trip_auth_breaker(_StatusErr(401))
    assert len(alerts) == 1
    for window in range(3):
        _env.clock[0] += 901  # cooldown 만료
        before = len(gate.calls)
        errs, oks = _burst(client, gate)
        assert len(gate.calls) - before == 1, f"window {window}: 키 재확인 1건만"
        assert client.calls == [], "키 무효 판정 — 실제 LLM 호출 생략"
        assert oks == [] and len(errs) == 8
        assert all(isinstance(e, sm.OpenRouterAuthError) for e in errs)
        blocked = [e for e in errs if "(차단 중)" in str(e)]
        assert len(blocked) == 7, "나머지 7건은 재확인 결과 전 즉시 차단"
        assert sm.auth_breaker_open(), "재확인 401 → 재차단"
        assert sm._auth_probe_owner is None
    assert gate.probe_saw_open == [False, False, False]
    assert gate.calls[0] == ("https://openrouter.ai/api/v1/key", TEST_KEY, sm.AUTH_PROBE_TIMEOUT_S)
    assert len(alerts) == 1, "같은 장애 재차단은 재알림 없음"

    # 키 복구 — 재확인 200 → breaker 해제 → probe 스레드 실제 호출 + 이후 호출 전부 통과
    _env.clock[0] += 901
    gate.result = 200
    errs, oks = _burst(client, gate)
    assert oks == ["ok"] and len(errs) == 7  # 재확인(≤AUTH_PROBE_TIMEOUT_S) 중 도착분만 차단
    assert not sm.auth_breaker_open() and not sm._auth_tripped
    before_keys, before = len(gate.calls), len(client.calls)
    results: list[str] = []
    ts = [threading.Thread(target=lambda: results.append(_call(client))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert results == ["ok"] * 8 and len(client.calls) - before == 8
    assert len(gate.calls) == before_keys, "해제 후엔 재확인 없음"


def test_403_on_key_check_retrips(_env):
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    _env.key.result = 403
    client = _KeyedClient("never")
    with pytest.raises(sm.OpenRouterAuthError) as ei:
        _call(client)
    assert "(차단 중)" not in str(ei.value)
    assert client.calls == [] and len(_env.key.calls) == 1
    assert sm.auth_breaker_open()


class _SlowFirstClient(_KeyedClient):
    """첫 create만 release까지 대기 (수 분짜리 합성 호출 재현), 나머지는 즉시 응답."""

    def __init__(self) -> None:
        super().__init__("ok")
        self.first_inside = threading.Event()
        self.release = threading.Event()

    def _create(self, **kw):
        with self._lock:
            self.calls.append(kw)
            first = len(self.calls) == 1
        if first:
            self.first_inside.set()
            assert self.release.wait(5), "테스트 데드락"
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="slow ok"))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])


def test_valid_key_slow_probe_call_does_not_block_others(_env):
    """키 복구 후 첫 호출(probe)이 느린 합성이어도 다른 호출은 '(차단 중)' 없이 통과.

    final/sim_probe_block 회귀: 예전엔 실제 호출 자체가 probe라 끝날 때까지(최대 300s) 전부 차단.
    """
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    client = _SlowFirstClient()
    slow = threading.Thread(target=lambda: slow_out.append(_call(client, max_attempts=1)))
    slow_out: list[str] = []
    slow.start()
    assert client.first_inside.wait(5)
    # 느린 실제 호출 진행 중 — 키 재확인은 이미 끝났고 임대도 해제
    assert sm._auth_probe_owner is None
    assert not sm.auth_breaker_open()
    results: list[object] = []
    lock = threading.Lock()

    def _other():
        try:
            r = _call(client, max_attempts=1)
        except BaseException as e:  # noqa: BLE001
            r = e
        with lock:
            results.append(r)

    ts = [threading.Thread(target=_other) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert results == ["ok"] * 5, results
    assert len(_env.key.calls) == 1, "재확인은 probe 1건만"
    client.release.set()
    slow.join(5)
    assert slow_out == ["slow ok"]
    assert not sm.auth_breaker_open()


def _in_thread(fn):
    """다른 스레드에서 실행 (probe 소유 스레드와 구분) → (결과, 예외)."""
    out: list = [None, None]

    def _run():
        try:
            out[0] = fn()
        except BaseException as e:  # noqa: BLE001
            out[1] = e

    t = threading.Thread(target=_run)
    t.start()
    t.join(5)
    return out[0], out[1]


def test_probe_non_auth_error_releases_no_wedge(_env):
    """probe가 timeout·5xx로 끝나도 probe 해제 → 다음 호출이 재확인 (breaker 영구 봉쇄 금지)."""
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    client = _FakeClient(TimeoutError("read timeout"), _StatusErr(503, "upstream"),
                         TimeoutError("read timeout"), "ok")
    r, err = _in_thread(lambda: _call(client, max_attempts=3))
    assert (r, err) == ("", None), "non-auth 실패 3회 → 빈 응답"
    assert len(client.calls) == 3
    assert sm._auth_probe_owner is None, "probe 해제됨 — 차단 상태로 고착 안 됨"
    assert _in_thread(sm.auth_breaker_open) == (False, None)
    assert _in_thread(lambda: _call(client)) == ("ok", None), "다른 스레드 호출이 판정 (재확인 보류 중 — 차단 없음)"
    assert len(client.calls) == 4
    assert not sm._auth_tripped


def test_probe_escaping_exception_releases(_env, monkeypatch):
    """chat_with_retry 밖으로 예외가 새어도 finally로 probe 해제."""
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901

    def _boom(m, ok, el):
        raise RuntimeError("health store down")

    monkeypatch.setattr(sm, "_record_health", _boom)
    client = _FakeClient(_StatusErr(500, "upstream"), "ok")
    with pytest.raises(RuntimeError):
        _call(client)
    assert sm._auth_probe_owner is None
    monkeypatch.setattr(sm, "_record_health", lambda m, ok, el: None)
    assert _in_thread(lambda: _call(client)) == ("ok", None)


def test_probe_lease_expiry_unwedges_stuck_probe(_env):
    """probe 스레드가 해제 없이 멈춰도 임대 만료 후 다른 호출이 재확인."""
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    t = threading.Thread(target=sm._auth_gate, args=("stuck",))  # 쥐고 해제 안 함
    t.start()
    t.join(5)
    assert sm.auth_breaker_open()
    client = _FakeClient("ok")
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert client.calls == []
    _env.clock[0] += sm.AUTH_PROBE_LEASE_S + 1
    assert _call(client) == "ok"
    assert len(client.calls) == 1 and not sm.auth_breaker_open()


@pytest.mark.parametrize("key_result", [ConnectionError("down"), 502])
def test_chain_single_key_check_across_models(_env, key_result):
    """chain → chat_with_retry 중첩: 재확인은 바깥 1회 — 판정 불가여도 모델마다 재확인 안 함."""
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    _env.key.result = key_result
    client = _KeyedClient(_StatusErr(500), _StatusErr(500), "ok")
    content, used = sm.chat_with_chain(client, models=["a/1", "b/2"], messages=MSGS,
                                       max_tokens=10, context="chain")
    assert (content, used) == ("ok", "b/2")
    assert len(client.calls) == 3
    assert len(_env.key.calls) == 1
    assert not sm.auth_breaker_open() and sm._auth_probe_owner is None


def test_chain_probe_all_fail_releases(_env):
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    client = _FakeClient(_StatusErr(500))
    assert sm.chat_with_chain(client, models=["a/1", "b/2"], messages=MSGS,
                              max_tokens=10) == ("", "")
    assert sm._auth_probe_owner is None
    assert _in_thread(sm.auth_breaker_open) == (False, None)


def test_empty_200_closes_breaker(_env):
    """빈 content라도 HTTP 응답 = 키 인증 통과 → breaker 해제 (half-open 고착 방지)."""
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    client = _FakeClient("")
    assert _call(client, max_attempts=2) == ""
    assert not sm._auth_tripped and not sm.auth_breaker_open()


def test_retry_rechecks_breaker_after_backoff(_env, monkeypatch):
    """재시도 대기 중 다른 호출이 401로 차단하면 다음 시도는 네트워크 없이 raise."""
    client = _FakeClient(_StatusErr(500), "never")
    monkeypatch.setattr(sm.time, "sleep", lambda s: sm.trip_auth_breaker(_StatusErr(401)))
    with pytest.raises(sm.OpenRouterAuthError) as ei:
        _call(client, max_attempts=3)
    assert "(차단 중)" in str(ei.value)
    assert len(client.calls) == 1


def test_clear_auth_alerted_realerts_next_trip(_env):
    """알림 발송 실패 호출부가 clear_auth_alerted() → 다음 차단 때 6h 대기 없이 재알림."""
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    client = _FakeClient(_StatusErr(401))
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert len(alerts) == 1
    sm.clear_auth_alerted()
    assert sm._auth_alerted_at is None
    assert sm.auth_breaker_open(), "알림 기록만 해제 — breaker는 그대로"
    _env.clock[0] += 901
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert len(alerts) == 2


def test_hook_exception_realerts_next_trip(_env):
    """hook 자체가 실패하면(미전달) 알림 기록 해제 — 다음 재차단 때 다시 시도."""
    calls: list[str] = []

    def _flaky(msg):
        calls.append(msg)
        if len(calls) == 1:
            raise RuntimeError("telegram down")

    sm.set_auth_alert_hook(_flaky)
    client = _FakeClient(_StatusErr(401))
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert sm._auth_alerted_at is None
    _env.clock[0] += 901
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert len(calls) == 2
    _env.clock[0] += 901
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    assert len(calls) == 2, "전달 성공 후엔 기존 dedup"


# ------------------------------------------------------------------
# 키 재확인(GET {base_url}/key) — 판정·네트워크 오류·키 비노출
# ------------------------------------------------------------------
class _ObservingClient(_KeyedClient):
    """실제 호출 시점의 breaker 상태 기록 — 재확인 후 임대가 풀렸는지 확인용."""

    def __init__(self, *behaviors) -> None:
        super().__init__(*behaviors)
        self.seen: list[tuple] = []

    def _create(self, **kw):
        other_open = _in_thread(sm.auth_breaker_open)[0]
        self.seen.append((sm._auth_probe_owner, sm._auth_tripped, other_open))
        return super()._create(**kw)


def test_probe_network_error_releases_real_call_decides(_env, caplog):
    """재확인 네트워크 오류 → 상태 그대로 임대만 해제, 실제 호출이 판정 (고착 금지)."""
    caplog.set_level("DEBUG")
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    _env.key.result = ConnectionError("connect failed https://openrouter.ai/api/v1/key")

    # (a) 실제 호출 성공 → 해제. 실제 호출 중엔 임대 없음·다른 스레드 안 막힘
    client = _ObservingClient("ok")
    assert _in_thread(lambda: _call(client)) == ("ok", None)
    assert client.seen == [(None, True, False)]
    assert len(_env.key.calls) == 1 and not sm._auth_tripped

    # (b) 실제 호출 401 → 재차단 (재확인 불확정이 키 판정을 대신하지 않음)
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    client = _ObservingClient(_StatusErr(401))
    _, err = _in_thread(lambda: _call(client, max_attempts=3))
    assert isinstance(err, sm.OpenRouterAuthError) and len(client.calls) == 1
    assert sm.auth_breaker_open()

    # (c) 실제 호출도 timeout → tripped 유지. 보류 기간엔 재확인 없이 실제 호출이 판정
    #     (연쇄 재확인·차단 없음), 보류가 끝나면 다음 호출이 새로 재확인
    _env.clock[0] += 901
    client = _ObservingClient(TimeoutError("read timeout"))
    assert _in_thread(lambda: _call(client, max_attempts=1)) == ("", None)
    assert sm._auth_tripped and sm._auth_probe_owner is None
    assert not sm.auth_breaker_open()
    before = len(_env.key.calls)
    _env.key.result = 200
    for _ in range(3):  # 보류 중 — 다른 스레드들도 차단·재확인 없이 실제 호출
        assert _in_thread(lambda: _call(client, max_attempts=1)) == ("", None)
    assert len(_env.key.calls) == before and sm._auth_tripped
    assert client.seen[-1] == (None, True, False)
    _env.clock[0] += sm.AUTH_PROBE_LEASE_S + 1
    client = _ObservingClient("ok")
    assert _in_thread(lambda: _call(client)) == ("ok", None)
    assert len(_env.key.calls) == before + 1 and not sm._auth_tripped
    assert TEST_KEY not in caplog.text, "키 값은 로그 금지"


def test_probe_inconclusive_no_cascading_block(_env, monkeypatch):
    """재확인이 불확정(네트워크 오류)으로 끝난 뒤 느린 실제 호출 진행 중 — 이후 호출이 매번
    재확인을 쥐고 서로 '(차단 중)'으로 막지 않음 (probefix/sim_probe_e2e neterr 회귀)."""
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += 901
    gate = _KeyGate(ConnectionError("reset"))
    gate.release.set()
    monkeypatch.setattr(sm, "_auth_key_http_get", gate)
    client = _SlowFirstClient()
    slow_out: list[str] = []
    slow = threading.Thread(target=lambda: slow_out.append(_call(client, max_attempts=1)))
    slow.start()
    assert client.first_inside.wait(5)
    assert not sm.auth_breaker_open() and sm._auth_tripped
    gate.release.clear()  # 혹시 재확인을 또 쥐면 걸리도록 (그 사이 도착분이 막히는지 확인)
    results: list[object] = []
    lock = threading.Lock()

    def _other():
        try:
            r = _call(client, max_attempts=1)
        except BaseException as e:  # noqa: BLE001
            r = e
        with lock:
            results.append(r)

    ts = [threading.Thread(target=_other) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    gate.release.set()
    assert results == ["ok"] * 5, results
    assert len(gate.calls) == 1
    assert not sm._auth_tripped, "실제 호출 성공이 판정 → 해제"
    client.release.set()
    slow.join(5)
    assert slow_out == ["slow ok"]


def test_key_check_verdicts(_env):
    client = _KeyedClient("x")
    for status, verdict in [(200, "ok"), (204, "ok"), (401, "revoked"), (403, "revoked"),
                            (404, "unknown"), (429, "unknown"), (500, "unknown")]:
        _env.key.result = status
        assert sm._auth_key_check(client) == (verdict, f"HTTP {status}")
    _env.key.result = OSError(f"boom {TEST_KEY}")
    assert sm._auth_key_check(client) == ("unknown", "OSError"), "예외는 타입명만 (메시지에 키 가능)"
    calls = len(_env.key.calls)
    # 자격증명 없는 client(테스트 더미·MagicMock 등)는 네트워크 없이 판정 불가
    for c in (_FakeClient("x"), SimpleNamespace(base_url="https://x/", api_key=None),
              SimpleNamespace(base_url=None, api_key=TEST_KEY), None):
        assert sm._auth_key_check(c)[0] == "unknown"
    assert len(_env.key.calls) == calls
    assert _env.key.calls[0] == ("https://openrouter.ai/api/v1/key", TEST_KEY, sm.AUTH_PROBE_TIMEOUT_S)


def test_key_http_get_bearer_no_redirect(monkeypatch):
    """GET {base_url}/key — Bearer 헤더, timeout, 리다이렉트 미추종 (키가 다른 호스트로 안 샘)."""
    import sys
    seen: dict = {}

    def _get(url, **kw):
        seen.update(url=url, **kw)
        return SimpleNamespace(status_code=401)

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=_get))
    assert _REAL_KEY_HTTP_GET("https://openrouter.ai/api/v1/key", TEST_KEY, 10.0) == 401
    assert seen == {"url": "https://openrouter.ai/api/v1/key",
                    "headers": {"Authorization": f"Bearer {TEST_KEY}"},
                    "timeout": 10.0, "follow_redirects": False}


def test_probe_lease_short(_env):
    """임대는 재확인 요청만 덮음 — 실제 호출(수 분) 길이와 무관."""
    assert sm.AUTH_PROBE_TIMEOUT_S < sm.AUTH_PROBE_LEASE_S <= 30


def test_revoked_probe_logs_without_key(_env, caplog):
    caplog.set_level("DEBUG")
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    sm.trip_auth_breaker(_StatusErr(401))
    _env.clock[0] += sm.AUTH_REALERT_S  # 리마인더 시점 — 재확인 401도 재알림 경로를 탐
    _env.key.result = 401
    with pytest.raises(sm.OpenRouterAuthError) as ei:
        _call(_KeyedClient("never"))
    assert len(alerts) == 2
    assert "GET /key" in caplog.text and "HTTP 401" in caplog.text
    assert TEST_KEY not in caplog.text and TEST_KEY not in str(ei.value)
    assert TEST_KEY not in "".join(alerts)


def test_keycheck_ok_but_chat_401_keeps_alert_dedup(_env):
    """/key는 200인데 chat은 계속 401 — cooldown마다 재알림 금지 (6h dedup 유지).

    키 재확인 OK는 breaker만 닫고 알림 기록은 실제 호출 성공 때만 해제.
    """
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    _env.key.result = 200
    client = _KeyedClient(_StatusErr(401, "User not found."))
    for _ in range(4):  # 최초 차단 + cooldown 3회
        with pytest.raises(sm.OpenRouterAuthError):
            _call(client)
        _env.clock[0] += sm._auth_cooldown_s() + 1
    assert len(alerts) == 1, "같은 장애는 AUTH_REALERT_S 안에 1회만 알림"
    assert len(_env.key.calls) == 3


def test_real_success_after_keycheck_clears_alert_record(_env):
    alerts: list[str] = []
    sm.set_auth_alert_hook(alerts.append)
    client = _KeyedClient(_StatusErr(401), "ok", _StatusErr(401))
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)
    _env.clock[0] += sm._auth_cooldown_s() + 1
    assert _call(client) == "ok"            # 키 복구 → 실제 성공이 알림 기록 해제
    with pytest.raises(sm.OpenRouterAuthError):
        _call(client)                       # 새 장애 → 다시 알림
    assert len(alerts) == 2
