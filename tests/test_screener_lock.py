"""screener_daily_job 동시 호출 가드 — TOCTOU 회귀 테스트.

locked() 체크와 acquire() 사이에 await가 끼면 두 호출이 직렬로 '둘 다'
실행돼 중복 발송 사고. 이 테스트는 동시 2회 호출 시 본체가 정확히
1번만 실행됨을 고정한다 (KR/US 동일 구조).
"""
from __future__ import annotations

import asyncio

import pytest

import src.screener_bot as kr_bot
import src.us_screener_bot as us_bot


class _NullBot:
    """send_* 전부 무시하는 봇 stub."""

    async def send_message(self, *a, **kw):
        return None


@pytest.mark.parametrize(
    "mod,job,inner_name",
    [
        (kr_bot, "screener_daily_job", "_screener_daily_job_locked"),
        (us_bot, "us_screener_daily_job", "_us_screener_daily_job_locked"),
    ],
)
def test_concurrent_calls_run_body_once(monkeypatch, mod, job, inner_name):
    calls = []

    async def _slow_body(bot, override_chat_id):
        calls.append(override_chat_id)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(mod, inner_name, _slow_body)

    async def _race():
        fn = getattr(mod, job)
        await asyncio.gather(
            fn(_NullBot(), override_chat_id="111"),
            fn(_NullBot(), override_chat_id="222"),
        )

    asyncio.run(_race())
    # 두 번째 호출은 lock 가드에서 skip — 본체는 정확히 1회
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mod,job,inner_name",
    [
        (kr_bot, "screener_daily_job", "_screener_daily_job_locked"),
        (us_bot, "us_screener_daily_job", "_us_screener_daily_job_locked"),
    ],
)
def test_lock_released_after_run_and_after_failure(monkeypatch, mod, job, inner_name):
    lock = getattr(mod, "_screener_lock", None) or getattr(mod, "_us_screener_lock")

    async def _ok(bot, override_chat_id):
        return None

    async def _fail(bot, override_chat_id):
        raise RuntimeError("simulated body crash")

    fn = getattr(mod, job)

    monkeypatch.setattr(mod, inner_name, _ok)
    asyncio.run(fn(_NullBot(), override_chat_id="111"))
    assert not lock.locked()

    monkeypatch.setattr(mod, inner_name, _fail)
    with pytest.raises(RuntimeError):
        asyncio.run(fn(_NullBot(), override_chat_id="111"))
    assert not lock.locked()  # 본체 예외에도 release 보장 (다음 cron 막히면 안 됨)
