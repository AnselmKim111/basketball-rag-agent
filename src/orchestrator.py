"""Orchestrator: 4개 봇 + APScheduler 동시 운영.

- CompanyBot (TELEGRAM_BOT_TOKEN): 기업명+티커 받아서 종목 리포트 발송
- IndustryBot (INDUSTRY_BOT_TOKEN): 산업 리포트 (월/수/금 9시 자동, 산업명 on-demand)
- MarketBot (MARKET_BOT_TOKEN): 투자전략/시황 신규 (매일 9시 자동)
- GlobalBot (GLOBAL_BOT_TOKEN): 글로벌 Top10 (매주 토 9시 자동)

각 봇 토큰이 있는 봇만 활성화. 모두 같은 asyncio 이벤트 루프에서 polling.
스케줄은 KST 기준.

Railway는 Dockerfile의 CMD가 이 모듈로 향함.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.bot_worker import build_company_app
from src.category_bots import (
    build_global_app,
    build_industry_app,
    build_market_app,
    global_top10_job,
    industry_top10_job,
    market_daily_job,
)

KST = timezone(timedelta(hours=9))


def _diag_env() -> None:
    """진단: 컨테이너에 inject된 관련 env vars 출력."""
    keys = sorted(os.environ.keys())
    relevant = {
        k: (
            f"<set, len={len(os.environ[k])}>"
            if any(s in k.upper() for s in ("TOKEN", "PW", "API_KEY"))
            else os.environ[k][:60]
        )
        for k in keys
        if any(
            kw in k.upper()
            for kw in ("TELEGRAM", "WISE", "OPEN", "ALLOWED", "CHAT", "INDUSTRY", "MARKET", "GLOBAL")
        )
    }
    print(f"[orch] env keys total = {len(keys)}", flush=True)
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

    apps = []  # list of (name, Application)
    bot_objects = {}  # name -> Bot (for scheduler jobs)

    # CompanyBot
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        app = build_company_app(os.environ["TELEGRAM_BOT_TOKEN"])
        apps.append(("company", app))
        bot_objects["company"] = app.bot
        log.info("CompanyBot 등록")
    else:
        log.warning("TELEGRAM_BOT_TOKEN 없음 → CompanyBot 스킵")

    # IndustryBot
    if os.getenv("INDUSTRY_BOT_TOKEN"):
        app = build_industry_app(os.environ["INDUSTRY_BOT_TOKEN"])
        apps.append(("industry", app))
        bot_objects["industry"] = app.bot
        log.info("IndustryBot 등록")
    else:
        log.warning("INDUSTRY_BOT_TOKEN 없음 → IndustryBot 스킵")

    # MarketBot
    if os.getenv("MARKET_BOT_TOKEN"):
        app = build_market_app(os.environ["MARKET_BOT_TOKEN"])
        apps.append(("market", app))
        bot_objects["market"] = app.bot
        log.info("MarketBot 등록")
    else:
        log.warning("MARKET_BOT_TOKEN 없음 → MarketBot 스킵")

    # GlobalBot
    if os.getenv("GLOBAL_BOT_TOKEN"):
        app = build_global_app(os.environ["GLOBAL_BOT_TOKEN"])
        apps.append(("global", app))
        bot_objects["global"] = app.bot
        log.info("GlobalBot 등록")
    else:
        log.info("GLOBAL_BOT_TOKEN 없음 → GlobalBot 스킵 (선택)")

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

    if "industry" in bot_objects:
        bot = bot_objects["industry"]
        scheduler.add_job(
            industry_top10_job,
            CronTrigger(day_of_week="mon,wed,fri", hour=9, minute=0, timezone=KST),
            args=[bot],
            id="industry_top10",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        log.info("스케줄: 산업 Top10 — 월/수/금 09:00 KST")

    if "market" in bot_objects:
        bot = bot_objects["market"]
        scheduler.add_job(
            market_daily_job,
            CronTrigger(hour=9, minute=0, timezone=KST),
            args=[bot],
            id="market_daily",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        log.info("스케줄: 시황 신규 — 매일 09:00 KST")

    if "global" in bot_objects:
        bot = bot_objects["global"]
        scheduler.add_job(
            global_top10_job,
            CronTrigger(day_of_week="sat", hour=9, minute=0, timezone=KST),
            args=[bot],
            id="global_top10",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        log.info("스케줄: 글로벌 Top10 — 매주 토 09:00 KST")

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
