"""OpenRouter 401(키 무효) 사용자 안내 — idea_bot / deep_research / earnings_bot 호출부.

2026-10-21 키 disable 사고 후속 재리뷰(rules/F2 잔여): OpenRouterAuthError는
OpenRouterCreditExhausted 하위 클래스라 아래 핸들러가 401도 그대로 잡아 '한도 초과 / 키 충전 /
결제 잔액 충전 / 크레딧 부족'을 띄웠고, 어닝봇은 '입력을 이해하지 못했습니다'로 사용자 탓을 했다.
관리자 알림('새 키를 OPENROUTER_API_KEY에 넣고 재배포')과 모순 → summarizer.llm_outage_label로 구분.
  - idea_bot /curate resolve_industry · curate thesis · 아이디어 파이프라인 최상위
  - deep_research 웹 축 (out.error → '❌ 모든 데이터 축 실패 … web: …')
  - earnings_bot _run_pipeline 0단계 파싱 (watch 경로 raise_outage=True는 안내 대신 raise)

크레딧/한도(402·429) 문구는 기존 그대로여야 한다 (회귀 방지).
네트워크·실제 텔레그램 미사용 (전부 mock). 다른 테스트가 src.summarizer를 sys.modules에서
교체·재import할 수 있어, 예외 클래스는 피검 코드가 실제로 참조하는 모듈에서 조회한다.
"""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

AUTH_MARK = "키 무효"
CREDIT_WORDS = ("크레딧", "충전", "토큰 부족", "한도 초과", "한도 상향")


def _sm():
    """호출 시점의 src.summarizer — idea_bot·earnings_bot은 핸들러 안에서 lazy import."""
    return importlib.import_module("src.summarizer")


def _exc(sm, auth: bool) -> Exception:
    return sm.OpenRouterAuthError("401 User not found.") if auth else \
        sm.OpenRouterCreditExhausted("402 insufficient credits")


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
    r = _Reply()
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=r.reply_text, _r=r),
        effective_chat=SimpleNamespace(id=1),
    )


def _recorder(monkeypatch, module, name: str = "send_text_chunked") -> list[str]:
    sent: list[str] = []

    async def _send(bot, chat_id, text, **kw):
        sent.append(text)

    monkeypatch.setattr(module, name, _send)
    return sent


# ======================================================================
# idea_bot
# ======================================================================
@pytest.fixture
def ib(monkeypatch):
    from src import idea_bot
    monkeypatch.setattr(idea_bot, "is_authorized", lambda update, env: True)
    yield idea_bot
    idea_bot.CURRENT_IDEA = None


@pytest.mark.parametrize("auth", [True, False])
def test_curate_resolve_industry_outage_text(ib, monkeypatch, auth):
    exc = _exc(_sm(), auth)

    def _raise(q):
        raise exc

    monkeypatch.setattr(ib, "resolve_industry", _raise)
    upd = _update("/curate 양자")
    asyncio.run(ib._cmd_curate(upd, SimpleNamespace(args=["양자"], bot=object())))
    texts = upd.message._r.texts
    assert len(texts) == 1
    if auth:
        _assert_auth_text(texts[0])
        assert "섹터 해석 불가" in texts[0]
    else:
        assert texts[0] == "❌ OpenRouter 한도 초과 — 섹터 해석 불가. 키 충전 후 재시도."


@pytest.mark.parametrize("auth", [True, False])
def test_curate_thesis_outage_text(ib, monkeypatch, auth):
    exc = _exc(_sm(), auth)

    async def _raise(name):
        raise exc

    started: list[str] = []

    async def _pipeline(*a, **k):
        started.append("run")

    monkeypatch.setattr(ib, "_generate_curate_thesis", _raise)
    monkeypatch.setattr(ib, "_run_pipeline", _pipeline)
    sent = _recorder(monkeypatch, ib)
    upd = _update("/curate 양자")
    asyncio.run(ib._launch_curate_pipeline(
        upd, SimpleNamespace(args=[], bot=object()), "양자",
        SimpleNamespace(name_ko="양자컴퓨팅"),
    ))
    assert started == [], "LLM 장애면 파이프라인 진입 안 함 (기존 동작)"
    assert len(sent) == 1
    if auth:
        _assert_auth_text(sent[0])
        assert "thesis 생성 불가" in sent[0]
    else:
        assert sent[0] == "❌ OpenRouter 한도 초과 — thesis 생성 불가."


@pytest.mark.parametrize("auth", [True, False])
def test_idea_pipeline_top_level_outage_text(ib, monkeypatch, tmp_path, auth):
    exc = _exc(_sm(), auth)

    async def _raise(idea_text):
        raise exc

    monkeypatch.setattr(ib, "_parse_idea", _raise)
    monkeypatch.setattr(ib, "download_root_for", lambda kind: tmp_path)
    sent = _recorder(monkeypatch, ib)
    asyncio.run(ib._run_pipeline(
        _update("AI 전력 인프라"), SimpleNamespace(bot=object()), "AI 전력 인프라 수혜주",
    ))
    assert ib.CURRENT_IDEA is None, "finally에서 CURRENT_IDEA 해제"
    assert sent[0].startswith("💡 아이디어:")
    last = sent[-1]
    if auth:
        _assert_auth_text(last)
        assert last == (
            "❌ OpenRouter 키 무효(401) — 관리자가 새 키를 OPENROUTER_API_KEY에 넣고 "
            "재배포한 뒤 다시 시도"
        )
        assert "openrouter.ai/credits" not in last
    else:
        assert last.startswith("❌ OpenRouter API 키 한도 초과")
        assert "결제 잔액 충전" in last


def test_idea_pipeline_auth_log_keeps_selftest_end_signal(ib, monkeypatch, tmp_path, caplog):
    """CLAUDE.md §4 self-test 종료 신호('OpenRouter 한도 초과')는 401에서도 로그에 남는다."""
    sm = _sm()

    async def _raise(idea_text):
        raise sm.OpenRouterAuthError("401")

    monkeypatch.setattr(ib, "_parse_idea", _raise)
    monkeypatch.setattr(ib, "download_root_for", lambda kind: tmp_path)
    _recorder(monkeypatch, ib)
    with caplog.at_level("ERROR", logger=ib.log.name):
        asyncio.run(ib._run_pipeline(
            _update("x"), SimpleNamespace(bot=object()), "AI 전력 인프라 수혜주",
        ))
    msgs = [r.getMessage() for r in caplog.records if r.name == ib.log.name]
    assert any("OpenRouter 한도 초과" in m and "401" in m for m in msgs), msgs


# ======================================================================
# deep_research — 웹 축 error가 '모든 데이터 축 실패' 메시지로 사용자 노출
# ======================================================================
def _dr_summarizer(dr):
    """deep_research가 import 시점에 바인딩한 summarizer 모듈 namespace."""
    return SimpleNamespace(**{
        k: dr.llm_outage_label.__globals__[k]
        for k in ("OpenRouterAuthError", "OpenRouterCreditExhausted")
    })


@pytest.mark.parametrize("auth", [True, False])
def test_deep_research_web_axis_outage_error(monkeypatch, auth):
    from src import deep_research as dr
    exc = _exc(_dr_summarizer(dr), auth)

    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(dr, "get_client", lambda: object())
    monkeypatch.setattr(dr, "chat_with_retry", _raise)
    out = asyncio.run(dr._collect_web("삼성전자", "005930"))
    assert out.text == ""
    if auth:
        _assert_auth_text(out.error)
    else:
        assert out.error == "OpenRouter 크레딧 부족"


def test_deep_research_run_all_axes_failed_shows_auth_text(monkeypatch, tmp_path):
    from src import deep_research as dr
    from src.deepdive import dart_client
    exc = _dr_summarizer(dr).OpenRouterAuthError("401 User not found.")

    def _raise(*a, **k):
        raise exc

    async def _nodart(*a, **k):
        return dr.DartData(error="DART 키 없음")

    async def _nowise(*a, **k):
        return dr.WisereportData(error="wise 로그인 실패")

    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(dart_client, "get_corp_code", lambda t: ("00126380", "삼성전자"))
    monkeypatch.setattr(dr, "_collect_dart", _nodart)
    monkeypatch.setattr(dr, "_collect_wisereport", _nowise)
    monkeypatch.setattr(dr, "get_client", lambda: object())
    monkeypatch.setattr(dr, "chat_with_retry", _raise)
    sent = _recorder(monkeypatch, dr)
    asyncio.run(dr.run(object(), "1", "005930"))
    last = sent[-1]
    assert last.startswith("❌ 모든 데이터 축 실패")
    web_line = [ln for ln in last.splitlines() if ln.startswith("web: ")]
    assert len(web_line) == 1
    _assert_auth_text(web_line[0])


# ======================================================================
# earnings_bot — 0단계 파싱 LLM 장애를 '입력을 이해하지 못했습니다'로 안내 금지
# ======================================================================
@pytest.fixture
def eb_env(monkeypatch):
    from src import earnings_bot as eb
    st = SimpleNamespace(sent=[], llm_calls=0, eb=eb)

    async def _ss(bot, chat_id, text):
        st.sent.append(text)

    async def _sr(update, text, **kw):
        st.sent.append(text)

    monkeypatch.setattr(eb, "_safe_send", _ss)
    monkeypatch.setattr(eb, "_safe_reply", _sr)
    return st


def _patch_llm(monkeypatch, st, behaviour):
    sm = _sm()  # _step_parse는 호출 시점에 `from src import summarizer`

    def _chat(*a, **k):
        st.llm_calls += 1
        return behaviour()

    monkeypatch.setattr(sm, "get_client", lambda: object())
    monkeypatch.setattr(sm, "chat_with_retry", _chat)
    return sm


def _run_eb(st, text="AAPL MSFT 2026 1Q", parsed=None, **kw):
    upd = _update(text)
    asyncio.run(st.eb._run_pipeline(upd, SimpleNamespace(bot=object(), args=[]), text, parsed=parsed, **kw))


@pytest.mark.parametrize("auth", [True, False])
def test_earnings_parse_outage_text(eb_env, monkeypatch, auth):
    sm = _sm()
    exc = _exc(sm, auth)

    def _raise():
        raise exc

    _patch_llm(monkeypatch, eb_env, _raise)
    _run_eb(eb_env)
    assert eb_env.llm_calls == 1
    assert eb_env.sent[0].startswith("📞 어닝콜 분석 시작")
    assert len(eb_env.sent) == 2, eb_env.sent
    last = eb_env.sent[-1]
    assert "입력을 이해하지 못했습니다" not in last
    assert last.endswith(" — 어닝 분석 불가")
    if auth:
        _assert_auth_text(last)
        assert last == f"⚠️ {sm.LLM_AUTH_USER_TEXT} — 어닝 분석 불가"
    else:
        assert last == "⚠️ OpenRouter 크레딧 부족 — 어닝 분석 불가"


def test_earnings_parse_non_outage_failure_still_asks_for_input(eb_env, monkeypatch):
    """일반 파싱 실패(빈 응답·일반 예외)는 기존대로 입력 안내 (회귀 방지)."""
    _patch_llm(monkeypatch, eb_env, lambda: "")
    _run_eb(eb_env)
    assert "입력을 이해하지 못했습니다" in eb_env.sent[-1]

    eb_env.sent.clear()

    def _boom():
        raise RuntimeError("network")

    _patch_llm(monkeypatch, eb_env, _boom)
    _run_eb(eb_env)
    assert "입력을 이해하지 못했습니다" in eb_env.sent[-1]


def test_earnings_preparsed_input_skips_parse(eb_env, monkeypatch):
    """watch 폴러가 넘긴 parsed가 있으면 재파싱(LLM 호출) 없음 — 장애 분기 미진입."""
    def _never():
        raise AssertionError("parsed가 주어지면 parse LLM 호출 금지")

    _patch_llm(monkeypatch, eb_env, _never)

    async def _no_expand(criteria):
        return None

    monkeypatch.setattr(eb_env.eb, "_step_expand_criteria", _no_expand)
    _run_eb(eb_env, parsed={"mode": "criteria", "criteria": "AI 반도체"})
    assert eb_env.llm_calls == 0
    assert eb_env.sent[-1].startswith("⚠️ 조건 → 티커 확장 실패")


@pytest.mark.parametrize("auth", [True, False])
def test_earnings_parse_outage_raises_on_watch_path(eb_env, monkeypatch, auth):
    """raise_outage=True(watch 경로)면 장애 안내를 보내지 않고 raise — 호출측이 'llm_outage'로
    판정해 seen 미마킹. 안내 후 정상 반환하면 402/429는 마킹돼 그 분기 분석이 영구 유실."""
    sm = _sm()
    exc = _exc(sm, auth)

    def _raise():
        raise exc

    _patch_llm(monkeypatch, eb_env, _raise)
    with pytest.raises(sm.OpenRouterCreditExhausted) as ei:
        _run_eb(eb_env, raise_outage=True)
    assert ei.value is exc
    assert eb_env.llm_calls == 1
    assert len(eb_env.sent) == 1 and eb_env.sent[0].startswith("📞 어닝콜 분석 시작"), eb_env.sent


def test_earnings_watch_path_non_outage_parse_failure_unchanged(eb_env, monkeypatch):
    """raise_outage=True여도 일반 파싱 실패는 raise 없이 기존 입력 안내."""
    _patch_llm(monkeypatch, eb_env, lambda: "")
    _run_eb(eb_env, raise_outage=True)
    assert "입력을 이해하지 못했습니다" in eb_env.sent[-1]
