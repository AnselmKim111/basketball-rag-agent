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

봇 단위 격리 (2026-10-21 유출 사고 후속): 봇 하나가 시작 실패(토큰 거부 등)해도 그 봇만
빠지고 나머지는 가동, 그 봇의 cron은 첫 가동 봇이 대행. 토큰 거부(InvalidToken)만 영구
실패 — 그 외(TimedOut·NetworkError 등 일시 장애)는 라운드 단위 재시도(기본 5/15/45초).
전부 실패 시: 전부 토큰 거부면 idle(exit하면 Railway 재시작 crash loop), 일시 장애가 하나라도
섞이면 exit 1 → Railway ON_FAILURE 재시작이 복구. 부팅 시 자격증명 점검 1회(src/cred_check.py).

Railway는 Dockerfile의 CMD가 이 모듈로 향함.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
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

try:
    from src import log_redact as _log_redact
except ImportError:  # 병행 작업 중 모듈 부재 — orchestrator 자체는 계속 import 가능해야 함
    _log_redact = None

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


async def _recap_weekly_via_company(bot: Bot) -> None:
    """주간 회고 cron을 종목봇으로 대행 — 전용 회고봇 토큰이 없을 때만 (중복 발송 가드)."""
    if os.getenv("RECAP_BOT_TOKEN"):
        logging.getLogger("orchestrator").info(
            "recap_weekly(company): RECAP_BOT_TOKEN 있음 → 전용 봇이 담당, 스킵")
        return
    await recap_weekly_job(bot)


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
            ScheduledJob(
                func=_recap_weekly_via_company,
                job_id="recap_weekly_company",
                cron={"day_of_week": "sun", "hour": 19, "minute": 0},
                description="주간 회고(종목봇 대행) — 일 19:00 KST, RECAP_BOT_TOKEN 없을 때만",
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
         "GLOBAL", "DART", "IDEA", "DISCLOSURE", "SCREENER", "EARNINGS", "SEC",
         "RAILWAY_PROJECT_ACCESS", "RAILWAY_ACCESS", "RAILWAY_SERVICE_ID")
    )
    print(f"[orch] env keys total = {len(os.environ)}", flush=True)
    print(f"[orch] relevant env vars = {relevant}", flush=True)


def _configure_logging() -> None:
    """로깅 설정 (멱등) — _run_forever 시작 + main()의 치명적 오류 경로 공용."""
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
    # 2026-10-21 Railway env 유출 후속 — 모든 로그 라인(예외 traceback 포함)에서
    # 토큰·키 값 마스킹. 로그는 호스트에 평문 보관되므로 basicConfig 직후 1회.
    if _log_redact is not None:
        _log_redact.install_redaction()


# ------------------------------------------------------------------
# 봇 단위 격리 (2026-10-21 유출 사고 후속)
# ------------------------------------------------------------------
# 사고: 토큰 폐기 후 app.initialize()(→ get_me)의 InvalidToken이 asyncio.run 밖으로
# 새어 프로세스 종료 → Railway 재시작(최대 10회) crash loop로 멀쩡한 봇까지 전부 다운 +
# traceback에 "The token `<토큰>` was rejected" 원문 출력. 이제 봇 하나 실패 = 그 봇만 스킵.
def _fingerprint(value: str) -> str:
    if _log_redact is None:
        return "?"
    try:
        return _log_redact.fingerprint(value)
    except Exception:
        return "?"


def _invalid_token_cls() -> type | None:
    """telegram.error.InvalidToken — 테스트 stub엔 telegram.error가 없을 수 있음."""
    try:
        from telegram.error import InvalidToken
    except ImportError:
        return None
    if isinstance(InvalidToken, type) and issubclass(InvalidToken, BaseException):
        return InvalidToken
    return None


async def _safe_shutdown(app: Application, name: str) -> None:
    """부분 기동된 app 정리 (best-effort). 예외 텍스트는 토큰을 담을 수 있어 타입명만."""
    log = logging.getLogger("orchestrator")
    updater = getattr(app, "updater", None)
    steps = []
    if updater is not None and getattr(updater, "running", False) is True:
        steps.append(updater.stop)
    if getattr(app, "running", False) is True:
        steps.append(app.stop)
    steps.append(app.shutdown)
    for step in steps:
        try:
            await step()
        except Exception as e:
            log.debug("%s 정리 단계 실패 (%s) — 무시", name, type(e).__name__)


# 시작 재시도 (2026-10-22 리뷰): 토큰 거부만 영구 — 부팅 순간 텔레그램·DNS·프록시 일시
# 장애(get_me TimedOut 등, PTB는 initialize에서 재시도 안 함)는 실패 봇만 모아 라운드 재시도.
# 값 개수 = 재시도 횟수. env BOT_START_RETRY_BACKOFF_S="5,15,45" (off = 재시도 없음).
_START_BACKOFF_S: tuple[float, ...] = (5.0, 15.0, 45.0)
_START_BACKOFF_MAX_S = 600.0  # 값 하나당 상한 (오설정으로 부팅이 무한 대기하지 않게)


def _start_backoff_s() -> tuple[float, ...]:
    raw = os.getenv("BOT_START_RETRY_BACKOFF_S", "").strip()
    if not raw:
        return _START_BACKOFF_S
    if raw.lower() in ("off", "none", "-"):
        return ()
    try:
        vals = [float(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        logging.getLogger("orchestrator").warning(
            "BOT_START_RETRY_BACKOFF_S 형식 오류 — 기본값 %s 사용", _START_BACKOFF_S)
        return _START_BACKOFF_S
    # 음수·NaN → 0, inf·과대값 → 상한
    return tuple(min(max(0.0, v), _START_BACKOFF_MAX_S) for v in vals)


async def _try_start(app: Application) -> Exception | None:
    """initialize → start → start_polling. 실패 시 예외를 raise 대신 반환 — 호출부가
    except 블록 밖에서 정리·로그 (정리 중 예외의 traceback에 원 예외(토큰 포함 메시지)가
    "During handling of..."로 딸려 나가지 않게)."""
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        return e
    return None


async def _set_commands(app: Application, name: str, spec: BotSpec | None) -> None:
    """텔레그램 / 자동완성 명령 등록 — manual lifecycle은 post_init이 자동 호출 안 되므로 직접."""
    if not (spec and spec.commands):
        return
    log = logging.getLogger("orchestrator")
    try:
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in spec.commands]
        )
        log.info("%s set_my_commands 등록: %d개", name, len(spec.commands))
    except Exception:
        log.exception("%s set_my_commands 실패 (봇은 정상 가동)", name)


async def _start_apps(
    apps: list[tuple[str, Application]],
    spec_by_name: dict[str, BotSpec],
) -> tuple[list[tuple[str, Application]], list[str], dict[str, str], set[str]]:
    """봇별 initialize/start/start_polling + set_my_commands — 실패한 봇만 건너뜀.

    1라운드에 전 봇을 1회씩 시도(정상 봇은 즉시 가동) → 일시 장애 봇만 backoff 후 재시도
    (봇 하나 때문에 나머지 기동이 늦어지지 않게). 토큰 거부(InvalidToken)는 재시도 없음.
    시도 사이엔 _safe_shutdown — PTB는 shutdown 후 재-initialize 지원.

    반환: (가동 중 apps, 실패 봇 이름 list, {실패 봇 이름: 짧은 사유},
          영구 실패(토큰 거부) 봇 이름 set). list는 입력(BOT_SPECS) 순서 유지.
    """
    log = logging.getLogger("orchestrator")
    invalid_token = _invalid_token_cls()
    order = {name: i for i, (name, _) in enumerate(apps)}
    backoff = _start_backoff_s()
    total = len(backoff) + 1
    running: list[tuple[str, Application]] = []
    reasons: dict[str, str] = {}
    permanent: set[str] = set()
    pending = list(apps)
    for attempt in range(1, total + 1):
        if attempt > 1:
            delay = backoff[attempt - 2]
            log.warning("봇 시작 재시도 %d/%d: %s — %.0f초 후",
                        attempt - 1, len(backoff), ", ".join(n for n, _ in pending), delay)
            await asyncio.sleep(delay)
        retry: list[tuple[str, Application]] = []
        for name, app in pending:
            spec = spec_by_name.get(name)
            token_env = spec.token_env if spec else "?"
            err = await _try_start(app)
            if err is None:
                reasons.pop(name, None)
                await _set_commands(app, name, spec)
                running.append((name, app))
                if attempt > 1:
                    log.info("%s 폴링 시작 (시도 %d회째)", name, attempt)
                else:
                    log.info("%s 폴링 시작", name)
                continue
            if invalid_token is not None and isinstance(err, invalid_token):
                # 예외 메시지에 토큰 원문 포함 → 텍스트·traceback 미출력, fingerprint만
                log.error("%s 토큰 거부(fp=%s) — %s 봇 건너뜀",
                          token_env, _fingerprint(os.getenv(token_env, "")), name)
                reasons[name] = "토큰 거부"
                permanent.add(name)
            elif attempt < total:
                # 중간 실패는 타입명만 — traceback은 최종 실패 1회
                log.warning("%sBot 시작 실패 (%s) — 시도 %d/%d, 재시도 예정",
                            name, type(err).__name__, attempt, total)
                reasons[name] = f"시작 실패 ({type(err).__name__})"
                retry.append((name, app))
            else:
                # traceback은 마스킹 formatter(install_redaction) 경유
                log.error("%sBot 시작 실패 — %d회 시도 후 %s 봇 건너뜀",
                          name, total, name, exc_info=err)
                reasons[name] = f"시작 실패 ({type(err).__name__})"
            del err
            await _safe_shutdown(app, name)
        pending = retry
        if not pending:
            break
    # 재시도로 늦게 뜬 봇도 원래 자리 — apps[0](첫 가동 봇)·cron 대행 봇 선택이 순서에 의존
    running.sort(key=lambda item: order[item[0]])
    failed = [name for name, _ in apps if name in reasons]
    return running, failed, reasons, permanent


def _schedule_jobs(scheduler, pending_jobs: list[tuple[str, ScheduledJob]],
                   bot_objects: dict[str, Bot], failed_bots: list[str]) -> None:
    """크론 등록. 소유 봇이 시작 실패면 첫 번째 가동 봇으로 대행 (발송 대상 chat은 job이
    env로 정하므로 그대로 — 바뀌는 건 보내는 봇 객체뿐). 가동 봇이 없으면 경고 후 스킵."""
    log = logging.getLogger("orchestrator")
    fallback_name = next(iter(bot_objects), None)
    for bot_name, job in pending_jobs:
        bot = bot_objects.get(bot_name)
        if bot is None:
            if bot_name not in failed_bots:
                continue
            if fallback_name is None:
                log.warning("스케줄 스킵: %s — 소유 봇 %s 시작 실패 + 대행할 가동 봇 없음",
                            job.job_id, bot_name)
                continue
            bot = bot_objects[fallback_name]
            log.warning("스케줄 대행: %s — 소유 봇 %s 시작 실패 → %s 봇이 발송 (대상 chat 동일)",
                        job.job_id, bot_name, fallback_name)
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


def _warn_idle_secrets() -> None:
    """idle 서비스(ACTIVE_BOTS=_disabled_ 등)가 쓰지도 않는 시크릿을 보유 → 유출 노출면.
    env 이름만 출력 (값 절대 금지)."""
    if _log_redact is None:
        return
    names = sorted(k for k in os.environ if _log_redact.is_secret_name(k))
    if names:
        logging.getLogger("orchestrator").warning(
            "idle 서비스가 시크릿 %d개 보유: %s — 삭제 권장 (안 쓰는 값은 유출 시 노출면만 늘림)",
            len(names), ", ".join(names))


async def _idle_forever(stop_event: asyncio.Event | None = None) -> None:
    """종료하지 않고 대기 — Railway는 exit 시 즉시 재시작 → crash loop 방지."""
    try:
        if stop_event is not None:
            await stop_event.wait()
            return
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        return


# ------------------------------------------------------------------
# 관리자 알림 — 대상 chat은 model_router cron 알림과 동일한 해석 순서 재사용
# ------------------------------------------------------------------
def _admin_chat_id() -> str | None:
    """REPORT_CHAT_ID → MODEL_ROUTER_CHAT_ID → ALLOWED_CHAT_IDS 첫 숫자 id."""
    try:
        from src.model_router.handler import _cron_notify_chat_id
        return _cron_notify_chat_id()
    except Exception:
        logging.getLogger("orchestrator").exception("관리자 chat 해석 실패")
        return None


async def _send_admin_text(bot: Bot, chat_id: str, text: str) -> bool:
    """plain text 발송(parse_mode 없음 — 파싱 위험 0). 실패는 타입명만 경고."""
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as e:
        logging.getLogger("orchestrator").warning(
            "관리자 알림 발송 실패 (%s)", type(e).__name__)
        return False


def _clear_auth_alerted() -> None:
    """401 관리자 알림 미전달 → summarizer 알림 기록 해제 (다음 차단 때 재알림). 실패는 무시."""
    try:
        from src import summarizer
        getattr(summarizer, "clear_auth_alerted", lambda: None)()
    except Exception:
        logging.getLogger("orchestrator").debug("[auth_alert] clear_auth_alerted 실패 (무시)")


def _make_auth_alert_hook(bot: Bot, loop: asyncio.AbstractEventLoop) -> Callable[[str], None] | None:
    """폴백 hook — model_router.handler.make_auth_alert_hook이 없을 때만 사용.

    summarizer는 hook 호출 시점에 알림 시각을 기록해 6h 재알림을 막으므로, 미전달
    (발송 실패·취소·루프 전달 실패)이면 기록을 해제 — 다음 차단 때 다시 알림.
    """
    chat_id = _admin_chat_id()
    if not chat_id:
        return None
    log = logging.getLogger("orchestrator")

    def _on_done(fut) -> None:
        try:
            if fut.cancelled():
                log.warning("[auth_alert] 401 관리자 알림 발송 취소됨")
                _clear_auth_alerted()
                return
            exc = fut.exception()
            ok = fut.result() if exc is None else False
        except Exception:
            return
        if exc is not None:
            # 예외 메시지는 URL(토큰 포함 가능)을 담을 수 있어 타입명만
            log.warning("[auth_alert] 401 관리자 알림 발송 실패 (%s) — 알림 미전달",
                        type(exc).__name__)
        if ok is False:  # _send_admin_text가 실패를 삼키고 False 반환 (타입명은 거기서 경고)
            _clear_auth_alerted()

    def _hook(msg: str) -> None:
        # LLM 워커 스레드에서 동기 호출 — 메인 루프로 넘기고 즉시 반환 (결과 대기 금지)
        coro = _send_admin_text(bot, chat_id, msg)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            coro.close()
            log.warning("[auth_alert] 이벤트 루프 전달 실패 (%s) — 알림 생략", type(e).__name__)
            _clear_auth_alerted()
            return
        fut.add_done_callback(_on_done)

    return _hook


def _register_auth_alert_hook(bot: Bot, loop: asyncio.AbstractEventLoop) -> bool:
    """summarizer 401 breaker → 관리자 알림 1건 hook 등록 (종목봇, 없으면 첫 가동 봇이 발송).
    등록 시 True."""
    log = logging.getLogger("orchestrator")
    try:
        from src.summarizer import set_auth_alert_hook
    except ImportError:
        log.warning("summarizer.set_auth_alert_hook 없음 — OpenRouter 401 관리자 알림 미등록")
        return False
    try:
        try:
            from src.model_router.handler import make_auth_alert_hook
        except ImportError:
            make_auth_alert_hook = None
        if make_auth_alert_hook is not None:
            hook = make_auth_alert_hook(bot, loop)
        else:
            hook = _make_auth_alert_hook(bot, loop)
        if hook is None:
            log.warning("OpenRouter 401 관리자 알림 hook 미등록 (알림 대상 chat 미설정)")
            return False
        set_auth_alert_hook(hook)
    except Exception:
        log.exception("OpenRouter 401 관리자 알림 hook 등록 실패 (봇 가동엔 영향 없음)")
        return False
    log.info("OpenRouter 401 관리자 알림 hook 등록")
    return True


def _unregister_auth_alert_hook() -> None:
    try:
        from src.summarizer import set_auth_alert_hook
        set_auth_alert_hook(None)
    except Exception:
        pass


def _cred_check_enabled() -> bool:
    return os.getenv("CRED_SELF_CHECK", "1").strip() != "0"


async def _cred_check_and_notify(bot: Bot | None, bot_objects: dict[str, Bot],
                                 failed_bots: list[str], fail_reasons: dict[str, str]) -> None:
    """부팅 1회 자격증명 점검 (src/cred_check.py) → 관리자 chat 1건. bot None이면 로그만.
    메시지엔 env 이름·fingerprint·✓/✗만 — 값 없음."""
    log = logging.getLogger("orchestrator")
    try:
        from src.cred_check import run_credential_check
        text = await run_credential_check(
            bot_objects, failed_bots,
            bot_token_envs={s.name: s.token_env for s in BOT_SPECS},
            fail_reasons=fail_reasons,
        )
    except Exception:
        log.exception("자격증명 점검 실패 (봇 가동엔 영향 없음)")
        return
    log.info("자격증명 점검 결과:\n%s", text)
    if bot is None:
        return
    chat_id = _admin_chat_id()
    if not chat_id:
        log.warning("자격증명 점검: 알림 대상 chat 미설정 "
                    "(REPORT_CHAT_ID/MODEL_ROUTER_CHAT_ID/ALLOWED_CHAT_IDS) — 로그만 기록")
        return
    await _send_admin_text(bot, chat_id, text)


async def _run_forever(stop_event: asyncio.Event | None = None) -> None:
    """stop_event: 테스트용 — 주면 시그널 핸들러 대신 이 이벤트로 종료."""
    _configure_logging()

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
        _warn_idle_secrets()
        await _idle_forever(stop_event)
        return

    # spec → commands 매핑 (initialize 후 set_my_commands 직접 호출용)
    spec_by_name = {s.name: s for s in BOT_SPECS}

    # 모든 봇 시작 (lifecycle 수동 관리) — 봇 단위 격리: 실패한 봇만 빠짐
    apps, failed_bots, fail_reasons, permanent_fail = await _start_apps(apps, spec_by_name)
    for name in failed_bots:
        bot_objects.pop(name, None)
    if failed_bots:
        log.warning("시작 실패 봇 %d개: %s — 나머지 봇은 정상 가동",
                    len(failed_bots), ", ".join(failed_bots))

    if not apps:
        transient = [n for n in failed_bots if n not in permanent_fail]
        if transient:
            log.error("모든 봇 시작 실패 (%s) — 일시 장애 의심(%s) 재시도 소진 → exit 1, "
                      "Railway 재시작으로 복구.", ", ".join(failed_bots), ", ".join(transient))
        else:
            # 전부 토큰 거부(일괄 폐기 등) — exit하면 같은 토큰이 계속 거부돼 Railway 재시작
            # crash loop. 토큰 교체 후 재배포가 복구 경로.
            log.error("모든 봇 시작 실패 (%s) — 전부 토큰 거부, idle 대기. 토큰 교체 후 재배포 필요.",
                      ", ".join(failed_bots))
        if _cred_check_enabled():
            await _cred_check_and_notify(None, bot_objects, failed_bots, fail_reasons)
        if transient:
            # 예전(격리 전)처럼 비정상 종료 — main()이 SystemExit는 그대로 전파 → exit code 1
            raise SystemExit(1)
        await _idle_forever(stop_event)
        return

    first_live_name, _ = apps[0]
    first_live_bot = bot_objects[first_live_name]
    # 관리자 알림 봇 — 종목봇(TELEGRAM_BOT_TOKEN, model_router cron 소유)이 가동 중이면 종목봇,
    # 아니면 첫 가동 봇 (관리자가 종목봇과는 대화를 시작했을 가능성이 가장 높음)
    admin_bot = bot_objects.get("company") or first_live_bot

    # APScheduler — KST 기준 cron
    scheduler = AsyncIOScheduler(timezone=KST)
    _schedule_jobs(scheduler, pending_jobs, bot_objects, failed_bots)

    # Telethon 세션 생성 (env 주도, 1회성) — TG_LOGIN_PHONE/CODE 있을 때만 동작.
    # 알림은 종목봇 — 종목봇이 시작 실패한 경우에만 첫 가동 봇이 대행 (미설정 서비스는 기존대로).
    tg_login_task = None
    try:
        from src import tg_login
        tg_login_task = tg_login.schedule_boot_login(
            bot_objects.get("company")
            or (first_live_bot if "company" in failed_bots else None))
    except Exception:
        log.exception("tg_login 스케줄 실패 (orchestrator 계속 진행)")

    # OpenRouter 401 breaker(summarizer) → 관리자 알림. LLM 호출은 워커 스레드라 메인 루프로 넘김.
    loop = asyncio.get_running_loop()
    auth_hook_set = _register_auth_alert_hook(admin_bot, loop)

    scheduler.start()
    next_run = {j.id: str(j.next_run_time) for j in scheduler.get_jobs()}
    log.info("스케줄러 시작. 다음 실행 시각: %s", next_run)

    # 부팅 1회 자격증명 점검 → 관리자 chat (CRED_SELF_CHECK=0이면 끔)
    cred_task = None
    if _cred_check_enabled():
        cred_task = asyncio.create_task(
            _cred_check_and_notify(first_live_bot, bot_objects, failed_bots, fail_reasons))
    log.info("==== 모든 봇 가동 중. SIGINT/SIGTERM 까지 대기 ====")

    # 종료 시그널 대기
    if stop_event is None:
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
    await stop_event.wait()

    log.info("종료 시그널 수신 → 정리")
    if tg_login_task and not tg_login_task.done():
        tg_login_task.cancel()
    if cred_task and not cred_task.done():
        cred_task.cancel()
    if auth_hook_set:
        _unregister_auth_alert_hook()
    scheduler.shutdown(wait=False)
    for name, app in apps:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            log.exception("%s 종료 실패", name)


def main() -> None:
    try:
        asyncio.run(_run_forever())
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        # 기본 excepthook(stderr 직출력)으로 가면 토큰 담긴 예외 메시지가 그대로 남음 →
        # 마스킹 logger로 critical 1회 기록 후 비정상 종료 코드
        try:
            _configure_logging()
            logging.getLogger("orchestrator").critical(
                "orchestrator 치명적 오류로 종료 (%s)", type(e).__name__, exc_info=e)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
