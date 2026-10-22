"""OpenRouter 401(키 무효) 사용자 안내 문구 — 2026-10-21 키 disable 사고 후속 리뷰(rules/F2).

OpenRouterAuthError는 OpenRouterCreditExhausted 하위 클래스라 기존 크레딧 핸들러가 그대로
잡는다. 그 결과 401에도 '크레딧 부족 / 키 충전 후 재시도'가 떠서 관리자 알림('새 키를
OPENROUTER_API_KEY에 넣고 재배포')과 모순 → 사용자 문구를 summarizer.llm_outage_label /
llm_outage_placeholder로 구분. 장애 placeholder prefix는 category_bots dedup 판별용으로 유지.

네트워크·실제 텔레그램 미사용 (전부 mock). 모듈 재import(다른 테스트의 sys.modules 교체)에
안전하도록 예외 클래스는 피검 모듈이 실제로 참조하는 것을 쓴다.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

AUTH_MARK = "키 무효"
CREDIT_WORDS = ("크레딧", "충전", "토큰 부족", "한도 초과")


def _sm():
    return importlib.import_module("src.summarizer")


def _assert_auth_text(text: str) -> None:
    assert AUTH_MARK in text, text
    for w in CREDIT_WORDS:
        assert w not in text, f"401 안내에 크레딧 문구 '{w}' 금지: {text}"


class _Reply:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)


def _update(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=_Reply().reply_text),
        effective_chat=SimpleNamespace(id=1),
    )


def _replies(update) -> list[str]:
    return update.message.reply_text.__self__.texts


# ------------------------------------------------------------------
# summarizer 헬퍼
# ------------------------------------------------------------------
def test_llm_outage_label_distinguishes_auth_and_credit():
    sm = _sm()
    auth = sm.OpenRouterAuthError("401")
    credit = sm.OpenRouterCreditExhausted("402")
    assert isinstance(auth, sm.OpenRouterCreditExhausted), "하위 클래스 관계 유지 (기존 단락)"
    assert sm.llm_outage_label(auth) == "OpenRouter 키 무효(401) — 관리자 키 교체 필요"
    _assert_auth_text(sm.llm_outage_label(auth))
    assert sm.llm_outage_label(credit) == "OpenRouter 크레딧 부족"
    # 호출부 기존 크레딧 문구 유지용 override — 401엔 적용 안 됨
    assert sm.llm_outage_label(credit, "OpenRouter 토큰 부족") == "OpenRouter 토큰 부족"
    _assert_auth_text(sm.llm_outage_label(auth, "OpenRouter 토큰 부족"))


def test_outage_placeholder_keeps_dedup_prefix():
    sm = _sm()
    auth_ph = sm.llm_outage_placeholder(sm.OpenRouterAuthError("401"))
    credit_ph = sm.llm_outage_placeholder(sm.OpenRouterCreditExhausted("402"))
    _assert_auth_text(auth_ph)
    assert credit_ph == "(요약 실패: OpenRouter 토큰 부족)", "크레딧 placeholder 문구 불변"
    for ph in (auth_ph, credit_ph):
        assert ph.startswith(sm.LLM_OUTAGE_PLACEHOLDER_PREFIX)
        assert sm.is_llm_outage_placeholder(ph)
    # 일반 실패 placeholder·비문자열은 장애 아님 (기존대로 발송·seen 처리)
    for s in ("(요약 실패: ValueError('x'))", "(LLM 빈 응답이 3회 연속 — 요약 생략)",
              "정상 요약", "", None, 123):
        assert not sm.is_llm_outage_placeholder(s)


# ------------------------------------------------------------------
# intent_router.classify — 실제 chat_with_retry 경유 401
# ------------------------------------------------------------------
@pytest.fixture
def ir_env(monkeypatch):
    from src import intent_router as ir
    g = ir.chat_with_retry.__globals__  # ir이 실제로 쓰는 summarizer 모듈 namespace
    g["reset_auth_breaker"]()
    g["set_auth_alert_hook"](None)
    monkeypatch.setitem(g, "_record_health", lambda *a, **k: None)
    yield ir, g
    g["reset_auth_breaker"]()


def _status_err(g, status: int, msg: str):
    base = g["APIStatusError"]

    class _StatusErr(base):  # 실제 openai/conftest 더미 어느 쪽이든 동작
        def __init__(self) -> None:
            Exception.__init__(self, msg)
            self.status_code = status
            self.body = {"error": {"message": msg, "code": status}}

    return _StatusErr()


class _FakeClient:
    def __init__(self, exc: Exception) -> None:
        self.calls = 0
        self.exc = exc
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        raise self.exc


def test_intent_router_real_401_says_key_invalid(ir_env, monkeypatch):
    ir, g = ir_env
    client = _FakeClient(_status_err(g, 401, "User not found."))
    monkeypatch.setattr(ir, "get_client", lambda: client)
    intent = asyncio.run(ir.classify("반도체 소재 아이디어 찾아줘"))
    assert intent.intent == "unknown"
    assert client.calls == 1, "401은 재시도 없음"
    _assert_auth_text(intent.clarification)
    assert "명시 명령 사용" in intent.clarification
    # breaker 열린 상태의 다음 호출(네트워크 없이 raise)도 같은 안내
    intent2 = asyncio.run(ir.classify("2차전지 밸류체인"))
    assert client.calls == 1
    _assert_auth_text(intent2.clarification)


def test_intent_router_credit_text_unchanged(ir_env, monkeypatch):
    ir, g = ir_env
    monkeypatch.setattr(ir, "get_client", lambda: object())

    def _raise(*a, **k):
        raise g["OpenRouterCreditExhausted"]("402 insufficient credits")

    monkeypatch.setattr(ir, "chat_with_retry", _raise)
    intent = asyncio.run(ir.classify("반도체 소재 아이디어 찾아줘"))
    assert intent.clarification == "OpenRouter 크레딧 부족 — 명시 명령 사용"


# ------------------------------------------------------------------
# bot_worker._answer_question
# ------------------------------------------------------------------
@pytest.mark.parametrize("auth", [True, False])
def test_bot_worker_answer_question_outage_text(monkeypatch, auth):
    import src.bot_worker as bw
    sm = _sm()  # _answer_question은 호출 시점에 src.summarizer를 lazy import
    exc = sm.OpenRouterAuthError("401") if auth else sm.OpenRouterCreditExhausted("402")

    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(sm, "get_client", lambda: object())
    monkeypatch.setattr(sm, "chat_with_retry", _raise)
    upd = _update("금리 인하 수혜주는?")
    asyncio.run(bw._answer_question(upd, "금리 인하 수혜주는?"))
    texts = _replies(upd)
    assert len(texts) == 1
    if auth:
        _assert_auth_text(texts[0])
    else:
        assert texts[0] == "⚠️ OpenRouter 크레딧 부족"


# ------------------------------------------------------------------
# category_bots.industry_on_demand — resolve_industry 401
# ------------------------------------------------------------------
@pytest.mark.parametrize("auth", [True, False])
def test_industry_on_demand_outage_text(monkeypatch, auth):
    from src import category_bots as cb
    exc = cb.OpenRouterAuthError("401") if auth else cb.OpenRouterCreditExhausted("402")

    def _raise(name):
        raise exc

    monkeypatch.setattr(cb, "_is_authorized", lambda update, env_key: True)
    monkeypatch.setattr(cb, "resolve_industry", _raise)
    upd = _update("반도체")
    ctx = SimpleNamespace(args=["반도체"], bot=object())
    asyncio.run(cb.industry_on_demand(upd, ctx))
    texts = _replies(upd)
    assert len(texts) == 1
    if auth:
        _assert_auth_text(texts[0])
        assert "산업명 해석 불가" in texts[0]
    else:
        assert texts[0] == "❌ OpenRouter 한도 초과 — 산업명 해석 불가. 키 충전 후 재시도."


# ------------------------------------------------------------------
# bot_helpers._summarize_pdfs_parallel → category_bots 장애 판별
# ------------------------------------------------------------------
def test_parallel_summary_auth_placeholder_detected_as_outage(monkeypatch, tmp_path):
    from src import bot_helpers as bh
    from src import category_bots as cb
    sm = _sm()  # _summarize_pdfs_parallel은 호출 시점에 lazy import

    def _summ(client, path):
        if path.name == "ok.pdf":
            return SimpleNamespace(summary_text="정상 요약")
        raise sm.OpenRouterAuthError("401")

    monkeypatch.setattr(sm, "get_client", lambda: object())
    monkeypatch.setattr(sm, "summarize_pdf_short", _summ)
    paths = [tmp_path / "ok.pdf", tmp_path / "dead.pdf"]
    out = asyncio.run(bh._summarize_pdfs_parallel(paths, short_summary=True, label="t"))
    assert out[0] == "정상 요약"
    _assert_auth_text(out[1])
    # breaker 닫힘(다른 경로로 이미 해제)이어도 placeholder만으로 장애 판별 → 미발송·미마킹
    monkeypatch.setattr(sm, "auth_breaker_open", lambda: False)
    assert cb._is_llm_outage(out)
    assert not cb._is_llm_outage(["정상 요약", "(요약 실패: ValueError('x'))"])


# ------------------------------------------------------------------
# deepdive._summarize_and_send
# ------------------------------------------------------------------
def test_deepdive_summary_auth_placeholder(monkeypatch):
    from src.deepdive import handler as dh
    from src.deepdive import prompts as prompt_loader
    sm = _sm()  # _summarize_and_send는 호출 시점에 `from src import summarizer`

    def _raise(*a, **k):
        raise sm.OpenRouterAuthError("401")

    monkeypatch.setattr(prompt_loader, "load", lambda name: "sys")
    monkeypatch.setattr(sm, "get_client", lambda: object())
    monkeypatch.setattr(sm, "chat_with_retry", _raise)
    sent: list[str] = []

    class _Bot:
        async def send_message(self, chat_id=None, text="", **kw):
            sent.append(text)

    asyncio.run(dh._summarize_and_send(
        _Bot(), 1, None, "business", "📘 헤더", extra_context="보조 자료",
    ))
    assert len(sent) == 1
    body = sent[0].split("\n\n", 1)[1]
    _assert_auth_text(body)
    assert sm.is_llm_outage_placeholder(body)


# ------------------------------------------------------------------
# main.py (wisereport CLI) — PDF만 발송 note / 종합 요약 문구
# ------------------------------------------------------------------
@pytest.fixture
def main_env(monkeypatch, tmp_path):
    from src import main as m
    st = SimpleNamespace(notes=[], reports=[], m=m)
    pdf = tmp_path / "r1.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    class _WR:
        def __init__(self, **kw) -> None: pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ensure_logged_in(self): pass
        def list_reports(self, **kw): return [object()]
        def download_reports(self, items, d): return [pdf]

    monkeypatch.setenv("WISEREPORT_ID", "fake-id")
    monkeypatch.setenv("WISEREPORT_PW", "fake-pw")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fake")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("SUMMARY_DIR", str(tmp_path / "sum"))
    monkeypatch.setenv("STORAGE_STATE", "")
    monkeypatch.delenv("DISABLE_TELEGRAM", raising=False)
    monkeypatch.setattr(m, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(m, "setup_logging", lambda verbose: None)
    monkeypatch.setattr(m, "parse_args", lambda: argparse.Namespace(
        company="테스트", ticker="005930", top=1, sort="latest", headed=False,
        no_summarize=False, no_telegram=False, slow_mo=0, verbose=False,
    ))
    monkeypatch.setattr(m, "WisereportClient", _WR)
    monkeypatch.setattr(m, "get_client", lambda: object())
    monkeypatch.setattr(m, "telegram_send_pdfs_only",
                        lambda **kw: st.notes.append(kw["note"]))

    def _write_report(out_dir, company, individual, combined):
        st.reports.append(combined)
        return Path(out_dir) / "report.md"

    monkeypatch.setattr(m, "write_report", _write_report)
    return st


@pytest.mark.parametrize("auth", [True, False])
def test_main_pdf_only_note(main_env, monkeypatch, auth):
    m = main_env.m
    exc = m.OpenRouterCreditExhausted("402")
    if auth:
        exc = m.llm_outage_label.__globals__["OpenRouterAuthError"]("401")

    def _raise(client, path):
        raise exc

    monkeypatch.setattr(m, "summarize_pdf", _raise)
    assert m.main() == 0
    assert len(main_env.notes) == 1
    head = main_env.notes[0].split("\n", 1)[0]
    if auth:
        _assert_auth_text(head)
    else:
        assert head == "⚠️ OpenRouter 토큰 부족 — 요약 생략."


@pytest.mark.parametrize("auth", [True, False])
def test_main_combined_outage_text(main_env, monkeypatch, auth):
    m = main_env.m
    sm_globals = m.llm_outage_label.__globals__
    exc = sm_globals["OpenRouterAuthError"]("401") if auth else m.OpenRouterCreditExhausted("402")

    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(m, "summarize_pdf", lambda client, path: SimpleNamespace(
        summary_text="개별 요약", pdf_path=path))
    monkeypatch.setattr(m, "summarize_combined", _raise)
    monkeypatch.setattr(m, "telegram_send_all", lambda **kw: None, raising=False)
    monkeypatch.setenv("DISABLE_TELEGRAM", "true")
    m.main()
    assert len(main_env.reports) == 1
    combined = main_env.reports[0]
    if auth:
        _assert_auth_text(combined)
    else:
        assert "OpenRouter 토큰 부족" in combined
    assert "개별 요약 1건만 포함" in combined
