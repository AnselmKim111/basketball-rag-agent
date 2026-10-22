"""railway_env — 토큰 실제 유효성 probe(token_valid) + 에러 요약 scrub-후-자르기 검증.

사고(2026-10-21 Railway env 유출): 폐기된 토큰이 Variables에 남아 있으면 available()(설정 존재만
확인)이 True → tg_login이 사용자 폰에 코드를 보내고 env 삭제·세션 upsert가 전부 401.
token_valid()는 인증 GraphQL 1회(projectToken 자기조회 — 비밀값 미반환)로 실제 인증을 확인.
probe()는 같은 판정을 3상태로 — 'ok' | 'invalid'(확정, 캐시) | 'unknown'(일시 장애, 캐시 안 함).
token_valid() == (probe() == 'ok'). tg_login이 일시 장애를 "토큰 무효"로 오안내하지 않게 구분.
가짜 httpx만 사용 (네트워크 없음), 토큰·값은 전부 가짜.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.model_router import railway_env

TOKEN = "tok-fake-railway-0123456789"
PID = "pid-fake-0001"
# Telethon StringSession 모양의 가짜 값 ('1' + urlsafe 352자 = 353자)
FAKE_SESSION = "1" + ("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_" * 10)[:352]


class _Resp:
    def __init__(self, status: int, data) -> None:
        self.status_code = status
        self._data = data

    def json(self):
        if isinstance(self._data, BaseException):
            raise self._data
        return self._data


def _fake_httpx(responses: list, calls: list[dict]):
    """responses: _Resp 또는 예외 인스턴스(post 시 raise) — 호출 순서대로, 마지막 것 반복."""

    class _Client:
        def __init__(self, *a, **kw) -> None:
            self.kw = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers, "client_kw": self.kw})
            r = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(r, BaseException):
                raise r
            return r

    return SimpleNamespace(Client=_Client)


# conftest가 httpx를 더미로 바꿔 두므로 httpx 예외 계층을 흉내 낸 로컬 예외 사용
class _HTTPXTransportError(Exception):
    pass


class _ConnectError(_HTTPXTransportError):  # httpx.ConnectError 상당
    pass


class _ReadTimeout(_HTTPXTransportError):  # httpx.ReadTimeout 상당
    pass


def _valid(pid=PID):
    return _Resp(200, {"data": {"projectToken": {"projectId": pid, "environmentId": "eid"}}})


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("RAILWAY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_IDS", raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", PID)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid-fake")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "sid-fake-0001")
    railway_env.reset_token_cache()
    yield
    railway_env.reset_token_cache()


def _install(monkeypatch, responses):
    calls: list[dict] = []
    monkeypatch.setattr(railway_env, "httpx", _fake_httpx(responses, calls))
    return calls


# ---------------------------------------------------------------- token_valid


def test_valid_token_probes_once_with_safe_query(cfg, monkeypatch):
    calls = _install(monkeypatch, [_valid()])
    assert railway_env.token_valid() is True
    assert railway_env.token_valid() is True  # 캐시
    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == railway_env.GRAPHQL_URL
    assert c["headers"]["Project-Access-Token"] == TOKEN
    assert c["client_kw"].get("timeout") == 10
    # 비밀값을 돌려주는 variables 쿼리 금지 — 토큰 자기조회만
    assert c["json"] == {"query": "query{projectToken{projectId environmentId}}"}
    assert "variables" not in c["json"]["query"]


def test_force_reprobes(cfg, monkeypatch):
    calls = _install(monkeypatch, [_valid(), _Resp(200, {"errors": [{"message": "Not Authorized"}],
                                                         "data": None})])
    assert railway_env.token_valid() is True
    assert railway_env.token_valid(force=True) is False
    assert railway_env.token_valid() is False  # force 결과로 캐시 갱신
    assert len(calls) == 2


@pytest.mark.parametrize("resp", [
    # Railway는 폐기 토큰에 HTTP 200 + errors로 답할 수 있음 — status만 보면 놓침
    _Resp(200, {"errors": [{"message": "Not Authorized"}], "data": None}),
    _Resp(200, {"errors": [{"message": "Not Authorized"}],
                "data": {"projectToken": {"projectId": PID}}}),
    _Resp(401, {"errors": [{"message": "Unauthorized"}]}),
    _Resp(403, {}),
    _Resp(200, {"data": {"projectToken": None}}),
    _Resp(200, {"data": None}),
    _Resp(401, ValueError("not json")),  # 401/403은 본문과 무관하게 확정
    _valid(pid="pid-OTHER-project"),  # 다른 프로젝트 토큰
], ids=["200-errors", "200-errors-with-data", "401", "403", "no-projectToken", "data-null",
        "401-bad-json", "other-project"])
def test_invalid_responses_are_false_and_cached(cfg, monkeypatch, resp):
    calls = _install(monkeypatch, [resp, _valid()])
    assert railway_env.token_valid() is False
    assert railway_env.token_valid() is False  # 확정 판정 → 캐시 (재호출 없음)
    assert railway_env.probe() == "invalid"  # 같은 캐시 공유
    assert len(calls) == 1


_TRANSIENT = [
    ConnectionError("network down"), TimeoutError("timed out"),
    _ConnectError("boom"), _ReadTimeout("slow"),
    _Resp(502, {}), _Resp(503, {"errors": [{"message": "Not Authorized"}]}), _Resp(429, {}),
    # 200인데 JSON 아님/딕트 아님 — 프록시·게이트웨이 응답 등, 토큰 판정 근거 없음
    _Resp(200, ValueError("not json")), _Resp(200, ["not", "a", "dict"]),
]
_TRANSIENT_IDS = ["conn", "timeout", "httpx-connect", "httpx-read-timeout", "502",
                  "503-with-errors", "429", "bad-json", "list-json"]


@pytest.mark.parametrize("first", _TRANSIENT, ids=_TRANSIENT_IDS)
def test_transient_failure_is_false_but_not_cached(cfg, monkeypatch, first):
    """네트워크 예외·5xx·429·JSON 아님은 fail closed(False)지만 캐시 안 함 → 다음 호출 재확인."""
    calls = _install(monkeypatch, [first, _valid()])
    assert railway_env.token_valid() is False
    assert railway_env.token_valid() is True
    assert len(calls) == 2


# ---------------------------------------------------------------- probe (3상태)


def test_probe_ok_cached_and_token_valid_agrees(cfg, monkeypatch):
    calls = _install(monkeypatch, [_valid()])
    assert railway_env.probe() == "ok"
    assert railway_env.token_valid() is True  # 같은 캐시 — 재호출 없음
    assert railway_env.probe() == "ok"
    assert len(calls) == 1


@pytest.mark.parametrize("first", _TRANSIENT, ids=_TRANSIENT_IDS)
def test_probe_transient_is_unknown_not_cached(cfg, monkeypatch, first):
    calls = _install(monkeypatch, [first, first, _valid()])
    assert railway_env.probe() == "unknown"
    assert railway_env.probe() == "unknown"  # 캐시 안 함 → 매번 재확인
    assert railway_env.probe() == "ok"  # 장애 해소 → 확정
    assert railway_env.probe() == "ok"  # 이제 캐시
    assert len(calls) == 3


def test_probe_network_error_logs_transient_without_token(cfg, monkeypatch, caplog):
    """네트워크 예외 → 'unknown' + '일시 장애' 로그 (무효로 단정하지 않음), 토큰 미노출."""
    caplog.set_level(logging.DEBUG, logger="src.model_router.railway_env")
    calls = _install(monkeypatch, [_ConnectError(f"connect fail token={TOKEN}")])
    assert railway_env.probe() == "unknown"
    assert railway_env.token_valid() is False
    assert len(calls) == 2
    assert railway_env._token_valid_cache == {}
    assert "네트워크" in caplog.text and "무효" not in caplog.text
    assert TOKEN not in caplog.text and TOKEN[:12] not in caplog.text


def test_probe_5xx_logs_transient_not_revoked(cfg, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="src.model_router.railway_env")
    _install(monkeypatch, [_Resp(502, ValueError("<html>bad gateway</html>"))])
    assert railway_env.probe() == "unknown"
    assert "일시 장애" in caplog.text and "무효" not in caplog.text
    assert "HTTP 502" in caplog.text


@pytest.mark.parametrize("resp", [
    _Resp(200, {"errors": [{"message": "Not Authorized"}], "data": None}),
    _Resp(401, {"errors": [{"message": "Unauthorized"}]}),
    _Resp(403, {}),
    _Resp(200, {"data": {"projectToken": None}}),
    _valid(pid="pid-OTHER-project"),
], ids=["200-not-authorized", "401", "403", "no-projectToken", "other-project"])
def test_probe_invalid_is_cached(cfg, monkeypatch, resp):
    calls = _install(monkeypatch, [resp, _valid()])
    assert railway_env.probe() == "invalid"
    assert railway_env.probe() == "invalid"
    assert len(calls) == 1


def test_probe_missing_config_is_invalid_without_network(monkeypatch):
    for n in ("RAILWAY_PROJECT_ACCESS_TOKEN", "RAILWAY_ACCESS_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", PID)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "sid")
    railway_env.reset_token_cache()
    calls = _install(monkeypatch, [_valid()])
    assert railway_env.probe() == "invalid"
    assert calls == []


def test_force_with_transient_keeps_firm_cache(cfg, monkeypatch):
    """force 재확인이 일시 장애면 'unknown'을 돌려주되 이전 확정 판정 캐시는 덮지 않음."""
    calls = _install(monkeypatch, [_valid(), _ConnectError("boom")])
    assert railway_env.probe() == "ok"
    assert railway_env.probe(force=True) == "unknown"
    assert railway_env.token_valid(force=True) is False
    assert railway_env.probe() == "ok"  # 캐시된 확정 판정 유지
    assert railway_env.token_valid() is True
    assert len(calls) == 3


_ALL_CASES = [_valid(), _valid(pid="pid-OTHER-project"),
              _Resp(200, {"errors": [{"message": "Not Authorized"}], "data": None}),
              _Resp(401, {}), *_TRANSIENT]


@pytest.mark.parametrize("resp", _ALL_CASES)
def test_token_valid_equals_probe_ok(cfg, monkeypatch, resp):
    _install(monkeypatch, [resp])
    state = railway_env.probe(force=True)
    assert state in ("ok", "invalid", "unknown")
    railway_env.reset_token_cache()
    assert railway_env.token_valid(force=True) is (state == "ok")


def test_missing_config_is_false_without_network(monkeypatch):
    for n in ("RAILWAY_PROJECT_ACCESS_TOKEN", "RAILWAY_ACCESS_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", PID)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "sid")
    railway_env.reset_token_cache()
    calls = _install(monkeypatch, [_valid()])
    assert railway_env.token_valid() is False
    assert calls == []


def test_cache_is_per_token(cfg, monkeypatch):
    calls = _install(monkeypatch, [_Resp(200, {"errors": [{"message": "Not Authorized"}]}),
                                   _valid()])
    assert railway_env.token_valid() is False
    # 사용자가 새 토큰으로 교체(재배포 없이 env만 바뀐 경우도) → 새로 확인
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", "tok-fake-NEW-9876543210")
    assert railway_env.token_valid() is True
    assert len(calls) == 2
    # 캐시 key에 토큰 원문 미보관
    assert all(TOKEN not in k for k in railway_env._token_valid_cache)


def test_probe_never_logs_token(cfg, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="src.model_router.railway_env")
    _install(monkeypatch, [_Resp(401, {"errors": [{"message": f"bad token {TOKEN}"}]})])
    assert railway_env.token_valid() is False
    assert "무효" in caplog.text
    assert TOKEN not in caplog.text and TOKEN[:12] not in caplog.text
    caplog.clear()
    railway_env.reset_token_cache()
    _install(monkeypatch, [RuntimeError(f"connect fail token={TOKEN}")])
    assert railway_env.token_valid() is False
    assert TOKEN not in caplog.text and TOKEN[:12] not in caplog.text


def test_available_semantics_unchanged_no_network(cfg, monkeypatch):
    """available()은 설정 존재만 — 네트워크 호출 없음 (Layer D·handler 의미 유지)."""

    class _NoClient:
        def __init__(self, *a, **kw):
            raise AssertionError("available()이 네트워크 호출")

    monkeypatch.setattr(railway_env, "httpx", SimpleNamespace(Client=_NoClient))
    assert railway_env.available() is True


# ---------------------------------------------------------------- scrub → truncate 순서


def _leaks(secret: str, text: str, n: int = 20) -> bool:
    """secret의 n자 이상 연속 조각이 text에 있으면 True."""
    return any(secret[i:i + n] in text for i in range(len(secret) - n + 1))


@pytest.mark.parametrize("prefix", ["", "Invalid value: \"", "x" * 50 + " "],
                         ids=["bare", "quoted", "offset"])
def test_graphql_error_echo_never_leaks_fragment(cfg, monkeypatch, prefix):
    """160자 자르기 전에 scrub — 이전엔 353자 세션의 앞 ~100-150자 조각이 남았다."""
    echo = {"errors": [{"message": f"{prefix}{FAKE_SESSION}\""}], "data": None}
    _install(monkeypatch, [_Resp(400, echo)])
    ok, msg = railway_env.upsert_variable("TG_SESSION_STRING", FAKE_SESSION,
                                          service_ids=["sid-fake-0001"])
    assert not ok
    assert not _leaks(FAKE_SESSION, msg), msg
    assert "‹redacted›" in msg and "HTTP 400" in msg


def test_exception_echo_never_leaks_fragment(cfg, monkeypatch):
    _install(monkeypatch, [ValueError("x " + FAKE_SESSION)])
    ok, msg = railway_env.upsert_variable("TG_SESSION_STRING", FAKE_SESSION,
                                          service_ids=["sid-fake-0001"])
    assert not ok
    assert not _leaks(FAKE_SESSION, msg), msg
    assert "ValueError" in msg and "‹redacted›" in msg


def test_token_echo_in_error_never_leaks_fragment(cfg, monkeypatch):
    long_token = "tok-" + "Zz9" * 70  # 160자 넘는 토큰도 조각 미노출
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", long_token)
    _install(monkeypatch, [_Resp(401, {"errors": [{"message": f"token {long_token} revoked"}]})])
    ok, msg = railway_env.delete_variable("X", service_ids=["sid-fake-0001"])
    assert not ok
    assert not _leaks(long_token, msg), msg


def test_error_message_still_truncated_after_scrub(cfg, monkeypatch):
    echo = {"errors": [{"message": "E" * 1000}]}
    _install(monkeypatch, [_Resp(400, echo)])
    ok, msg = railway_env.upsert_variable("X", "value-fake-123", service_ids=["sid-fake-0001"])
    assert not ok
    assert "E" * 160 in msg and "E" * 161 not in msg
