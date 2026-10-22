"""Layer D 롤백 가드 + model_router 하드닝 — 2026-10-21 Railway env 유출 사고 후속.

- 401 breaker 최근 작동 → 판정 skip (upsert·메시지 없음)
- 모델 2개+ 동시 실패 → systemic (롤백 안 함)
- Railway 토큰 없음 → upsert 시도 없이 수동 안내
- 제외 프로바이더(anthropic) 모델로 롤백 금지
- railway_env: service_ids 지정·available()·에러 메시지에 value 미포함
- handler: 토큰 없으면 수동 안내, canary 401은 '품질 미달' 아님, 제어 명령은 '*' 불인정
- fetcher: /models에 Authorization 헤더 없음
네트워크 미사용 (httpx·canary·upsert 전부 mock).
"""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

from src.model_router import (
    approval_store, canary, fetcher, handler, health, railway_env, rollback,
)
from src.model_router.candidates import TIER_ENV

ALL_ENVS = sorted({e for envs in TIER_ENV.values() for e in envs})


def _sm():
    """현재 src.summarizer — 다른 테스트(test_idea_sourcing)가 sys.modules에서 빼고
    재import시키므로, rollback의 지연 import와 같은 객체를 매번 조회."""
    return importlib.import_module("src.summarizer")


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    """store를 tmp로, TIER env 전부 비움, breaker 초기화."""
    monkeypatch.setattr(health, "_store_dir", lambda: tmp_path)
    monkeypatch.setattr(approval_store, "_store_dir", lambda: tmp_path)
    for e in ALL_ENVS:
        monkeypatch.delenv(e, raising=False)
    monkeypatch.delenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", raising=False)
    _sm().reset_auth_breaker()
    yield tmp_path
    _sm().reset_auth_breaker()


def _fail(model: str, n: int = 10) -> None:
    for _ in range(n):
        health.record_call(model, False, 100)


def _history(tmp_path, items: list[dict]) -> None:
    (tmp_path / "history.json").write_text(json.dumps(items))


class _UpsertSpy:
    def __init__(self, ok: bool = True, msg: str = "✅ 1개 서비스 upsert 성공") -> None:
        self.calls: list[tuple] = []
        self.ok, self.msg = ok, msg

    def __call__(self, name, value, service_ids=None):
        self.calls.append((name, value))
        return self.ok, self.msg


def _no_upsert(*a, **kw):
    raise AssertionError("upsert 호출되면 안 됨")


# ---------------------------------------------------------------- Layer D


def test_skip_when_auth_breaker_tripped(monkeypatch, _iso):
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash")
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    _sm().trip_auth_breaker()
    assert rollback.check_and_rollback() == []


def test_breaker_guard_expires_after_2h(monkeypatch, _iso):
    clock = [5000.0]
    monkeypatch.setattr(_sm(), "_clock", lambda: clock[0])
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash")
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    _sm().trip_auth_breaker()
    clock[0] += rollback.AUTH_GUARD_S + 1
    actions = rollback.check_and_rollback()
    assert spy.calls == [("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")]
    assert actions[0]["applied"] is True


def test_systemic_two_models_no_upsert(monkeypatch, _iso):
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro")
    _fail("deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-pro")
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    actions = rollback.check_and_rollback()
    assert len(actions) == 1
    assert actions[0]["applied"] is False and actions[0]["note"] == "systemic"
    msg = rollback.format_rollback_message(actions)
    assert "플랫폼/키 장애" in msg


def test_same_model_two_envs_is_not_systemic(monkeypatch, _iso):
    """같은 모델이 env 2개에 쓰이면 distinct 1개 — 모델 문제로 정상 롤백."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_NARROW_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash")
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    actions = rollback.check_and_rollback()
    assert ("OPENROUTER_MODEL", "moonshotai/kimi-k2.6") in spy.calls
    assert all(a.get("note") != "systemic" for a in actions)


def test_systemic_needs_min_samples(monkeypatch, _iso):
    monkeypatch.setenv("OPENROUTER_MODEL", "a/one")
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "b/two")
    _fail("a/one", health.ROLLBACK_MIN_SAMPLES)
    _fail("b/two", health.ROLLBACK_MIN_SAMPLES - 1)  # 샘플 부족 — 집계 제외
    assert health.failing_models(["a/one", "b/two", "a/one"]) == ["a/one"]


def test_recent_stats_non_utc_timezone(monkeypatch, _iso):
    """bucket은 UTC — 컨테이너 TZ가 KST여도 방금 기록이 1h 창에 잡혀야 함."""
    import os as _os
    import time as _t
    if not hasattr(_t, "tzset"):
        pytest.skip("tzset 미지원 플랫폼")
    orig = _os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Seoul")
    _t.tzset()
    try:
        _fail("a/one", 3)
        assert health.recent_stats("a/one")["samples"] == 3
    finally:
        if orig is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", orig)
        _t.tzset()


def test_no_token_manual_instruction(monkeypatch, _iso):
    monkeypatch.setenv("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus")
    _fail("qwen/qwen3.7-plus")
    _history(_iso, [{"env_name": "IDEA_NARROW_MODEL", "status": "approved",
                     "old_model": "deepseek/deepseek-v4-flash",
                     "new_model": "qwen/qwen3.7-plus"}])
    monkeypatch.setattr(railway_env, "available", lambda: False)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    actions = rollback.check_and_rollback()
    assert len(actions) == 1
    a = actions[0]
    assert a["applied"] is False and a["note"] == "manual"
    assert a["to"] == "deepseek/deepseek-v4-flash"
    assert "IDEA_NARROW_MODEL" in a["manual"] and "deepseek/deepseek-v4-flash" in a["manual"]
    msg = rollback.format_rollback_message(actions)
    assert "Variables" in msg and "deepseek/deepseek-v4-flash" in msg
    assert "롤백 불가" not in msg


def test_upsert_failure_includes_manual_hint(monkeypatch, _iso):
    monkeypatch.setenv("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus")
    _fail("qwen/qwen3.7-plus")
    _history(_iso, [{"env_name": "IDEA_NARROW_MODEL", "status": "approved",
                     "old_model": "deepseek/deepseek-v4-flash",
                     "new_model": "qwen/qwen3.7-plus"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable",
                        _UpsertSpy(ok=False, msg="❌ 전부 실패: sid=abc: HTTP 401"))
    actions = rollback.check_and_rollback()
    msg = rollback.format_rollback_message(actions)
    assert "롤백 불가" in msg and "Variables" in msg


def test_excluded_provider_not_restored(monkeypatch, _iso):
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro")
    _fail("deepseek/deepseek-v4-pro")
    _history(_iso, [{"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
                     "old_model": "anthropic/claude-sonnet-4.6",
                     "new_model": "deepseek/deepseek-v4-pro"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    assert rollback._previous_model_for_env(
        "IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro") is None
    actions = rollback.check_and_rollback()
    assert actions[0]["to"] is None and actions[0]["applied"] is False


def test_excluded_provider_skipped_for_older_candidate(_iso):
    _history(_iso, [
        {"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
         "old_model": "openai/gpt-5.1", "new_model": "deepseek/deepseek-v4-pro"},
        {"env_name": "IDEA_SYNTHESIS_MODEL", "status": "auto_applied",
         "old_model": "anthropic/claude-sonnet-4.6", "new_model": "deepseek/deepseek-v4-pro"},
    ])
    assert rollback._previous_model_for_env(
        "IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro") == "openai/gpt-5.1"


def test_exclusion_disabled_allows_restore(monkeypatch, _iso):
    monkeypatch.setenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", "")
    _history(_iso, [{"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
                     "old_model": "anthropic/claude-sonnet-4.6",
                     "new_model": "deepseek/deepseek-v4-pro"}])
    assert rollback._previous_model_for_env(
        "IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro") == "anthropic/claude-sonnet-4.6"


# ---------------------------------------------------------------- railway_env


class _Resp:
    def __init__(self, status: int, data) -> None:
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


def _fake_httpx(responses: list[_Resp], posts: list[dict]):
    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            posts.append(json)
            return responses[min(len(posts) - 1, len(responses) - 1)]

    return SimpleNamespace(Client=_Client)


def _railway_env(monkeypatch, sids="s1,s2,s3", token="tok-fake-123"):
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", token)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid")
    monkeypatch.setenv("RAILWAY_SERVICE_IDS", sids)


def test_available(monkeypatch):
    for n in ("RAILWAY_PROJECT_ACCESS_TOKEN", "RAILWAY_ACCESS_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "s1")
    monkeypatch.delenv("RAILWAY_SERVICE_IDS", raising=False)
    assert railway_env.available() is False
    monkeypatch.setenv("RAILWAY_ACCESS_TOKEN", "tok-fake")
    assert railway_env.available() is True


def test_upsert_service_ids_subset(monkeypatch):
    _railway_env(monkeypatch)
    posts: list[dict] = []
    monkeypatch.setattr(railway_env, "httpx",
                        _fake_httpx([_Resp(200, {"data": {"variableUpsert": True}})], posts))
    ok, _ = railway_env.upsert_variable("X", "v", service_ids=["s2"])
    assert ok
    assert [p["variables"]["input"]["serviceId"] for p in posts] == ["s2"]
    posts.clear()
    ok, _ = railway_env.upsert_variable("X", "v")  # None = 전 서비스 (기존 동작)
    assert [p["variables"]["input"]["serviceId"] for p in posts] == ["s1", "s2", "s3"]


def test_delete_service_ids_subset(monkeypatch):
    _railway_env(monkeypatch)
    posts: list[dict] = []
    monkeypatch.setattr(railway_env, "httpx",
                        _fake_httpx([_Resp(200, {"data": {"variableDelete": True}})], posts))
    ok, msg = railway_env.delete_variable("X", service_ids=["s3", "s1"])
    assert ok and "2개" in msg
    assert [p["variables"]["input"]["serviceId"] for p in posts] == ["s3", "s1"]


def test_upsert_error_never_echoes_value(monkeypatch):
    _railway_env(monkeypatch, sids="s1")
    secret = "1SESSIONSTRINGfake0123456789abcdef"
    echo = {"errors": [{"message": f"invalid value {secret} for TG_SESSION_STRING"}],
            "data": None, "input": {"value": secret}}
    posts: list[dict] = []
    monkeypatch.setattr(railway_env, "httpx", _fake_httpx([_Resp(400, echo)], posts))
    ok, msg = railway_env.upsert_variable("TG_SESSION_STRING", secret)
    assert not ok
    assert secret not in msg and "tok-fake-123" not in msg
    assert "HTTP 400" in msg


def test_upsert_exception_never_echoes_value(monkeypatch):
    _railway_env(monkeypatch, sids="s1")
    secret = "supersecretvalue-fake-xyz"

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError(f"bad payload {secret}")

    monkeypatch.setattr(railway_env, "httpx", SimpleNamespace(Client=_Boom))
    ok, msg = railway_env.upsert_variable("SOME_KEY", secret)
    assert not ok and secret not in msg


# ---------------------------------------------------------------- fetcher


def test_fetch_models_sends_no_authorization(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fakefakefake")
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "mgmt-fake")
    seen: list[dict] = []

    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            seen.append({"url": url, **kw})
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": [{"id": "a/b", "pricing": {"prompt": "0.000001",
                                                                 "completion": "0.000002"}}]},
            )

    monkeypatch.setattr(fetcher, "httpx", SimpleNamespace(Client=_Client))
    out = fetcher.fetch_models()
    assert "a/b" in out
    assert seen and not (seen[0].get("headers") or {}).get("Authorization")
    assert "headers" not in seen[0] or not seen[0]["headers"]


# ---------------------------------------------------------------- canary


def test_canary_breaker_open_no_network(monkeypatch):
    monkeypatch.setattr(canary, "auth_breaker_open", lambda: True)

    def _no_call(*a, **kw):
        raise AssertionError("breaker 열림 — LLM 호출 금지")

    monkeypatch.setattr(canary, "chat_with_retry", _no_call)
    monkeypatch.setattr(canary, "get_client", _no_call)
    monkeypatch.delenv("MODEL_ROUTER_SKIP_CANARY", raising=False)
    r = canary.run_canary("OPENROUTER_MODEL", "x/y")
    assert r["passed"] is False and r["auth_error"] is True


def test_canary_auth_error_flagged(monkeypatch):
    calls = []

    def _auth_fail(*a, **kw):
        calls.append(1)
        raise canary.OpenRouterAuthError("401")

    monkeypatch.setattr(canary, "auth_breaker_open", lambda: False)
    monkeypatch.setattr(canary, "chat_with_retry", _auth_fail)
    monkeypatch.setattr(canary, "get_client", lambda: object())
    monkeypatch.delenv("MODEL_ROUTER_SKIP_CANARY", raising=False)
    r = canary.run_canary("OPENROUTER_MODEL", "x/y")
    assert r["passed"] is False and r["auth_error"] is True
    assert len(calls) == 1


# ---------------------------------------------------------------- handler


def test_apply_change_manual_without_railway(monkeypatch):
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: False)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    status, msg = asyncio.run(handler._apply_change("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus"))
    assert status == "manual"
    assert "IDEA_NARROW_MODEL" in msg and "qwen/qwen3.7-plus" in msg
    assert "대시보드 Variables에서 수정" in msg


def test_apply_change_canary_auth_is_not_quality(monkeypatch):
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": False, "failures": ["[0] 401"],
                                      "sentinels": 1, "auth_error": True})
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    status, msg = asyncio.run(handler._apply_change("OPENROUTER_MODEL", "x/y"))
    assert status == "key_failed"
    assert "키" in msg and "품질" not in msg


def test_apply_change_quality_failure_unchanged(monkeypatch):
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": False, "failures": ["[0] validator: x"],
                                      "sentinels": 1})
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    status, msg = asyncio.run(handler._apply_change("OPENROUTER_MODEL", "x/y"))
    assert status == "canary_failed" and "Canary 실패" in msg


def test_apply_change_applied(monkeypatch):
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: True)
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    status, _ = asyncio.run(handler._apply_change("OPENROUTER_MODEL", "x/y"))
    assert status == "applied" and spy.calls == [("OPENROUTER_MODEL", "x/y")]


def _admin_env(monkeypatch, report_allowed="", report_chat="", allowed=""):
    for n, v in (("REPORT_ALLOWED_CHAT_IDS", report_allowed), ("REPORT_CHAT_ID", report_chat),
                 ("ALLOWED_CHAT_IDS", allowed)):
        if v:
            monkeypatch.setenv(n, v)
        else:
            monkeypatch.delenv(n, raising=False)


def test_is_admin_wildcard_not_for_control_plane(monkeypatch):
    _admin_env(monkeypatch, allowed="*")
    assert handler._is_admin(999) is True  # 읽기 명령(/model_status)은 기존 동작
    assert handler._is_admin(999, control_plane=True) is False


def test_is_admin_control_plane_explicit_ids(monkeypatch):
    _admin_env(monkeypatch, report_allowed="*", allowed="*,-100123,456")
    assert handler._is_admin(-100123, control_plane=True) is True
    assert handler._is_admin(456, control_plane=True) is True
    assert handler._is_admin(999, control_plane=True) is False
    _admin_env(monkeypatch, allowed="111,222")
    assert handler._is_admin(111, control_plane=True) is True
    assert handler._is_admin(333, control_plane=True) is False


class _Msg:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _update(chat_id: int, msg: _Msg):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=msg)


def test_approve_cmd_wildcard_denied(monkeypatch):
    _admin_env(monkeypatch, allowed="*")
    msg = _Msg()
    asyncio.run(handler.model_approve_cmd(_update(5, msg), SimpleNamespace(args=["all"])))
    assert msg.replies and "권한 없음" in msg.replies[0]


def test_approve_cmd_manual_flow(monkeypatch, _iso):
    _admin_env(monkeypatch, allowed="42")
    approval_store.save_pending([{
        "id": 1, "env_name": "IDEA_NARROW_MODEL", "old_model": "a/old",
        "new_model": "qwen/qwen3.7-plus", "status": "pending",
    }])
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: False)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    msg = _Msg()
    asyncio.run(handler.model_approve_cmd(_update(42, msg), SimpleNamespace(args=["1"])))
    joined = "\n".join(msg.replies)
    assert "수동" in joined and "qwen/qwen3.7-plus" in joined
    assert "전부 실패" not in joined
    assert approval_store.load_pending()[0]["status"] == "approved"


def test_approve_cmd_auth_failure_hint(monkeypatch, _iso):
    _admin_env(monkeypatch, allowed="42")
    approval_store.save_pending([{
        "id": 1, "env_name": "OPENROUTER_MODEL", "old_model": "a/old",
        "new_model": "b/new", "status": "pending",
    }])
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": False, "failures": ["[0] 401"],
                                      "sentinels": 1, "auth_error": True})
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    msg = _Msg()
    asyncio.run(handler.model_approve_cmd(_update(42, msg), SimpleNamespace(args=["1"])))
    final = msg.replies[-1]
    assert "전부 실패" in final and "키" in final
    assert "품질 미달" not in final
    assert approval_store.load_pending()[0]["status"] == "pending"


def test_eval_message_manual_section():
    rec = {"env_name": "OPENROUTER_MODEL", "old_model": "a/old", "new_model": "b/new",
           "reason": "x"}
    out = handler._format_eval_message([rec], [], [], [rec])
    assert "수동 적용 필요" in out and "b/new" in out
    assert "변경 권고 없음" not in out


def test_model_health_job_silent_on_breaker(monkeypatch, _iso):
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash")
    monkeypatch.setenv("REPORT_CHAT_ID", "42")
    _sm().trip_auth_breaker()
    sent: list = []

    class _Bot:
        async def send_message(self, *a, **kw):
            sent.append(a)

    asyncio.run(handler.model_health_job(_Bot()))
    assert sent == []


def test_make_auth_alert_hook_schedules_on_loop(monkeypatch):
    _admin_env(monkeypatch)
    monkeypatch.setenv("REPORT_CHAT_ID", "42")
    sent: list[tuple] = []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    loop = asyncio.new_event_loop()
    try:
        hook = handler.make_auth_alert_hook(_Bot(), loop)
        assert hook is not None
        hook("🔑 401")  # 다른 스레드(루프 미실행)에서 호출돼도 예약만 하고 즉시 반환
        loop.run_until_complete(asyncio.sleep(0.01))
    finally:
        loop.close()
    assert sent == [("42", "🔑 401")]


def test_make_auth_alert_hook_none_without_target(monkeypatch):
    _admin_env(monkeypatch)
    monkeypatch.delenv("MODEL_ROUTER_CHAT_ID", raising=False)
    assert handler.make_auth_alert_hook(object(), None) is None
