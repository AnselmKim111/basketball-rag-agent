"""전송 계층 자격증명 노출 회귀 테스트 (2026-10-21 Railway env 유출 사고 후속).

검증 계약:
  1) 텔레그램/DART는 TLS 검증을 끄지 않는다 (verify=False 금지, DART는 비상 스위치만).
  2) Finnhub 키는 쿼리 token= 이 아니라 X-Finnhub-Token 헤더로 나간다.
  3) FMP·AlphaVantage·DART(키가 쿼리에 남을 수밖에 없는 곳)와 텔레그램(URL에 토큰)은
     401/5xx·네트워크 예외가 나도 로그에 키가 찍히지 않는다.

HTTP 계층은 가짜 httpx/requests로 대체 — 가짜 예외는 실제 httpx/requests처럼
문자열에 전체 URL(쿼리 키 포함)을 담아, str(e)·log.exception을 쓰면 테스트가 깨진다.
로그 검사는 formatter를 거치지 않은 record 원문(getMessage + traceback)으로 —
전역 마스킹 formatter가 있어도 소스 단계 누출을 잡기 위함.
"""
from __future__ import annotations

import json
import logging
import sys
import types
from urllib.parse import urlencode

import pytest
import requests

from src import telegram_sender
from src.deepdive import dart_client
from src.earnings import transcript_source
from src.report.data import (
    fetch_earnings,
    fetch_earnings_surprise,
    fetch_estimate_revisions,
    fetch_ipo,
    fetch_market_caps,
)
from src.us_screener import earnings_proximity

# 가짜 값 — 실제 키 형식과 무관한 식별용 문자열
FAKE_TG_TOKEN = "123456789:FAKEtokenFAKEtokenFAKEtokenFAKE0000"
FAKE_FMP = "fake-fmp-key-0000aaaa"
FAKE_FINNHUB = "fake-finnhub-key-0000bbbb"
FAKE_AV = "fake-alphavantage-key-0000cccc"
FAKE_DART = "fake-dart-crtfc-key-0000dddd"
ALL_FAKE = (FAKE_TG_TOKEN, FAKE_FMP, FAKE_FINNHUB, FAKE_AV, FAKE_DART)


# ------------------------------------------------------------------
# 가짜 HTTP 계층
# ------------------------------------------------------------------
class FakeHTTPError(Exception):
    pass


class FakeHTTPStatusError(FakeHTTPError):
    def __init__(self, message, *, request=None, response=None):
        super().__init__(message)
        self.request = request
        self.response = response


class FakeConnectError(FakeHTTPError):
    pass


def _full_url(url: str, params) -> str:
    if not params:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(params)


class FakeResponse:
    def __init__(self, status: int, url: str = "", body=None, content: bytes | None = None,
                 headers: dict | None = None):
        self.status_code = status
        self.url = url
        self._body = body
        if content is None:
            content = json.dumps(body).encode() if body is not None else b""
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            kind = "Client" if self.status_code < 500 else "Server"
            # 실제 httpx와 동일하게 전체 URL(쿼리 키 포함)을 메시지에 담음
            raise FakeHTTPStatusError(
                f"{kind} error '{self.status_code}' for url '{self.url}'", response=self,
            )
        return self


def make_fake_httpx(status: int = 500, body=None, content: bytes | None = None,
                    headers: dict | None = None, raise_connect: bool = False):
    """status 응답(또는 URL 담은 연결 예외)을 돌려주는 가짜 httpx 모듈. 호출은 .calls에 기록."""
    mod = types.ModuleType("httpx")
    mod.calls = []
    mod.clients = []
    mod.HTTPError = FakeHTTPError
    mod.HTTPStatusError = FakeHTTPStatusError
    mod.ConnectError = FakeConnectError
    mod.RequestError = FakeHTTPError
    mod.Timeout = lambda *a, **kw: ("timeout", a, tuple(sorted(kw.items())))

    def _do(method, url, params=None, headers=None, **kw):
        full = _full_url(url, params)
        mod.calls.append({
            "method": method, "url": url, "full_url": full,
            "params": dict(params or {}), "headers": dict(headers or {}), "kwargs": kw,
        })
        if raise_connect:
            raise FakeConnectError(f"connection refused while requesting {full}")
        return FakeResponse(status, full, body=body, content=content, headers=headers_resp)

    headers_resp = headers
    mod.get = lambda url, **kw: _do("GET", url, **kw)
    mod.post = lambda url, **kw: _do("POST", url, **kw)

    class Client:
        def __init__(self, **kw):
            mod.clients.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            return _do("GET", url, **kw)

    mod.Client = Client
    return mod


class FakeRequests:
    """requests.get 대체 — FMP는 실제 requests처럼 URL째 담은 ConnectionError, 그 외는 status 응답."""

    def __init__(self, status: int = 401, body=None, fmp_connect_error: bool = False):
        self.status = status
        self.body = body
        self.fmp_connect_error = fmp_connect_error
        self.calls: list[dict] = []

    def __call__(self, url, params=None, headers=None, timeout=None, **kw):
        full = _full_url(url, params)
        self.calls.append({"url": url, "full_url": full, "params": dict(params or {}),
                           "headers": dict(headers or {}), "kwargs": kw})
        if self.fmp_connect_error and "financialmodelingprep" in url:
            path = full.split("financialmodelingprep.com", 1)[1]
            raise requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='financialmodelingprep.com', port=443): "
                f"Max retries exceeded with url: {path}"
            )
        return FakeResponse(self.status, full, body=self.body)


def _log_text(caplog) -> str:
    """formatter 거치지 않은 record 원문 — 메시지 + traceback + stack."""
    fmt = logging.Formatter()
    parts = []
    for rec in caplog.records:
        parts.append(rec.getMessage())
        if rec.exc_info:
            parts.append(fmt.formatException(rec.exc_info))
        if rec.stack_info:
            parts.append(rec.stack_info)
    return "\n".join(parts)


def _assert_no_secret(caplog):
    text = _log_text(caplog)
    for secret in ALL_FAKE:
        assert secret not in text, f"로그에 키 노출: {secret!r}\n{text}"
    # 텔레그램 토큰의 비밀 부분(콜론 뒤)만으로도 안 됨
    assert FAKE_TG_TOKEN.split(":", 1)[1] not in text


@pytest.fixture(autouse=True)
def _env(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    for name in ("FMP_API_KEY", "FINNHUB_API_KEY", "ALPHA_VANTAGE_KEY", "DART_API_KEY",
                 "TELEGRAM_BOT_TOKEN", "DART_TLS_VERIFY", "API_NINJAS_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_fake_errors_really_carry_the_key():
    """테스트 전제 확인 — 가짜 예외 문자열에 키가 실제로 들어간다 (str(e) 쓰면 걸림)."""
    fake = make_fake_httpx(status=500)
    r = fake.get("https://x.example/q", params={"apikey": FAKE_FMP})
    with pytest.raises(FakeHTTPStatusError) as ei:
        r.raise_for_status()
    assert FAKE_FMP in str(ei.value)


# ------------------------------------------------------------------
# 1) 텔레그램 — verify=False 제거 + 토큰 URL 비노출
# ------------------------------------------------------------------
def test_telegram_calls_keep_tls_verification(monkeypatch, tmp_path):
    fake = make_fake_httpx(status=200, body={"ok": True, "result": []})
    monkeypatch.setattr(telegram_sender, "httpx", fake)
    doc = tmp_path / "r.pdf"
    doc.write_bytes(b"%PDF-1.4 test")

    assert telegram_sender.send_message(FAKE_TG_TOKEN, "1", "hello") is True
    assert telegram_sender.send_document(FAKE_TG_TOKEN, "1", doc, caption="c") is True
    assert telegram_sender.resolve_chat_id(FAKE_TG_TOKEN, "@someone") is None
    assert len(fake.calls) == 3
    for call in fake.calls:
        assert call["kwargs"].get("verify", True) is not False


@pytest.mark.parametrize("status", [401, 500])
def test_telegram_http_errors_log_status_not_token(monkeypatch, tmp_path, caplog, status):
    desc = "Unauthorized" if status == 401 else "Internal Server Error"
    fake = make_fake_httpx(status=status, body={"ok": False, "error_code": status,
                                                "description": desc})
    monkeypatch.setattr(telegram_sender, "httpx", fake)
    doc = tmp_path / "r.pdf"
    doc.write_bytes(b"%PDF-1.4 test")

    assert telegram_sender.send_message(FAKE_TG_TOKEN, "1", "hello") is False
    assert telegram_sender.send_document(FAKE_TG_TOKEN, "1", doc) is False
    assert telegram_sender.resolve_chat_id(FAKE_TG_TOKEN, "@someone") is None

    _assert_no_secret(caplog)
    text = _log_text(caplog)
    assert f"status={status}" in text
    assert desc in text
    assert "r.pdf" in text


def test_telegram_network_error_logs_type_only(monkeypatch, tmp_path, caplog):
    fake = make_fake_httpx(raise_connect=True)
    monkeypatch.setattr(telegram_sender, "httpx", fake)
    doc = tmp_path / "r.pdf"
    doc.write_bytes(b"%PDF-1.4 test")

    assert telegram_sender.send_message(FAKE_TG_TOKEN, "1", "hello") is False
    assert telegram_sender.send_document(FAKE_TG_TOKEN, "1", doc) is False
    assert telegram_sender.resolve_chat_id(FAKE_TG_TOKEN, "@someone") is None
    _assert_no_secret(caplog)
    assert "FakeConnectError" in _log_text(caplog)


def test_telegram_description_echoing_token_is_scrubbed(monkeypatch, caplog):
    fake = make_fake_httpx(status=400, body={"ok": False,
                                             "description": f"bad token {FAKE_TG_TOKEN}"})
    monkeypatch.setattr(telegram_sender, "httpx", fake)
    assert telegram_sender.send_message(FAKE_TG_TOKEN, "1", "x") is False
    _assert_no_secret(caplog)


# ------------------------------------------------------------------
# 2) DART — TLS 검증 기본 ON + 비상 스위치 + crtfc_key 비노출
# ------------------------------------------------------------------
def test_dart_tls_verify_default_on(monkeypatch, tmp_path):
    monkeypatch.setenv("DART_API_KEY", FAKE_DART)
    fake = make_fake_httpx(status=200, body={"status": "000", "list": []})
    monkeypatch.setattr(dart_client, "httpx", fake)
    monkeypatch.setattr(dart_client, "_CORP_MAP_CACHE", None)

    assert dart_client._get("list.json", {"corp_code": "00126380"}) is not None
    dart_client._load_corp_map()  # 200이지만 zip 아님 → {} (verify 인자만 확인)
    dart_client.download_report_archive("20261021000001", tmp_path)
    assert len(fake.clients) == 3
    for kw in fake.clients:
        assert kw.get("verify") is True


def test_dart_tls_escape_hatch_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("DART_API_KEY", FAKE_DART)
    monkeypatch.setenv("DART_TLS_VERIFY", "0")
    monkeypatch.setattr(dart_client, "_TLS_OFF_WARNED", False)
    fake = make_fake_httpx(status=200, body={"status": "000"})
    monkeypatch.setattr(dart_client, "httpx", fake)

    dart_client._get("list.json", {})
    dart_client._get("list.json", {})
    assert [kw.get("verify") for kw in fake.clients] == [False, False]
    warns = [r for r in caplog.records if "DART_TLS_VERIFY=0" in r.getMessage()]
    assert len(warns) == 1


@pytest.mark.parametrize("status", [401, 500])
def test_dart_http_errors_hide_crtfc_key(monkeypatch, tmp_path, caplog, status):
    monkeypatch.setenv("DART_API_KEY", FAKE_DART)
    fake = make_fake_httpx(status=status, body={"status": "010"})
    monkeypatch.setattr(dart_client, "httpx", fake)
    monkeypatch.setattr(dart_client, "_CORP_MAP_CACHE", None)

    assert dart_client._get("list.json", {"corp_code": "00126380"}) is None
    assert dart_client._load_corp_map() == {}
    assert dart_client.download_report_archive("20261021000001", tmp_path) is None
    # 키가 실제로 쿼리로 나갔는지 (그래야 노출 검사가 의미 있음)
    assert all(c["params"].get("crtfc_key") == FAKE_DART for c in fake.calls)

    _assert_no_secret(caplog)
    text = _log_text(caplog)
    assert f"status={status}" in text
    assert "list.json" in text and "corpCode.xml" in text and "document.xml" in text


def test_dart_network_error_hides_crtfc_key(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("DART_API_KEY", FAKE_DART)
    fake = make_fake_httpx(raise_connect=True)
    monkeypatch.setattr(dart_client, "httpx", fake)
    monkeypatch.setattr(dart_client, "_CORP_MAP_CACHE", None)

    assert dart_client._get("list.json", {}) is None
    assert dart_client._load_corp_map() == {}
    assert dart_client.download_report_archive("20261021000001", tmp_path) is None
    _assert_no_secret(caplog)


def test_dart_document_xml_error_body_not_logged_raw(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("DART_API_KEY", FAKE_DART)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><result><status>020</status>'
        f'<message>요청 제한 초과</message><echo>{FAKE_DART}</echo></result>'
    ).encode()
    fake = make_fake_httpx(status=200, content=body,
                           headers={"content-type": "application/xml"})
    monkeypatch.setattr(dart_client, "httpx", fake)

    assert dart_client.download_report_archive("20261021000001", tmp_path) is None
    _assert_no_secret(caplog)
    text = _log_text(caplog)
    assert "status=020" in text and "요청 제한 초과" in text


# ------------------------------------------------------------------
# 3) Finnhub — X-Finnhub-Token 헤더 (쿼리 token= 금지)
# ------------------------------------------------------------------
def _assert_finnhub_header_calls(calls):
    assert calls, "Finnhub 호출이 없음"
    for c in calls:
        assert c["headers"].get("X-Finnhub-Token") == FAKE_FINNHUB
        assert "token=" not in c["full_url"]
        assert FAKE_FINNHUB not in c["full_url"]
        assert "token" not in c["params"]


@pytest.mark.parametrize("status", [401, 500])
def test_finnhub_earnings_surprise_uses_header(monkeypatch, caplog, status):
    monkeypatch.setenv("FINNHUB_API_KEY", FAKE_FINNHUB)
    fake = make_fake_httpx(status=status, body={"error": "Invalid API key"})
    monkeypatch.setitem(sys.modules, "httpx", fake)

    assert fetch_earnings_surprise.fetch_finnhub_calendar(days_back=3) == []
    _assert_finnhub_header_calls(fake.calls)
    _assert_no_secret(caplog)


@pytest.mark.parametrize("status", [401, 500])
def test_finnhub_ipo_uses_header(monkeypatch, caplog, status):
    monkeypatch.setenv("FINNHUB_API_KEY", FAKE_FINNHUB)
    fake = make_fake_httpx(status=status, body={"error": "Invalid API key"})
    monkeypatch.setitem(sys.modules, "httpx", fake)

    assert fetch_ipo.fetch_ipo_calendar() == []
    _assert_finnhub_header_calls(fake.calls)
    _assert_no_secret(caplog)


@pytest.mark.parametrize("status", [401, 500])
def test_finnhub_estimate_revisions_uses_header(monkeypatch, caplog, status):
    monkeypatch.setenv("FINNHUB_API_KEY", FAKE_FINNHUB)
    fake = make_fake_httpx(status=status, body={"error": "Invalid API key"})
    monkeypatch.setitem(sys.modules, "httpx", fake)
    monkeypatch.setattr(fetch_estimate_revisions, "_FORBIDDEN_PATHS", set())

    assert fetch_estimate_revisions.fetch_recommendation_trend("AAPL", FAKE_FINNHUB) is None
    assert fetch_estimate_revisions.fetch_price_target("AAPL", FAKE_FINNHUB) is None
    assert fetch_estimate_revisions.batch_revisions(["AAPL"]) == {}
    _assert_finnhub_header_calls(fake.calls)
    assert all("symbol=AAPL" in c["full_url"] for c in fake.calls)
    _assert_no_secret(caplog)


def test_finnhub_earnings_fallback_uses_header(monkeypatch, caplog):
    monkeypatch.setenv("FMP_API_KEY", FAKE_FMP)
    monkeypatch.setenv("FINNHUB_API_KEY", FAKE_FINNHUB)
    past_row = {"symbol": "AAA", "date": "2026-10-20", "epsActual": 1.1,
                "epsEstimated": 1.0, "revenueActual": 5e9}
    monkeypatch.setattr(fetch_earnings, "_calendar",
                        lambda s, e, key, step_days=14: [past_row])
    fake_req = FakeRequests(status=401, body={"error": "Invalid API key"})
    monkeypatch.setattr(requests, "get", fake_req)

    out = fetch_earnings.fetch_earnings_momentum()
    assert out is not None and out["upcoming"] == []
    _assert_finnhub_header_calls(fake_req.calls)
    assert set(fake_req.calls[0]["params"]) == {"from", "to"}
    _assert_no_secret(caplog)


# ------------------------------------------------------------------
# 4) FMP (쿼리 키 유지) — 실패 로그엔 상태코드 + 경로만
# ------------------------------------------------------------------
@pytest.mark.parametrize("status", [401, 500])
def test_fmp_calendar_window_hides_key(monkeypatch, caplog, status):
    fake = make_fake_httpx(status=status, body={"Error Message": "Invalid API KEY"})
    monkeypatch.setitem(sys.modules, "httpx", fake)

    assert fetch_earnings._calendar_window("2026-10-01", "2026-10-14", FAKE_FMP) == []
    assert len(fake.calls) == 2  # 재시도 1회 유지
    assert all(FAKE_FMP in c["full_url"] for c in fake.calls)  # 키는 여전히 쿼리로
    _assert_no_secret(caplog)
    text = _log_text(caplog)
    assert "/earnings-calendar" in text and f"status={status}" in text


def test_fmp_calendar_network_error_hides_key(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "httpx", make_fake_httpx(raise_connect=True))
    assert fetch_earnings._calendar_window("2026-10-01", "2026-10-14", FAKE_FMP) == []
    _assert_no_secret(caplog)


@pytest.mark.parametrize("status", [401, 500])
def test_earnings_proximity_hides_fmp_key(monkeypatch, caplog, status):
    monkeypatch.setenv("FMP_API_KEY", FAKE_FMP)
    monkeypatch.setitem(sys.modules, "httpx", make_fake_httpx(status=status))
    assert earnings_proximity.fetch_upcoming_earnings({"NVDA"}, days_fwd=7) == {}
    _assert_no_secret(caplog)


def test_earnings_proximity_unexpected_error_hides_key(monkeypatch, caplog):
    monkeypatch.setenv("FMP_API_KEY", FAKE_FMP)

    def _boom(*a, **kw):
        raise RuntimeError(f"boom url=https://x/earnings-calendar?apikey={FAKE_FMP}")

    monkeypatch.setattr(earnings_proximity, "_calendar", _boom)
    assert earnings_proximity.fetch_upcoming_earnings({"NVDA"}, days_fwd=7) == {}
    _assert_no_secret(caplog)
    assert "RuntimeError" in _log_text(caplog)


@pytest.mark.parametrize("status", [401, 500])
def test_market_caps_fmp_and_finnhub(monkeypatch, caplog, status):
    monkeypatch.setenv("FMP_API_KEY", FAKE_FMP)
    monkeypatch.setenv("FINNHUB_API_KEY", FAKE_FINNHUB)
    monkeypatch.setattr(fetch_market_caps, "_US_CACHE", {})
    monkeypatch.setattr(fetch_market_caps, "_US_CACHE_TS", 0.0)
    fake_req = FakeRequests(status=status, body={"error": "x"})
    monkeypatch.setattr(requests, "get", fake_req)

    assert fetch_market_caps.fetch_us_market_caps(["AAPL"]) == {}
    fmp_calls = [c for c in fake_req.calls if "financialmodelingprep" in c["url"]]
    fh_calls = [c for c in fake_req.calls if "finnhub" in c["url"]]
    assert len(fmp_calls) == 1 and fmp_calls[0]["params"].get("apikey") == FAKE_FMP
    _assert_finnhub_header_calls(fh_calls)
    _assert_no_secret(caplog)


def test_market_caps_fmp_connection_error_hides_key(monkeypatch, caplog):
    monkeypatch.setenv("FMP_API_KEY", FAKE_FMP)
    monkeypatch.setenv("FINNHUB_API_KEY", FAKE_FINNHUB)
    monkeypatch.setattr(fetch_market_caps, "_US_CACHE", {})
    monkeypatch.setattr(fetch_market_caps, "_US_CACHE_TS", 0.0)
    fake_req = FakeRequests(status=500, fmp_connect_error=True)
    monkeypatch.setattr(requests, "get", fake_req)

    assert fetch_market_caps.fetch_us_market_caps(["AAPL", "MSFT"]) == {}
    _assert_no_secret(caplog)
    text = _log_text(caplog)
    assert "ConnectionError" in text and "/quote" in text


# ------------------------------------------------------------------
# 5) 어닝콜 전문 — AlphaVantage·FMP (쿼리 키 유지)
# ------------------------------------------------------------------
@pytest.mark.parametrize("status", [401, 402, 500])
def test_alpha_vantage_errors_hide_key(monkeypatch, caplog, status):
    monkeypatch.setenv("ALPHA_VANTAGE_KEY", FAKE_AV)
    fake = make_fake_httpx(status=status, body={"Error Message": "x"})
    monkeypatch.setattr(transcript_source, "httpx", fake)

    assert transcript_source._alpha_vantage("AAPL", 2026, 3) is None
    assert fake.calls[0]["params"]["apikey"] == FAKE_AV
    _assert_no_secret(caplog)
    text = _log_text(caplog)
    assert "/query" in text and f"status={status}" in text


def test_alpha_vantage_info_message_echoing_key_is_scrubbed(monkeypatch, caplog):
    monkeypatch.setenv("ALPHA_VANTAGE_KEY", FAKE_AV)
    fake = make_fake_httpx(status=200, body={
        "Information": f"rate limit reached for apikey {FAKE_AV}; upgrade to premium",
    })
    monkeypatch.setattr(transcript_source, "httpx", fake)

    assert transcript_source._alpha_vantage("AAPL", 2026, 3) is None
    _assert_no_secret(caplog)
    text = _log_text(caplog)
    assert "Information" in text and "rate limit reached" in text


@pytest.mark.parametrize("status", [401, 402, 500])
def test_fmp_transcript_errors_hide_key(monkeypatch, caplog, status):
    monkeypatch.setenv("FMP_API_KEY", FAKE_FMP)
    fake = make_fake_httpx(status=status, body={"Error Message": "x"})
    monkeypatch.setattr(transcript_source, "httpx", fake)

    assert transcript_source._fmp("AAPL", 2026, 3) is None
    assert fake.calls[0]["params"]["apikey"] == FAKE_FMP
    _assert_no_secret(caplog)
    assert f"{status}" in _log_text(caplog)


def test_transcript_network_errors_hide_keys(monkeypatch, caplog):
    monkeypatch.setenv("ALPHA_VANTAGE_KEY", FAKE_AV)
    monkeypatch.setenv("FMP_API_KEY", FAKE_FMP)
    monkeypatch.setattr(transcript_source, "httpx", make_fake_httpx(raise_connect=True))

    assert transcript_source._alpha_vantage("AAPL", 2026, 3) is None
    assert transcript_source._fmp("AAPL", 2026, 3) is None
    _assert_no_secret(caplog)
