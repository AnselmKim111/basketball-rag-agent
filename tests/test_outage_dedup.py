"""LLM/텔레그램 장애 시 dedup(seen) 처리 — 2026-10-21 Railway env 유출 → 키 disable 사고 후속.

  - category_bots: OpenRouter 인증/크레딧 장애 배치는 placeholder로 발송·seen 처리하지 않음
  - disclosure_watcher: 발송 성공분만 seen, 실패분은 재시도 (상한 넘으면 포기)
  - earnings_bot watch: 파이프라인 종료 후 seen, LLM 장애면 미마킹 + 폴링 skip/backoff,
    pre-call 합성 실패는 24h stamp로 재발사·스팸 방지

네트워크·실제 state 파일 미사용 (전부 mock / tmp_path).
"""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src import category_bots as cb
from src import disclosure_watcher as dw
from src import earnings_bot as eb


def _sm():
    """현재 src.summarizer — 다른 테스트가 sys.modules에서 교체·제거했다 재import될 수 있어
    (test_idea_sourcing·test_holiday_skip 등) 모듈 로드 시점 참조 대신 매번 조회.
    earnings_bot·category_bots도 호출 시점에 `from src import ...` 한다."""
    return importlib.import_module("src.summarizer")


def _auth_error_cls():
    sm = _sm()
    return getattr(sm, "OpenRouterAuthError", sm.OpenRouterCreditExhausted)


class _FakeBot:
    def __init__(self, fail_if=None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_if = fail_if or (lambda chat_id, text: False)

    async def send_message(self, chat_id=None, text="", **kw):
        await asyncio.sleep(0)  # 실제 네트워크처럼 이벤트 루프 양보 (구독자 task 동시 진행)
        if self.fail_if(str(chat_id), text):
            raise RuntimeError("telegram send 실패 (mock)")
        self.sent.append((str(chat_id), text))


@pytest.fixture
def breaker(monkeypatch):
    st = SimpleNamespace(open=False)
    monkeypatch.setattr(_sm(), "auth_breaker_open", lambda: st.open, raising=False)
    return st


# ======================================================================
# 1) category_bots
# ======================================================================
@pytest.fixture
def cat_env(monkeypatch, tmp_path, breaker):
    from src.wisereport import ReportItem

    st = SimpleNamespace(
        texts=[], pdfs=[], marks=[], wr_inits=0, breaker=breaker,
        summaries=lambda paths: [f"요약 {p.stem}" for p in paths],
    )
    items = [
        ReportItem(rpt_id=f"R{i}", sch_db="Y", sch_lang="K", sch_dt="20261022090000",
                   sch_gubun="", sch_cont="", title=f"리포트 {i}")
        for i in (1, 2)
    ]

    class _FakeWR:
        def __init__(self, *a, **kw) -> None:
            st.wr_inits += 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ensure_logged_in(self):
            pass

        def list_top_reports(self, **kw):
            return list(items)

        def download_reports(self, its, target_dir):
            target_dir.mkdir(parents=True, exist_ok=True)
            out = []
            for it in its:
                p = target_dir / f"{it.rpt_id}.pdf"
                p.write_bytes(b"%PDF")
                out.append(p)
            return out

    async def _send_text(bot, chat_id, text, *a, **kw):
        st.texts.append(text)

    async def _send_pdf(bot, chat_id, path, *a, **kw):
        st.pdfs.append(path.name)

    async def _summ(paths, *, short_summary, label):
        return st.summaries(paths)

    monkeypatch.setattr(cb, "wisereport_creds", lambda: ("id", "pw"))
    monkeypatch.setattr(cb, "WisereportClient", _FakeWR)
    monkeypatch.setattr(cb, "seen", lambda k: set())
    monkeypatch.setattr(cb, "seen_titles", lambda k: set())
    monkeypatch.setattr(cb, "mark_seen_with_titles",
                        lambda key, pairs, cap=5000: st.marks.append((key, list(pairs))))
    monkeypatch.setattr(cb, "_send_text", _send_text)
    monkeypatch.setattr(cb, "_send_pdf", _send_pdf)
    monkeypatch.setattr(cb, "_summarize_pdfs_parallel", _summ)
    st.root = tmp_path
    return st


def _run_category(st, dedup_key="industry_top10"):
    return asyncio.run(cb._process_and_send_category(
        bot=object(), chat_id="1", category="industry", label="산업 조회수 Top10",
        sort_by="popular", limit=2, days_back=14, dedup_key=dedup_key,
        download_root=st.root, short_summary=True,
    ))


def test_category_breaker_open_skips_download_send_and_mark(cat_env):
    cat_env.breaker.open = True
    assert _run_category(cat_env) == 0
    assert cat_env.wr_inits == 0, "breaker 열림이면 wisereport 접속도 생략"
    assert cat_env.pdfs == [] and cat_env.marks == []
    assert len(cat_env.texts) == 1 and "LLM 요약 불가" in cat_env.texts[0]


def test_category_auth_error_during_batch_not_marked(cat_env):
    def _summ(paths):
        cat_env.breaker.open = True  # 요약 중 401 → breaker 열림
        return ["(요약 실패: OpenRouter 토큰 부족)" for _ in paths]
    cat_env.summaries = _summ
    assert _run_category(cat_env) == 0
    assert cat_env.marks == [], "placeholder 배치는 seen 처리 금지 (복구 후 재요약)"
    assert cat_env.pdfs == []
    assert len(cat_env.texts) == 1 and "LLM 요약 불가" in cat_env.texts[0]


def test_category_credit_placeholder_without_breaker_not_marked(cat_env):
    cat_env.summaries = lambda paths: ["요약 R1", "(요약 실패: OpenRouter 토큰 부족)"]
    assert _run_category(cat_env) == 0
    assert cat_env.marks == [] and cat_env.pdfs == []


def test_category_normal_llm_failure_keeps_send_and_mark(cat_env):
    cat_env.summaries = lambda paths: [
        "(LLM 빈 응답이 3회 연속 — 요약 생략)", "(요약 실패: ValueError('x'))",
    ]
    assert _run_category(cat_env) == 2
    assert cat_env.pdfs == ["R1.pdf", "R2.pdf"]
    assert cat_env.marks == [("industry_top10", [("R1", "리포트 1"), ("R2", "리포트 2")])]


def test_category_on_demand_outage_keeps_today_behavior(cat_env):
    cat_env.breaker.open = True
    cat_env.summaries = lambda paths: ["(요약 실패: OpenRouter 토큰 부족)" for _ in paths]
    assert _run_category(cat_env, dedup_key=None) == 2
    assert cat_env.pdfs == ["R1.pdf", "R2.pdf"]
    assert cat_env.marks == []


# ======================================================================
# 2) disclosure_watcher
# ======================================================================
@pytest.fixture
def disc_env(monkeypatch):
    st = SimpleNamespace(store={}, reports=[])
    monkeypatch.setattr(dw.watchlist_store, "all_tickers", lambda: {"005930"})
    monkeypatch.setattr(dw.watchlist_store, "subscribers_of", lambda t: ["100"])
    monkeypatch.setattr(dw.dart_client, "get_corp_code", lambda t: ("00126380", "삼성전자"))
    monkeypatch.setattr(dw.dart_client, "fetch_recent_disclosures", lambda c, d: list(st.reports))
    monkeypatch.setattr(dw.dart_client, "classify_disclosure", lambda nm: "normal")
    monkeypatch.setattr(dw.state_store, "seen", lambda k: set(st.store.get(k, [])))
    monkeypatch.setattr(dw.state_store, "mark_seen",
                        lambda k, ids, cap=1000: st.store.setdefault(k, []).extend(ids))
    monkeypatch.setattr(dw, "_log_disclosure_for_recap", lambda *a: None)
    monkeypatch.delenv("DISCLOSURE_SEND_MAX_ATTEMPTS", raising=False)
    dw._SEND_FAILS.clear()
    # DART는 최신순 반환 → 발송은 reversed (오래된 것부터)
    st.reports = [
        SimpleNamespace(rcept_no="B002", report_nm="잠정실적", rcept_dt="20261022"),
        SimpleNamespace(rcept_no="A001", report_nm="주요사항보고", rcept_dt="20261022"),
    ]
    yield st
    dw._SEND_FAILS.clear()


def test_disclosure_marks_only_delivered_and_retries_failed(disc_env):
    bot = _FakeBot(fail_if=lambda cid, text: "B002" in text)
    asyncio.run(dw.poll_and_dispatch(bot))
    assert disc_env.store["disc_100"] == ["A001"], "실패한 B002는 seen 처리 금지"
    assert [t for _, t in bot.sent if "A001" in t]

    bot2 = _FakeBot()
    asyncio.run(dw.poll_and_dispatch(bot2))
    assert len(bot2.sent) == 1 and "B002" in bot2.sent[0][1], "A001 재발송 없이 B002만 재시도"
    assert sorted(disc_env.store["disc_100"]) == ["A001", "B002"]
    assert dw._SEND_FAILS == {}, "성공 시 실패 카운터 삭제"


def test_disclosure_gives_up_after_max_attempts(disc_env, monkeypatch):
    monkeypatch.setenv("DISCLOSURE_SEND_MAX_ATTEMPTS", "3")
    attempts = []

    def _fail(cid, text):
        if "B002" in text:
            attempts.append(1)
            return True
        return False
    bot = _FakeBot(fail_if=_fail)
    for _ in range(2):
        asyncio.run(dw.poll_and_dispatch(bot))
        assert "B002" not in disc_env.store.get("disc_100", [])
    asyncio.run(dw.poll_and_dispatch(bot))  # 3회째 실패 → 포기, seen 처리
    assert "B002" in disc_env.store["disc_100"]
    assert dw._SEND_FAILS == {}
    asyncio.run(dw.poll_and_dispatch(bot))  # 이후 재시도 없음
    assert len(attempts) == 3


def test_disclosure_all_failed_marks_nothing(disc_env):
    asyncio.run(dw.poll_and_dispatch(_FakeBot(fail_if=lambda cid, text: True)))
    assert disc_env.store.get("disc_100", []) == []
    assert dw._SEND_FAILS == {("100", "A001"): 1, ("100", "B002"): 1}


# ======================================================================
# 3) earnings_bot watch poll + pre-call
# ======================================================================
@pytest.fixture
def eb_env(monkeypatch, tmp_path, breaker):
    from src.earnings import consensus
    from src.earnings import earnings_watchlist as wl
    from src.earnings import transcript_source as ts

    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("EARNINGS_WATCH_OUTAGE_BACKOFF_S", raising=False)
    eb._WATCH_INFLIGHT.clear()
    eb._PRECALL_FAIL_MEM.clear()
    monkeypatch.setattr(eb, "_watch_outage_at", None)

    today = datetime.utcnow().date()
    st = SimpleNamespace(
        breaker=breaker, clock=1_000_000.0, store={}, call_offset=0,
        cal_calls=0, tr_calls=0, parse_calls=0, parse_exc=None,
        pipelines=[], pipeline_hook=None, synth_calls=0, synth_result=(None, None),
        today=today,
    )
    monkeypatch.setattr(eb, "_now", lambda: st.clock)

    def _cal(t):
        st.cal_calls += 1
        return (today + timedelta(days=st.call_offset)).isoformat()

    def _tr(client, ticker, y, q):
        st.tr_calls += 1
        return SimpleNamespace(grounded=True, full_text="x" * 3000)

    async def _parse(user_text, *, raise_outage=False):
        st.parse_calls += 1
        if st.parse_exc is not None:
            raise st.parse_exc
        return {"mode": "tickers", "tickers": ["AAPL"]}

    async def _pipeline(update, context, user_text, parsed=None):
        st.pipelines.append((str(update.effective_chat.id), parsed, set(st.store.get("earnings_watch", []))))
        if st.pipeline_hook:
            st.pipeline_hook()

    async def _synth(ticker, year, quarter, fiscal_label):
        st.synth_calls += 1
        return st.synth_result

    monkeypatch.setattr(wl, "all_tickers", lambda: {"AAPL"})
    monkeypatch.setattr(wl, "subscribers_of", lambda t: ["111", "222"])
    monkeypatch.setattr(consensus, "fetch_next_earnings_date", _cal)
    monkeypatch.setattr(ts, "fetch_full_transcript", _tr)
    monkeypatch.setattr(_sm(), "get_client", lambda: object())
    state_store = importlib.import_module("src.state_store")
    monkeypatch.setattr(state_store, "seen", lambda k: set(st.store.get(k, [])))
    monkeypatch.setattr(state_store, "mark_seen",
                        lambda k, ids, cap=1000: st.store.setdefault(k, []).extend(ids))
    monkeypatch.setattr(eb, "_step_parse", _parse)
    monkeypatch.setattr(eb, "_run_pipeline", _pipeline)
    monkeypatch.setattr(eb, "_step_synthesize_pre_call", _synth)

    q = (today.month - 1) // 3 + 1
    st.seen_key = f"AAPL:Q{q}_{today.year}"
    yield st
    eb._WATCH_INFLIGHT.clear()
    eb._PRECALL_FAIL_MEM.clear()


def _poll(bot) -> None:
    async def _go():
        await eb.earnings_watch_poll_job(bot)
        while eb._WATCH_TASKS:
            await asyncio.gather(*list(eb._WATCH_TASKS))
    asyncio.run(_go())


def test_watch_poll_skipped_entirely_when_breaker_open(eb_env):
    eb_env.breaker.open = True
    bot = _FakeBot()
    _poll(bot)
    assert eb_env.cal_calls == 0 and eb_env.tr_calls == 0, "Yahoo·transcript(AV quota) 호출 0"
    assert bot.sent == [] and eb_env.pipelines == []


def test_watch_marks_seen_only_after_pipeline(eb_env):
    bot = _FakeBot()
    _poll(bot)
    assert [cid for cid, _, _ in eb_env.pipelines] == ["111", "222"]
    assert all(parsed == {"mode": "tickers", "tickers": ["AAPL"]} for _, parsed, _ in eb_env.pipelines)
    assert eb_env.parse_calls == 1, "파싱은 구독자 공용 1회"
    assert all(eb_env.seen_key not in seen for _, _, seen in eb_env.pipelines), "실행 중엔 미마킹"
    assert eb_env.store["earnings_watch"] == [eb_env.seen_key]
    assert eb._WATCH_INFLIGHT == set()
    assert sum("watch 감지" in t for _, t in bot.sent) == 1


def test_watch_auth_error_at_parse_unmarked_silent_and_backoff(eb_env):
    eb_env.parse_exc = _auth_error_cls()("401 (mock)")
    bot = _FakeBot()
    _poll(bot)
    assert eb_env.pipelines == [] and bot.sent == [], "장애 시 메시지 0"
    assert eb_env.store.get("earnings_watch", []) == []
    assert eb_env.tr_calls == 1

    _poll(bot)  # backoff 중 → 폴링 전체 skip (transcript quota 보호)
    assert eb_env.tr_calls == 1 and eb_env.cal_calls == 1

    eb_env.clock += eb.WATCH_OUTAGE_BACKOFF_DEFAULT_S + 1
    eb_env.parse_exc = None
    _poll(bot)
    assert eb_env.store["earnings_watch"] == [eb_env.seen_key], "복구 후 재시도 → 발송·마킹"
    assert len(eb_env.pipelines) == 2


def test_watch_auth_mid_pipeline_leaves_unmarked(eb_env):
    def _trip():
        eb_env.breaker.open = True
    eb_env.pipeline_hook = _trip
    _poll(_FakeBot())
    assert eb_env.store.get("earnings_watch", []) == []
    assert eb._WATCH_INFLIGHT == set()
    assert eb._watch_outage_at is not None


def test_watch_credit_exception_from_pipeline_leaves_unmarked(eb_env):
    def _raise():
        raise _sm().OpenRouterCreditExhausted("402 (mock)")
    eb_env.pipeline_hook = _raise
    _poll(_FakeBot())
    assert eb_env.store.get("earnings_watch", []) == []


def test_watch_non_auth_pipeline_error_still_marks(eb_env):
    def _raise():
        raise ValueError("boom")
    eb_env.pipeline_hook = _raise
    _poll(_FakeBot())
    assert eb_env.store["earnings_watch"] == [eb_env.seen_key]


def test_watch_inflight_key_not_refetched(eb_env):
    eb._WATCH_INFLIGHT.add(eb_env.seen_key)
    _poll(_FakeBot())
    assert eb_env.tr_calls == 0 and eb_env.pipelines == []


def test_precall_failure_stamp_suppresses_refire_for_24h(eb_env, monkeypatch):
    eb_env.call_offset = 2  # D-2 → pre-call 윈도 (transcript 윈도 밖)
    bot = _FakeBot()
    _poll(bot)
    assert eb_env.synth_calls == 2  # 구독자 2명
    first = list(bot.sent)
    assert sum("pre-call 자동 발사" in t for _, t in first) == 2
    assert sum("pre-call 합성 실패" in t for _, t in first) == 2

    call_date = (eb_env.today + timedelta(days=2))
    y, q = call_date.year, (call_date.month - 1) // 3 + 1
    assert eb._precall_fail_path("AAPL", y, q).exists(), "precall 캐시 디렉토리에 실패 stamp"

    eb_env.clock += 4 * 3600  # 다음 4h 폴링
    _poll(bot)
    assert eb_env.synth_calls == 2 and bot.sent == first, "24h 내 재발사·재메시지 금지"

    eb_env.clock += 20 * 3600 + 1  # 실패 후 24h 경과
    eb_env.synth_result = ("pre-call 본문", None)
    sent_texts = []

    async def _chunked(bot_, chat_id, text, **kw):
        sent_texts.append(text)
    monkeypatch.setattr(eb, "send_text_chunked", _chunked)
    _poll(bot)
    assert eb_env.synth_calls >= 3 and "pre-call 본문" in sent_texts


def test_precall_breaker_open_is_silent(eb_env):
    eb_env.breaker.open = True
    bot = _FakeBot()
    asyncio.run(eb._watch_fire_pre_call(bot, "111", "AAPL", 2026, 4, "Q4 2026", call_date="2026-10-24"))
    assert bot.sent == [] and eb_env.synth_calls == 0


def test_precall_stamp_keyed_by_call_date_and_persisted(eb_env):
    eb._precall_fail_mark("AAPL", 2026, 4, "2026-10-24", reason="empty")
    assert eb._precall_fail_recent("AAPL", 2026, 4, "2026-10-24")
    assert not eb._precall_fail_recent("AAPL", 2026, 4, "2026-10-30"), "일정 변경 → 새 날짜는 허용"
    eb._PRECALL_FAIL_MEM.clear()  # 재배포 (메모리 초기화) — 파일 stamp로 유지
    assert eb._precall_fail_recent("AAPL", 2026, 4, "2026-10-24")
    eb_env.clock += eb.PRECALL_FAIL_TTL_S + 1
    assert not eb._precall_fail_recent("AAPL", 2026, 4, "2026-10-24")


def test_step_parse_raise_outage_flag(monkeypatch):
    sm = _sm()
    err = _auth_error_cls()

    def _boom(*a, **kw):
        raise err("401 (mock)")
    monkeypatch.setattr(sm, "get_client", lambda: object())
    monkeypatch.setattr(sm, "chat_with_retry", _boom)
    assert asyncio.run(eb._step_parse("AAPL Q3 2026")) is None, "기본은 기존대로 None"
    with pytest.raises(sm.OpenRouterCreditExhausted):
        asyncio.run(eb._step_parse("AAPL Q3 2026", raise_outage=True))
