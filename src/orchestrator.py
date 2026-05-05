"""Orchestrator: 다중 봇 + APScheduler 동시 운영.

현재 등록된 봇:
  - CompanyBot (TELEGRAM_BOT_TOKEN): /report, /deepdive
  - IndustryBot (INDUSTRY_BOT_TOKEN): 산업 리포트 (매일 9시 + on-demand)
  - MarketBot (MARKET_BOT_TOKEN): 투자전략/시황 (매일 9시)
  - GlobalBot (GLOBAL_BOT_TOKEN): 글로벌 Top10 (매주 토 9시)

새 봇 추가 절차는 BOTS.md 참조. 핵심: 아래 BOT_SPECS list에 항목 1개 append.

각 봇 토큰이 있는 봇만 활성화. 모두 같은 asyncio 이벤트 루프에서 polling.
스케줄은 KST 기준.

Railway는 Dockerfile의 CMD가 이 모듈로 향함.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot
from telegram.ext import Application

from src.bot_helpers import diag_env_keys
from src.bot_worker import build_company_app
from src.category_bots import (
    build_global_app,
    build_industry_app,
    build_market_app,
    global_top10_job,
    industry_top10_job,
    market_daily_job,
)
from src.idea_bot import build_idea_app

KST = timezone(timedelta(hours=9))


# ------------------------------------------------------------------
# 봇 등록 데이터 — 새 봇 추가 시 이 리스트에 entry 1개 append.
# ------------------------------------------------------------------
@dataclass
class ScheduledJob:
    """봇 단위 cron 작업. job_func는 async (bot: telegram.Bot) -> None."""
    func: Callable[[Bot], Awaitable[None]]
    job_id: str
    cron: dict  # CronTrigger kwargs (day_of_week, hour, minute 등)
    description: str = ""


@dataclass
class BotSpec:
    """오케스트레이터가 인식하는 단일 봇 등록 정보."""
    name: str                                       # 'company', 'industry', ...
    token_env: str                                  # 토큰 환경변수 이름
    builder: Callable[[str], Application]           # build_xxx_app(token) → Application
    jobs: list[ScheduledJob] = field(default_factory=list)
    optional: bool = True                           # False면 토큰 없을 때 systemexit


BOT_SPECS: list[BotSpec] = [
    BotSpec(
        name="company",
        token_env="TELEGRAM_BOT_TOKEN",
        builder=build_company_app,
        jobs=[],  # 스케줄 없음 (사용자 명령 기반)
    ),
    BotSpec(
        name="industry",
        token_env="INDUSTRY_BOT_TOKEN",
        builder=build_industry_app,
        jobs=[
            ScheduledJob(
                func=industry_top10_job,
                job_id="industry_top10",
                cron={"hour": 9, "minute": 0},
                description="산업 Top10 — 매일 09:00 KST (시황봇과 동일 인터벌)",
            ),
        ],
    ),
    BotSpec(
        name="market",
        token_env="MARKET_BOT_TOKEN",
        builder=build_market_app,
        jobs=[
            ScheduledJob(
                func=market_daily_job,
                job_id="market_daily",
                cron={"hour": 9, "minute": 0},
                description="시황 신규 — 매일 09:00 KST",
            ),
        ],
    ),
    BotSpec(
        name="global",
        token_env="GLOBAL_BOT_TOKEN",
        builder=build_global_app,
        jobs=[
            ScheduledJob(
                func=global_top10_job,
                job_id="global_top10",
                cron={"day_of_week": "sat", "hour": 9, "minute": 0},
                description="글로벌 Top10 — 매주 토 09:00 KST",
            ),
        ],
    ),
    BotSpec(
        name="idea",
        token_env="IDEA_BOT_TOKEN",
        builder=build_idea_app,
        jobs=[],  # 사용자 입력 기반, 스케줄 없음
    ),
]


# ------------------------------------------------------------------
# 진단: inject된 관련 env vars 출력
# ------------------------------------------------------------------
def _diag_env() -> None:
    relevant = diag_env_keys(
        ("TELEGRAM", "WISE", "OPEN", "ALLOWED", "CHAT", "INDUSTRY", "MARKET",
         "GLOBAL", "DART", "IDEA")
    )
    print(f"[orch] env keys total = {len(os.environ)}", flush=True)
    print(f"[orch] relevant env vars = {relevant}", flush=True)


async def _run_forever() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)

    log = logging.getLogger("orchestrator")
    _diag_env()

    # state_store 진단 — 볼륨 mount + 영구 보존 여부 즉시 검증
    try:
        from src import state_store
        state_store.diag_log()
    except Exception:
        log.exception("state_store 진단 호출 실패 (orchestrator 계속 진행)")

    apps: list[tuple[str, Application]] = []
    bot_objects: dict[str, Bot] = {}
    pending_jobs: list[tuple[str, ScheduledJob]] = []

    for spec in BOT_SPECS:
        token = os.getenv(spec.token_env)
        if not token:
            level = log.warning if spec.optional else log.error
            level("%s 미설정 → %sBot 스킵", spec.token_env, spec.name)
            if not spec.optional:
                raise SystemExit(f"{spec.token_env} 환경변수 필수 ({spec.name}Bot)")
            continue
        try:
            app = spec.builder(token)
        except Exception:
            log.exception("%sBot builder 실패 — 스킵", spec.name)
            continue
        apps.append((spec.name, app))
        bot_objects[spec.name] = app.bot
        for job in spec.jobs:
            pending_jobs.append((spec.name, job))
        log.info("%sBot 등록", spec.name.capitalize())

    if not apps:
        raise SystemExit("활성화된 봇이 하나도 없음. 최소 한 봇의 토큰 환경변수가 필요합니다.")

    # 모든 봇 시작 (lifecycle 수동 관리)
    for name, app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("%s 폴링 시작", name)

    # APScheduler — KST 기준 cron
    scheduler = AsyncIOScheduler(timezone=KST)
    for bot_name, job in pending_jobs:
        bot = bot_objects.get(bot_name)
        if bot is None:
            continue
        scheduler.add_job(
            job.func,
            CronTrigger(timezone=KST, **job.cron),
            args=[bot],
            id=job.job_id,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        log.info("스케줄: %s", job.description or job.job_id)

    scheduler.start()
    next_run = {j.id: str(j.next_run_time) for j in scheduler.get_jobs()}
    log.info("스케줄러 시작. 다음 실행 시각: %s", next_run)
    log.info("==== 모든 봇 가동 중. SIGINT/SIGTERM 까지 대기 ====")

    # 종료 시그널 대기
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await stop_event.wait()

    log.info("종료 시그널 수신 → 정리")
    scheduler.shutdown(wait=False)
    for name, app in apps:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            log.exception("%s 종료 실패", name)


def main() -> None:
    asyncio.run(_run_forever())


if __name__ == "__main__":
    main()
