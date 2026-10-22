"""orchestrator 봇 단위 격리 — 2026-10-21 Railway env 유출 → 토큰 폐기 사고 재발 방지.

사고: 폐기된 토큰의 app.initialize()가 InvalidToken("The token `<토큰>` was rejected")을
던져 asyncio.run 밖으로 새어 나감 → 프로세스 종료 → Railway 재시작 crash loop로 멀쩡한
봇까지 전부 다운 + traceback에 토큰 원문. 실제 텔레그램·네트워크는 전부 가짜 객체.
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
import time
import types

import pytest

# conftest의 telegram stub엔 BotCommand가 없음 — orchestrator import 전에 보충
_tg = sys.modules["telegram"]
if not hasattr(_tg, "BotCommand"):
    _tg.BotCommand = lambda command, description: (command, description)

from src import orchestrator as orch  # noqa: E402

BAD_TOKEN = "111111111:AAFakeRevokedTokenFakeRevokedToken12345"
GOOD_TOKEN = "222222222:AAFakeLiveTokenFakeLiveTokenFakeLive999"
ADMIN_CHAT = "12345678"


# ------------------------------------------------------------------
# 가짜 PTB 객체
# ------------------------------------------------------------------
class FakeBot:
    def __init__(self, token: str, username: str) -> None:
        self.token = token
        self.username = username
        self.sent: list[tuple[str, str]] = []
        self.commands = None

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append((str(chat_id), text))

    async def set_my_commands(self, commands):
        self.commands = commands


class FakeUpdater:
    def __init__(self, fail: Exception | None = None) -> None:
        self.running = False
        self.fail = fail

    async def start_polling(self, **kw):
        if self.fail is not None:
            raise self.fail
        self.running = True

    async def stop(self):
        self.running = False


class FakeApp:
    def __init__(self, token: str, username: str, *, init_fail: Exception | None = None,
                 poll_fail: Exception | None = None) -> None:
        self.bot = FakeBot(token, username)
        self.updater = FakeUpdater(poll_fail)
        self.init_fail = init_fail
        self.running = False
        self.initialized = False
        self.shutdown_calls = 0
        self.stop_calls = 0

    async def initialize(self):
        if self.init_fail is not None:
            raise self.init_fail
        self.initialized = True

    async def start(self):
        self.running = True

    async def stop(self):
        self.stop_calls += 1
        self.running = False

    async def shutdown(self):
        self.shutdown_calls += 1
        self.initialized = False


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------
@pytest.fixture
def invalid_token(monkeypatch):
    """telegram.error.InvalidToken — 실제 PTB가 없으면 가짜 모듈 주입."""
    real = sys.modules.get("telegram.error")
    cls = getattr(real, "InvalidToken", None) if real is not None else None
    if isinstance(cls, type) and issubclass(cls, Exception):
        return cls
    mod = types.ModuleType("telegram.error")

    class InvalidToken(Exception):
        pass

    mod.InvalidToken = InvalidToken
    monkeypatch.setitem(sys.modules, "telegram.error", mod)
    return InvalidToken


def _rejected(cls, token: str) -> Exception:
    # PTB Bot.initialize가 실제로 던지는 메시지 형태 (토큰 원문 포함)
    return cls(f"The token `{token}` was rejected by the server.")


@pytest.fixture(autouse=True)
def _isolate_logging(monkeypatch):
    """install_redaction은 pytest handler·sys.excepthook까지 감싸므로 기록만 하는 가짜로.
    _configure_logging의 전역 logging 부작용은 테스트 후 복원."""
    calls: list[int] = []
    if orch._log_redact is not None:
        monkeypatch.setattr(orch._log_redact, "install_redaction", lambda: calls.append(1))
    monkeypatch.setattr(logging, "raiseExceptions", logging.raiseExceptions)
    levels = {n: logging.getLogger(n).level for n in ("httpx", "telegram", "apscheduler", "pykrx")}
    yield calls
    for n, lv in levels.items():
        logging.getLogger(n).setLevel(lv)


def _spec(name, token_env, app, jobs=()):
    return orch.BotSpec(name=name, token_env=token_env, builder=lambda token: app,
                        jobs=list(jobs), commands=[("start", "시작")])


async def _noop_job(bot):
    return None


def _job(job_id):
    return orch.ScheduledJob(func=_noop_job, job_id=job_id, cron={"hour": 3, "minute": 0},
                             description=job_id)


def _assert_no_token(caplog, *tokens):
    for tok in tokens:
        secret = tok.split(":", 1)[1]
        assert secret not in caplog.text
        for r in caplog.records:
            assert secret not in r.getMessage()
            assert secret not in (r.exc_text or "")


async def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


# ------------------------------------------------------------------
# _start_apps — 봇 단위 격리
# ------------------------------------------------------------------
def test_invalid_token_bot_skipped_good_bot_runs(invalid_token, caplog):
    caplog.set_level(logging.INFO)
    bad = FakeApp(BAD_TOKEN, "bad_bot", init_fail=_rejected(invalid_token, BAD_TOKEN))
    good = FakeApp(GOOD_TOKEN, "good_bot")
    specs = {"company": _spec("company", "TELEGRAM_BOT_TOKEN", bad),
             "screener": _spec("screener", "SCREENER_BOT_TOKEN", good)}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TELEGRAM_BOT_TOKEN", BAD_TOKEN)
        running, failed, reasons = asyncio.run(
            orch._start_apps([("company", bad), ("screener", good)], specs))

    assert [n for n, _ in running] == ["screener"]
    assert failed == ["company"]
    assert reasons == {"company": "토큰 거부"}
    assert good.updater.running and good.bot.commands is not None
    assert bad.shutdown_calls == 1  # best-effort 정리
    # 로그: env 이름 + fingerprint만, 예외 텍스트·traceback 없음
    rec = next(r for r in caplog.records if "토큰 거부" in r.getMessage())
    assert rec.levelno == logging.ERROR
    assert "TELEGRAM_BOT_TOKEN" in rec.getMessage() and "company" in rec.getMessage()
    assert f"fp={orch._fingerprint(BAD_TOKEN)}" in rec.getMessage()
    assert rec.exc_info is None
    assert "was rejected" not in caplog.text
    _assert_no_token(caplog, BAD_TOKEN, GOOD_TOKEN)


def test_generic_start_failure_skips_bot_and_cleans_up(invalid_token, caplog):
    caplog.set_level(logging.INFO)
    broken = FakeApp(BAD_TOKEN, "b", poll_fail=RuntimeError("polling boom"))
    good = FakeApp(GOOD_TOKEN, "g")
    specs = {"market": _spec("market", "MARKET_BOT_TOKEN", broken),
             "idea": _spec("idea", "IDEA_BOT_TOKEN", good)}
    running, failed, reasons = asyncio.run(
        orch._start_apps([("market", broken), ("idea", good)], specs))

    assert [n for n, _ in running] == ["idea"]
    assert failed == ["market"]
    assert reasons["market"] == "시작 실패 (RuntimeError)"
    # start()까지 진행된 app은 stop → shutdown
    assert broken.stop_calls == 1 and broken.shutdown_calls == 1
    rec = next(r for r in caplog.records if "시작 실패" in r.getMessage())
    assert rec.exc_info is not None  # 일반 예외는 traceback 유지 (마스킹 formatter 경유)


def test_all_bots_fail_returns_empty_without_raising(invalid_token):
    a = FakeApp(BAD_TOKEN, "a", init_fail=_rejected(invalid_token, BAD_TOKEN))
    b = FakeApp(GOOD_TOKEN, "b", init_fail=OSError("network down"))
    specs = {"company": _spec("company", "TELEGRAM_BOT_TOKEN", a),
             "screener": _spec("screener", "SCREENER_BOT_TOKEN", b)}
    running, failed, _ = asyncio.run(orch._start_apps([("company", a), ("screener", b)], specs))
    assert running == [] and failed == ["company", "screener"]


# ------------------------------------------------------------------
# _schedule_jobs — 실패한 소유 봇의 cron 대행
# ------------------------------------------------------------------
class _RecScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, trigger, args=None, id=None, **kw):
        self.jobs.append({"func": func, "args": args, "id": id, **kw})


def test_schedule_jobs_rehomes_failed_owner_to_first_live_bot(caplog):
    caplog.set_level(logging.INFO)
    live_a, live_b = FakeBot(GOOD_TOKEN, "a"), FakeBot(GOOD_TOKEN, "b")
    sched = _RecScheduler()
    pending = [("company", _job("model_router_weekly")),
               ("screener", _job("screener_daily")),
               ("market", _job("market_daily"))]
    orch._schedule_jobs(sched, pending, {"market": live_a, "screener": live_b}, ["company"])

    by_id = {j["id"]: j for j in sched.jobs}
    assert set(by_id) == {"model_router_weekly", "screener_daily", "market_daily"}
    # 대행: 첫 가동 봇(dict 순서 = BOT_SPECS 순서) — 나머지 인자(job_id·deadline·alert env) 동일
    args = by_id["model_router_weekly"]["args"]
    assert args[1] is live_a
    assert args[0] is _noop_job and args[2] == "model_router_weekly"
    assert by_id["screener_daily"]["args"][1] is live_b  # 자기 봇은 그대로
    assert any("대행" in r.getMessage() and "company" in r.getMessage() for r in caplog.records)


def test_schedule_jobs_skip_when_no_live_bot(caplog):
    caplog.set_level(logging.INFO)
    sched = _RecScheduler()
    orch._schedule_jobs(sched, [("company", _job("recap_weekly_company"))], {}, ["company"])
    assert sched.jobs == []
    assert any(r.levelno == logging.WARNING and "recap_weekly_company" in r.getMessage()
               for r in caplog.records)


def test_schedule_jobs_unknown_owner_not_rehomed():
    """시작 실패가 아닌(기동 대상 아님) 봇의 job은 기존처럼 조용히 스킵 — 대행 대상 아님."""
    sched = _RecScheduler()
    orch._schedule_jobs(sched, [("global", _job("global_top10"))],
                        {"company": FakeBot(GOOD_TOKEN, "c")}, [])
    assert sched.jobs == []


# ------------------------------------------------------------------
# _run_forever — end-to-end (가짜 앱·스케줄러는 실제 APScheduler)
# ------------------------------------------------------------------
@pytest.fixture
def boot_env(monkeypatch):
    """_run_forever 부팅 부작용(진단 print·디스크 정리·Telethon) 차단 + env 정리."""
    monkeypatch.setattr(orch, "_diag_env", lambda: None)
    for modname in ("state_store", "watchlist_store"):
        try:
            mod = __import__(f"src.{modname}", fromlist=["diag_log"])
            monkeypatch.setattr(mod, "diag_log", lambda: None, raising=False)
        except Exception:
            pass
    try:
        from src import idea_cache
        monkeypatch.setattr(idea_cache, "cleanup_old", lambda keep=200: 0)
    except Exception:
        pass
    tg_calls: list = []
    from src import tg_login
    monkeypatch.setattr(tg_login, "schedule_boot_login",
                        lambda bot=None: tg_calls.append(bot) or None)
    for k in ("ACTIVE_BOTS", "MODEL_ROUTER_CHAT_ID", "ALLOWED_CHAT_IDS", "CRED_SELF_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("REPORT_CHAT_ID", ADMIN_CHAT)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BAD_TOKEN)
    monkeypatch.setenv("SCREENER_BOT_TOKEN", GOOD_TOKEN)

    schedulers: list = []

    class RecScheduler(orch.AsyncIOScheduler):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            schedulers.append(self)

    monkeypatch.setattr(orch, "AsyncIOScheduler", RecScheduler)

    hooks: list = []
    from src import summarizer
    monkeypatch.setattr(summarizer, "set_auth_alert_hook", lambda fn: hooks.append(fn))

    cred_calls: list = []

    async def fake_cred(bot_objects, failed_bots, **kw):
        cred_calls.append((dict(bot_objects), list(failed_bots), kw))
        return "🔐 자격증명 점검\n✗ TELEGRAM_BOT_TOKEN — company봇 시작 실패 — 토큰 거부"

    from src import cred_check
    monkeypatch.setattr(cred_check, "run_credential_check", fake_cred)
    return types.SimpleNamespace(tg_calls=tg_calls, schedulers=schedulers, hooks=hooks,
                                 cred_calls=cred_calls)


def test_run_forever_keeps_good_bot_running_when_other_token_rejected(
        invalid_token, boot_env, monkeypatch, caplog, _isolate_logging):
    caplog.set_level(logging.INFO)
    bad = FakeApp(BAD_TOKEN, "bad_bot", init_fail=_rejected(invalid_token, BAD_TOKEN))
    good = FakeApp(GOOD_TOKEN, "good_bot")
    monkeypatch.setattr(orch, "BOT_SPECS", [
        _spec("company", "TELEGRAM_BOT_TOKEN", bad, jobs=[_job("model_router_weekly")]),
        _spec("screener", "SCREENER_BOT_TOKEN", good, jobs=[_job("screener_daily")]),
    ])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(orch._run_forever(stop))
        assert await _wait_until(lambda: "모든 봇 가동 중" in caplog.text), caplog.text
        assert not task.done()  # 프로세스(루프) 계속 — crash 없음
        assert good.updater.running and bad.shutdown_calls == 1

        # 1) 마스킹 formatter 설치
        assert _isolate_logging, "install_redaction 미호출"
        # 2) company cron → 첫 가동 봇(screener)이 대행, 자기 cron은 그대로
        sched = boot_env.schedulers[0]
        assert sched.get_job("model_router_weekly").args[1] is good.bot
        assert sched.get_job("screener_daily").args[1] is good.bot
        # 3) Telethon 로그인 알림 봇도 첫 가동 봇
        assert boot_env.tg_calls == [good.bot]
        # 4) 401 hook — 워커 스레드에서 호출해도 즉시 반환, 메인 루프에서 admin chat 1건
        hook = next(h for h in boot_env.hooks if h is not None)
        t0 = time.monotonic()
        await asyncio.to_thread(hook, "🔑 OpenRouter 인증 실패(401)")
        assert time.monotonic() - t0 < 1.0
        assert await _wait_until(lambda: any("401" in (t or "") for _, t in good.bot.sent))
        # 5) 자격증명 점검 1회 → admin chat, 실패 봇 기록 전달
        assert await _wait_until(lambda: any("자격증명 점검" in (t or "") for _, t in good.bot.sent))
        assert len(boot_env.cred_calls) == 1
        bots, failed, kw = boot_env.cred_calls[0]
        assert list(bots) == ["screener"] and failed == ["company"]
        assert kw["fail_reasons"] == {"company": "토큰 거부"}
        assert kw["bot_token_envs"]["company"] == "TELEGRAM_BOT_TOKEN"
        assert all(cid == ADMIN_CHAT for cid, _ in good.bot.sent)
        assert bad.bot.sent == []

        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert not good.updater.running and good.shutdown_calls == 1
    assert boot_env.hooks[-1] is None  # 종료 시 hook 해제
    _assert_no_token(caplog, BAD_TOKEN, GOOD_TOKEN)


def test_run_forever_all_bots_fail_stays_idle(invalid_token, boot_env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    a = FakeApp(BAD_TOKEN, "a", init_fail=_rejected(invalid_token, BAD_TOKEN))
    b = FakeApp(GOOD_TOKEN, "b", init_fail=_rejected(invalid_token, GOOD_TOKEN))
    monkeypatch.setattr(orch, "BOT_SPECS", [
        _spec("company", "TELEGRAM_BOT_TOKEN", a, jobs=[_job("model_router_weekly")]),
        _spec("screener", "SCREENER_BOT_TOKEN", b),
    ])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(orch._run_forever(stop))
        assert await _wait_until(lambda: "모든 봇 시작 실패" in caplog.text)
        await asyncio.sleep(0.1)
        assert not task.done(), "전부 실패해도 종료 금지 (Railway crash loop)"
        stop.set()
        await asyncio.wait_for(task, 5)
        assert task.exception() is None

    asyncio.run(scenario())
    assert boot_env.schedulers == []  # 가동 봇 없음 → 스케줄러·hook 미기동
    assert boot_env.hooks == []
    # 자격증명 점검은 로그로만 (보낼 봇 없음)
    assert len(boot_env.cred_calls) == 1
    assert boot_env.cred_calls[0][1] == ["company", "screener"]
    assert "자격증명 점검" in caplog.text
    _assert_no_token(caplog, BAD_TOKEN, GOOD_TOKEN)


def test_cred_self_check_disabled(invalid_token, boot_env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("CRED_SELF_CHECK", "0")
    good = FakeApp(GOOD_TOKEN, "good_bot")
    monkeypatch.setattr(orch, "BOT_SPECS", [_spec("screener", "SCREENER_BOT_TOKEN", good)])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(orch._run_forever(stop))
        assert await _wait_until(lambda: "모든 봇 가동 중" in caplog.text)
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert boot_env.cred_calls == []
    assert not any("자격증명" in (t or "") for _, t in good.bot.sent)


def test_idle_branch_warns_secret_names_only(boot_env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    secret_val = "sk-or-v1-" + "ab" * 24
    monkeypatch.setenv("ACTIVE_BOTS", "_disabled_")
    monkeypatch.setenv("OPENROUTER_API_KEY", secret_val)
    monkeypatch.setenv("REPORT_CHAT_ID", ADMIN_CHAT)

    async def scenario():
        stop = asyncio.Event()
        stop.set()  # idle 진입 즉시 반환
        await asyncio.wait_for(orch._run_forever(stop), 5)

    asyncio.run(scenario())
    rec = next(r for r in caplog.records if "idle 서비스가 시크릿" in r.getMessage())
    msg = rec.getMessage()
    assert rec.levelno == logging.WARNING
    assert "OPENROUTER_API_KEY" in msg and "TELEGRAM_BOT_TOKEN" in msg
    assert "REPORT_CHAT_ID" not in msg  # chat id는 비밀 아님
    assert "삭제 권장" in msg
    assert secret_val not in caplog.text and BAD_TOKEN not in caplog.text


# ------------------------------------------------------------------
# 401 알림 hook — handler 팩토리 부재 시 폴백
# ------------------------------------------------------------------
def test_auth_hook_fallback_sends_one_alert_without_blocking(monkeypatch):
    from src import summarizer
    from src.model_router import handler
    monkeypatch.delattr(handler, "make_auth_alert_hook", raising=False)
    monkeypatch.setenv("REPORT_CHAT_ID", ADMIN_CHAT)
    hooks: list = []
    monkeypatch.setattr(summarizer, "set_auth_alert_hook", lambda fn: hooks.append(fn))
    bot = FakeBot(GOOD_TOKEN, "g")

    async def scenario():
        loop = asyncio.get_running_loop()
        assert orch._register_auth_alert_hook(bot, loop) is True
        hook = hooks[0]
        done = threading.Event()

        def worker():
            hook("🔑 401 알림")
            done.set()

        threading.Thread(target=worker).start()
        assert await asyncio.to_thread(done.wait, 1.0)
        assert await _wait_until(lambda: bot.sent == [(ADMIN_CHAT, "🔑 401 알림")])

    asyncio.run(scenario())


def test_auth_hook_not_registered_without_admin_chat(monkeypatch):
    from src import summarizer
    for k in ("REPORT_CHAT_ID", "MODEL_ROUTER_CHAT_ID", "ALLOWED_CHAT_IDS"):
        monkeypatch.delenv(k, raising=False)
    hooks: list = []
    monkeypatch.setattr(summarizer, "set_auth_alert_hook", lambda fn: hooks.append(fn))

    async def scenario():
        return orch._register_auth_alert_hook(FakeBot(GOOD_TOKEN, "g"), asyncio.get_running_loop())

    assert asyncio.run(scenario()) is False
    assert hooks == []


# ------------------------------------------------------------------
# main() — 미처리 예외는 마스킹 logger로 critical 후 exit 1
# ------------------------------------------------------------------
def test_main_logs_critical_redacted_and_exits_nonzero(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    async def boom(stop_event=None):
        raise RuntimeError(f"The token `{BAD_TOKEN}` was rejected by the server.")

    monkeypatch.setattr(orch, "_run_forever", boom)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    if orch._log_redact is not None:
        handler.setFormatter(orch._log_redact.RedactingFormatter("%(levelname)s %(message)s"))
    lg = logging.getLogger("orchestrator")
    lg.addHandler(handler)
    try:
        with pytest.raises(SystemExit) as ei:
            orch.main()
    finally:
        lg.removeHandler(handler)
    assert ei.value.code == 1
    rec = next(r for r in caplog.records if r.levelno == logging.CRITICAL)
    assert "RuntimeError" in rec.getMessage() and rec.exc_info is not None
    if orch._log_redact is not None:
        out = stream.getvalue()
        assert "CRITICAL" in out and "RuntimeError" in out
        assert BAD_TOKEN.split(":", 1)[1] not in out


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(3)])
def test_main_reraises_keyboard_interrupt_and_systemexit(monkeypatch, caplog, exc):
    async def boom(stop_event=None):
        raise exc

    monkeypatch.setattr(orch, "_run_forever", boom)
    with pytest.raises(type(exc)) as ei:
        orch.main()
    if isinstance(exc, SystemExit):
        assert ei.value.code == 3
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_tg_login_bot_unchanged_when_company_not_configured(boot_env, monkeypatch, caplog):
    """종목봇 미설정 서비스(screener 전용)는 기존대로 tg_login 알림 봇 None — 대행은 시작 실패 때만."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("CRED_SELF_CHECK", "0")
    good = FakeApp(GOOD_TOKEN, "good_bot")
    monkeypatch.setattr(orch, "BOT_SPECS", [_spec("screener", "SCREENER_BOT_TOKEN", good)])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(orch._run_forever(stop))
        assert await _wait_until(lambda: "모든 봇 가동 중" in caplog.text)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert boot_env.tg_calls == [None]
