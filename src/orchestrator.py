"""Orchestrator: 다중 봇 + APScheduler 동시 운영.

현재 등록된 봇:
  - CompanyBot (TELEGRAM_BOT_TOKEN): /report, /deepdive
  - IndustryBot (INDUSTRY_BOT_TOKEN): 산업 리포트 (매일 9시 + on-demand)
  - MarketBot (MARKET_BOT_TOKEN): 투자전략/시황 (매일 9시)
  - GlobalBot (GLOBAL_BOT_TOKEN): 글로벌 Top10 (매주 토 9시)
  - IdeaBot (IDEA_BOT_TOKEN): 종목 아이디어 발굴 (on-demand)
  - DisclosureBot (DISCLOSURE_BOT_TOKEN): DART 공시 폴링 (5분)
  - ScreenerBot (SCREENER_BOT_TOKEN): 한국 주식 기술적 신호 (매일 16시)
  - EarningsBot (EARNINGS_BOT_TOKEN): 미국 어닝콜 + 비교 PDF (on-demand)

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

from telegram import BotCommand

from src.bot_helpers import diag_env_keys
from src.bot_worker import COMPANY_COMMANDS, build_company_app
from src.category_bots import (
    GLOBAL_COMMANDS,
    INDUSTRY_COMMANDS,
    MARKET_COMMANDS,
    build_global_app,
    build_industry_app,
    build_market_app,
    global_top10_job,
    industry_top10_job,
    market_daily_job,
)
from src.channel_relay import channel_relay_job
from src.disclosure_bot import DISCLOSURE_COMMANDS, build_disclosure_app, disclosure_poll_job
from src.earnings_bot import EARNINGS_COMMANDS, build_earnings_app, earnings_watch_poll_job, earnings_digest_cron_job
from src.idea_bot import IDEA_COMMANDS, build_idea_app
from src.recap_bot import RECAP_COMMANDS, build_recap_app, recap_weekly_job
from src.screener_bot import SCREENER_COMMANDS, build_screener_app, screener_daily_job
from src.us_screener_bot import US_SCREENER_COMMANDS, build_us_screener_app, us_screener_daily_job
from src.report_bot import REPORT_COMMANDS, build_report_app, report_daily_job

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
    deadline_sec: int = 1800  # 이 시간 안에 잡이 끝나지 않으면 admin에 미완주 알림
    alert_env_keys: tuple = ()  # admin chat_id env vars (없으면 알림 생략)


async def _instrumented_job(func: Callable[[Bot], Awaitable[None]], bot: Bot,
                            job_id: str, deadline_sec: int,
                            alert_env_keys: tuple) -> None:
    """크론 잡 래퍼 — deadline_sec 안에 완주 안 되면 admin에 ⚠ 알림."""
    import asyncio
    from src.admin_alerts import alert_admin
    done = asyncio.Event()

    async def _watchdog():
        try:
            await asyncio.sleep(deadline_sec)
            if not done.is_set() and alert_env_keys:
                await alert_admin(
                    bot, alert_env_keys,
                    f"⏰ 크론 {job_id} 미완주",
                    f"{deadline_sec // 60}분 경과, 발송 완료 없음 — 로그 확인 필요",
                )
        except asyncio.CancelledError:
            pass

    watchdog = asyncio.create_task(_watchdog())
    try:
        await func(bot)
    finally:
        done.set()
        watchdog.cancel()


@dataclass
class BotSpec:
    """오케스트레이터가 인식하는 단일 봇 등록 정보."""
    name: str                                       # 'company', 'industry', ...
    token_env: str                                  # 토큰 환경변수 이름
    builder: Callable[[str], Application]           # build_xxx_app(token) → Application
    jobs: list[ScheduledJob] = field(default_factory=list)
    # 텔레그램 / 자동완성 명령 [(command, description), ...]. orchestrator가
    # app.initialize() 후 app.bot.set_my_commands로 직접 등록.
    # post_init은 manual lifecycle (initialize/start/start_polling)에서 호출
    # 안 되므로 spec 단위로 직접 보관.
    commands: list[tuple[str, str]] = field(default_factory=list)
    optional: bool = True                           # False면 토큰 없을 때 systemexit


async def _model_router_weekly_job(bot: Bot) -> None:
    """주간 모델 가성비 재평가 cron — 일요일 21:00 KST."""
    from src.model_router.handler import model_eval_job
    await model_eval_job(bot)


async def _model_health_cron_job(bot: Bot) -> None:
    """Layer D 자동 rollback 검사 — 시간당."""
    from src.model_router.handler import model_health_job
    await model_health_job(bot)


BOT_SPECS: list[BotSpec] = [
    BotSpec(
        name="company",
        token_env="TELEGRAM_BOT_TOKEN",
        builder=build_company_app,
        commands=COMPANY_COMMANDS,
        jobs=[
            # model_router cron — 버터대디봇(report) 사망으로 이관 (2026-09).
            # 살아있는 봇 토큰이면 어떤 봇이든 무방 — 알림은 handler가 env로 라우팅.
            ScheduledJob(
                func=_model_router_weekly_job,
                job_id="model_router_weekly",
                cron={"day_of_week": "sun", "hour": 21, "minute": 0},
                description="모델 가성비 주간 재평가 — 매주 일요일 21:00 KST",
            ),
            ScheduledJob(
                func=_model_health_cron_job,
                job_id="model_health_hourly",
                cron={"minute": 17},
                description="Layer D — 모델 health 검사 + 자동 rollback (시간당 17분)",
            ),
        ],
    ),
    BotSpec(
        name="industry",
        token_env="INDUSTRY_BOT_TOKEN",
        builder=build_industry_app,
        commands=INDUSTRY_COMMANDS,
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
        commands=MARKET_COMMANDS,
        jobs=[
            ScheduledJob(
                func=market_daily_job,
                job_id="market_daily",
                cron={"hour": 9, "minute": 0},
                description="시황 신규 — 매일 09:00 KST",
            ),
            ScheduledJob(
                func=channel_relay_job,
                job_id="channel_relay",
                # 20분 폴링 — "저자 글 3시간 이내 전달" 요구에 충분한 여유.
                cron={"minute": "*/20"},
                description="t.me/DSInvResearch 양형모 글 릴레이 — 20분 폴링",
            ),
        ],
    ),
    BotSpec(
        name="global",
        token_env="GLOBAL_BOT_TOKEN",
        builder=build_global_app,
        commands=GLOBAL_COMMANDS,
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
        commands=IDEA_COMMANDS,
        jobs=[],  # 사용자 입력 기반, 스케줄 없음
    ),
    BotSpec(
        name="earnings",
        token_env="EARNINGS_BOT_TOKEN",
        builder=build_earnings_app,
        commands=EARNINGS_COMMANDS,
        jobs=[
            ScheduledJob(
                func=earnings_watch_poll_job,
                job_id="earnings_watch_poll",
                # 4시간 간격 — Yahoo calendarEvents로 D-1~D+4 윈도 게이팅. AV 25/day 안전.
                cron={"hour": "0,4,8,12,16,20", "minute": 7},
                description="어닝콜 watch 자동 감지 — 4h 간격 (calendar 게이팅)",
            ),
            ScheduledJob(
                func=earnings_digest_cron_job,
                job_id="earnings_digest_weekly",
                cron={"day_of_week": "mon", "hour": 9, "minute": 0},
                description="watchlist 주간 digest — 매주 월 09:00 KST (Phase 7F)",
            ),
        ],
    ),
    BotSpec(
        name="disclosure",
        token_env="DISCLOSURE_BOT_TOKEN",
        builder=build_disclosure_app,
        commands=DISCLOSURE_COMMANDS,
        jobs=[
            ScheduledJob(
                func=disclosure_poll_job,
                job_id="disclosure_poll",
                # 5분마다. 비거래 시간엔 disclosure_poll_job 내부에서 minute 0/30만 실행.
                cron={"minute": "*/5"},
                description="DART 공시 폴링 — 5분 (거래시간) / 30분 (외부)",
            ),
        ],
    ),
    BotSpec(
        name="screener",
        token_env="SCREENER_BOT_TOKEN",
        builder=build_screener_app,
        commands=SCREENER_COMMANDS,
        jobs=[
            ScheduledJob(
                func=screener_daily_job,
                job_id="screener_daily",
                # mon-fri — 주말 구조적 휴장 제외. 평일 공휴일은 봇 내부의
                # 데이터 기반 휴장 판정(last_sent_base_date 중복 skip)이 처리.
                cron={"day_of_week": "mon-fri", "hour": 16, "minute": 0},
                description="한국 주식 기술적 신호 — 평일 16:00 KST (15:30 종가 기준)",
                # 90분 — 실측 base case ~48분(약 1200종목 Naver 재fetch + today-fetch).
                # 최악(KRX 미발행 시 30분 retry 누적)까지 여유. 종전 45분은 매일 거짓
                # 미완주 알림 발생(2026-06-16 16:48 완주인데 16:45 알림) → 알람 피로.
                deadline_sec=5400,
                alert_env_keys=("SCREENER_ALLOWED_CHAT_IDS", "SCREENER_CHAT_ID"),
            ),
        ],
    ),
    BotSpec(
        name="us_screener",
        token_env="US_SCREENER_BOT_TOKEN",
        builder=build_us_screener_app,
        commands=US_SCREENER_COMMANDS,
        jobs=[
            ScheduledJob(
                func=us_screener_daily_job,
                job_id="us_screener_daily",
                # tue-sat — 미국 금요일 종가는 토요일 07:00 KST에 커버되므로 토요일 유지,
                # 일·월 아침(미국 주말)만 제외. 미국 공휴일은 봇 내부 데이터 기반 skip이 처리.
                cron={"day_of_week": "tue-sat", "hour": 7, "minute": 0},
                description="미국 기술적 신호 — 화~토 07:00 KST (미국 4PM ET 종가)",
                # 40분 — Naver 없어 KR보다 짧지만, 차트 채널 게시(종목당 3s pacing)+
                # FMP 어닝 조회가 추가돼 25분은 빠듯. 거짓 미완주 방지 여유.
                deadline_sec=2400,
                alert_env_keys=("US_SCREENER_ALLOWED_CHAT_IDS", "US_SCREENER_CHAT_ID"),
            ),
        ],
    ),
    BotSpec(
        name="report",
        token_env="REPORT_BOT_TOKEN",
        builder=build_report_app,
        commands=REPORT_COMMANDS,
        jobs=[
            ScheduledJob(
                func=report_daily_job,
                job_id="report_daily",
                cron={"hour": 8, "minute": 0},
                description="버터대디봇 시황 리포트 — 매일 08:00 KST",
            ),
        ],
    ),
    BotSpec(
        name="recap",
        token_env="RECAP_BOT_TOKEN",
        builder=build_recap_app,
        commands=RECAP_COMMANDS,
        jobs=[
            ScheduledJob(
                func=recap_weekly_job,
                job_id="recap_weekly",
                cron={"day_of_week": "sun", "hour": 19, "minute": 0},
                description="주간 회고 — 매주 일요일 19:00 KST (signal + ideas + themes + sonnet 합성)",
            ),
        ],
    ),
]


# ------------------------------------------------------------------
# 진단: inject된 관련 env vars 출력
# ------------------------------------------------------------------
def _diag_env() -> None:
    relevant = diag_env_keys(
        ("TELEGRAM", "WISE", "OPEN", "ALLOWED", "CHAT", "INDUSTRY", "MARKET",
         "GLOBAL", "DART", "IDEA", "DISCLOSURE", "SCREENER", "EARNINGS", "SEC")
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
    # pykrx 내부 wrapper가 KRX 빈응답 시 `logging.info(args, kwargs)` 잘못 호출 →
    # TypeError + Traceback 폭주 (실제 기능은 graceful skip). logging 자체의
    # exception 전파를 끄면 stderr noise 사라짐. Python 권장: 앱 시작 시 1회.
    logging.raiseExceptions = False
    logging.getLogger("pykrx").setLevel(logging.CRITICAL)

    log = logging.getLogger("orchestrator")
    _diag_env()

    # state_store 진단 — 볼륨 mount + 영구 보존 여부 즉시 검증
    try:
        from src import state_store, watchlist_store
        state_store.diag_log()
        watchlist_store.diag_log()
    except Exception:
        log.exception("state_store 진단 호출 실패 (orchestrator 계속 진행)")

    # idea_cache 무한 증가 방지 — 부팅 시 1회 cleanup (default 200 cap).
    # 별도 cron 없이 봇 재시작마다 정리되어 디스크 사용 안정.
    try:
        from src import idea_cache
        removed = idea_cache.cleanup_old(keep=200)
        if removed:
            log.info("idea_cache cleanup: removed=%d (keep=200)", removed)
    except Exception:
        log.exception("idea_cache cleanup 실패 (orchestrator 계속 진행)")

    apps: list[tuple[str, Application]] = []
    bot_objects: dict[str, Bot] = {}
    pending_jobs: list[tuple[str, ScheduledJob]] = []

    # 분리 배포 가드 — ACTIVE_BOTS="screener,us_screener" 식으로 설정하면 그 봇만 기동.
    # 미설정이면 기존처럼 토큰 있는 봇 전부. 두 Railway 서비스가 같은 토큰을 공유할 때
    # 동일 봇이 양쪽에서 getUpdates 폴링 → 텔레그램 409 Conflict 나는 사고 방지.
    active_filter = {
        x.strip() for x in os.getenv("ACTIVE_BOTS", "").split(",") if x.strip()
    }
    if active_filter:
        log.info("ACTIVE_BOTS 필터 적용: %s", sorted(active_filter))

    for spec in BOT_SPECS:
        if active_filter and spec.name not in active_filter:
            log.info("%sBot — ACTIVE_BOTS 필터로 스킵", spec.name)
            continue
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
        # ACTIVE_BOTS로 전부 비활성화 시 crash 대신 idle (잠정폐기 안전망).
        # Railway는 exit 시 즉시 재시작 → crash loop. asyncio.sleep으로 컨테이너 살림.
        log.warning("활성화된 봇이 하나도 없음 — idle 모드로 대기 "
                    "(ACTIVE_BOTS 환경변수로 봇이 전부 필터되었거나 토큰 미설정).")
        log.warning("복원: ACTIVE_BOTS env 삭제 또는 정상값 설정 후 재배포.")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            return

    # spec → commands 매핑 (initialize 후 set_my_commands 직접 호출용)
    spec_by_name = {s.name: s for s in BOT_SPECS}

    # 모든 봇 시작 (lifecycle 수동 관리)
    for name, app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        # 텔레그램 / 자동완성 명령 등록 — manual lifecycle은 post_init이 자동
        # 호출 안 되므로 여기서 직접 호출.
        spec = spec_by_name.get(name)
        if spec and spec.commands:
            try:
                await app.bot.set_my_commands(
                    [BotCommand(c, d) for c, d in spec.commands]
                )
                log.info("%s set_my_commands 등록: %d개", name, len(spec.commands))
            except Exception:
                log.exception("%s set_my_commands 실패 (봇은 정상 가동)", name)
        log.info("%s 폴링 시작", name)

    # APScheduler — KST 기준 cron
    scheduler = AsyncIOScheduler(timezone=KST)
    for bot_name, job in pending_jobs:
        bot = bot_objects.get(bot_name)
        if bot is None:
            continue
        scheduler.add_job(
            _instrumented_job,
            CronTrigger(timezone=KST, **job.cron),
            args=[job.func, bot, job.job_id, job.deadline_sec, job.alert_env_keys],
            id=job.job_id,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        log.info("스케줄: %s (deadline %ds)", job.description or job.job_id, job.deadline_sec)

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
