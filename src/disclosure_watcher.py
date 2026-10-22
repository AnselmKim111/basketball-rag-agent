"""DART 공시 5분 폴링 + 텔레그램 fan-out.

실행 단위:
  - APScheduler 등록 후 5분마다 `poll_and_dispatch(bot)` 호출.
  - watchlist의 union ticker 셋만 DART에 쿼리 (chat 단위 아님 → quota 절약).
  - 신규 공시 발견 → ticker별 subscribers에 fan-out (chat 단위 dedup).

dedup:
  - state_store의 `seen()` / `mark_seen_with_titles()` 그대로 재사용.
  - 카테고리 키: `"disc_{chat_id}"` — chat마다 독립 dedup state.
  - 공시는 rcept_no가 unique → 그걸로만 dedup (title 정규화는 안 씀).
  - 발송 성공한 rcept_no만 seen 처리 — 실패분은 다음 폴링에 재시도 (2026-10-21 텔레그램
    토큰 교체·장애 시 공시 유실 방지). 같은 (chat, rcept_no)가 `DISCLOSURE_SEND_MAX_ATTEMPTS`
    (기본 12회 ≈ 거래시간 1시간)번 연속 실패하면 포기하고 seen 처리 — 봇 차단·chat 삭제 같은
    영구 실패가 매 폴링 무한 재시도되지 않게. 실패 횟수는 프로세스 메모리 (재배포 시 0부터).
    DART 조회 창이 1일이라 그보다 오래된 미발송 공시는 어차피 다시 안 잡힘.

알림 메시지:
  공시명 + 발생 시각 + 카테고리(critical/normal) 표시.
  critical은 LLM 1-2줄 요약 시도 (kimi). 실패 시 공시명만.

품질 가드:
  - 모든 외부 호출 try/except — 한 ticker 실패가 전체 폴링 죽이지 않음.
  - 폴링 한 사이클 내에서 같은 corp_code는 한 번만 호출.
  - 거래 시간 외 (KST 18:00-08:30) 폴링 빈도 1시간으로 떨어뜨림.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any

from telegram import Bot

from src import state_store, watchlist_store
from src.deepdive import dart_client

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 거래 시간 (KST). 이 윈도우 밖이면 폴링 1시간 간격으로 동작 (스케줄러 측에서).
MARKET_OPEN_HOUR = 8
MARKET_CLOSE_HOUR = 18

# 알림 메시지 prefix별 카테고리 이모지
CATEGORY_EMOJI = {
    "critical": "🚨",
    "normal": "📄",
}

# 발송 실패 재시도 상한 (chat·rcept_no 단위). 넘으면 포기 + seen 처리.
SEND_MAX_ATTEMPTS_DEFAULT = 12
# (chat_id, rcept_no) → 연속 발송 실패 횟수. 성공·포기 시 삭제 → 크기는 미발송분으로 bounded.
_SEND_FAILS: dict[tuple[str, str], int] = {}


def _send_max_attempts() -> int:
    try:
        return max(1, int(os.getenv("DISCLOSURE_SEND_MAX_ATTEMPTS", str(SEND_MAX_ATTEMPTS_DEFAULT))))
    except ValueError:
        return SEND_MAX_ATTEMPTS_DEFAULT


def _note_send_failure(chat_id: str | int, rcept_no: str) -> bool:
    """발송 실패 1회 기록. 상한 도달(포기 → seen 처리 대상)이면 True."""
    key = (str(chat_id), rcept_no)
    n = _SEND_FAILS.get(key, 0) + 1
    if n >= _send_max_attempts():
        _SEND_FAILS.pop(key, None)
        return True
    _SEND_FAILS[key] = n
    return False


def is_market_hours() -> bool:
    """KST 8:00-18:00 사이면 True (정정공시·잠정실적 마감시간 마진 포함)."""
    now = datetime.now(KST).time()
    return time(MARKET_OPEN_HOUR, 0) <= now <= time(MARKET_CLOSE_HOUR, 0)


def _ticker_to_corp_code(ticker: str) -> tuple[str | None, str]:
    """ticker → (corp_code, name). 매핑 실패 시 (None, ticker)."""
    res = dart_client.get_corp_code(ticker)
    if res:
        return res[0], res[1]
    return None, ticker


def _log_disclosure_for_recap(
    rcept_no: str, chat_id: str, ticker: str,
    name: str, report_nm: str, category: str,
) -> None:
    """알림 발송 직후 screener.db disclosure_log에 영속화 (blocking — executor에서).

    baseline_close: screener.db ohlcv의 최근 종가 (그날 회고에서 '공시 후 수익률'
    기준점). watchlist 종목이 screener universe 밖이면 None — 회고에서 PnL 스킵.
    """
    from src.screener import db as screener_db
    baseline_close = None
    try:
        rows = screener_db.load_ohlcv(ticker, days=1)
        if rows:
            baseline_close = int(rows[-1]["close"])
    except Exception:
        log.debug("baseline_close 조회 실패 (무시): %s", ticker)
    screener_db.disclosure_log_insert(
        rcept_no=rcept_no, chat_id=chat_id, ticker=ticker,
        name=name, report_nm=report_nm, category=category,
        alert_date=datetime.now(KST).date().isoformat(),
        baseline_close=baseline_close,
    )


def _format_alert(ticker: str, name: str, report: dart_client.DartReport, category: str) -> str:
    """텔레그램 알림 메시지 텍스트 (마크다운 안 씀 — 안전).

    예:
      🚨 [삼성전자 005930]
      잠정실적공시
      2026-05-05  접수번호 20260505000123
    """
    emoji = CATEGORY_EMOJI.get(category, "📄")
    rcept_dt = report.rcept_dt or "?"
    if len(rcept_dt) == 8:
        rcept_dt = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}"
    return (
        f"{emoji} [{name} {ticker}]\n"
        f"{report.report_nm}\n"
        f"{rcept_dt}  접수번호 {report.rcept_no}"
    )


async def poll_and_dispatch(bot: Bot) -> None:
    """단일 폴링 사이클. 모든 watched ticker 한 번씩 DART 호출 → 신규 공시 fan-out.

    APScheduler가 호출. 예외는 사이클을 죽이지 않음 (다음 5분에 재시도).
    """
    tickers = watchlist_store.all_tickers()
    if not tickers:
        log.debug("disclosure_watcher: watchlist 비어있음 — skip")
        return

    log.info("disclosure_watcher 폴링 시작: %d tickers", len(tickers))
    new_count = 0
    error_count = 0
    loop = asyncio.get_running_loop()

    for ticker in sorted(tickers):
        try:
            corp_code, name = await loop.run_in_executor(None, _ticker_to_corp_code, ticker)
            if not corp_code:
                log.warning("corp_code 못 찾음: %s", ticker)
                continue
            reports = await loop.run_in_executor(
                None, dart_client.fetch_recent_disclosures, corp_code, 1,
            )
            if not reports:
                continue

            subscribers = watchlist_store.subscribers_of(ticker)
            if not subscribers:
                continue

            for chat_id in subscribers:
                dedup_key = f"disc_{chat_id}"
                seen = state_store.seen(dedup_key)
                fresh = [r for r in reports if r.rcept_no not in seen]
                if not fresh:
                    continue

                # 발송 (오래된 것부터). seen 처리는 발송 성공분 + 재시도 포기분만.
                done: list[str] = []
                for r in reversed(fresh):
                    category = dart_client.classify_disclosure(r.report_nm)
                    text = _format_alert(ticker, name, r, category)
                    try:
                        await bot.send_message(chat_id=chat_id, text=text)
                        new_count += 1
                    except Exception:
                        log.exception("공시 알림 발송 실패: chat=%s ticker=%s", chat_id, ticker)
                        error_count += 1
                        if _note_send_failure(chat_id, r.rcept_no):
                            log.warning(
                                "공시 알림 발송 %d회 연속 실패 — 포기(seen 처리): chat=%s rcept_no=%s",
                                _send_max_attempts(), chat_id, r.rcept_no,
                            )
                            done.append(r.rcept_no)
                        continue
                    done.append(r.rcept_no)
                    _SEND_FAILS.pop((str(chat_id), r.rcept_no), None)
                    # 회고 학습 루프용 영속화 (RecapBot §3). 실패해도 알림 흐름엔 무영향.
                    try:
                        await loop.run_in_executor(
                            None, _log_disclosure_for_recap,
                            r.rcept_no, chat_id, ticker, name, r.report_nm, category,
                        )
                    except Exception:
                        log.exception("disclosure_log 영속화 실패: %s", r.rcept_no)

                # dedup state 갱신 (한 chat 단위) — 실패분은 남겨 다음 폴링에 재시도
                if not done:
                    continue
                try:
                    state_store.mark_seen(dedup_key, done, cap=2000)
                except Exception:
                    log.exception("dedup state 갱신 실패: chat=%s", chat_id)

        except Exception:
            log.exception("disclosure poll 한 ticker 실패: %s", ticker)
            error_count += 1

    log.info(
        "disclosure_watcher 폴링 완료: tickers=%d, 신규알림=%d, 에러=%d",
        len(tickers), new_count, error_count,
    )


# ------------------------------------------------------------------
# 부팅 시 dedup state seeding — 처음 등록한 ticker가 과거 공시로 한꺼번에
# 100건 폭격 안 맞게.
# ------------------------------------------------------------------
async def seed_dedup_for_new_ticker(chat_id: str | int, ticker: str) -> int:
    """ticker 추가 직후 호출. 현재 시점의 모든 공시를 "이미 본 것"으로 마킹.

    다음 폴링부터 신규만 알림. (False positive 알림 폭탄 방지.)
    Returns: seeded count.
    """
    loop = asyncio.get_running_loop()
    try:
        corp_code, _name = await loop.run_in_executor(None, _ticker_to_corp_code, ticker)
        if not corp_code:
            return 0
        reports = await loop.run_in_executor(
            None, dart_client.fetch_recent_disclosures, corp_code, 7,
        )
        if not reports:
            return 0
        rcept_nos = [r.rcept_no for r in reports if r.rcept_no]
        state_store.mark_seen(f"disc_{chat_id}", rcept_nos, cap=2000)
        log.info("seed dedup: chat=%s ticker=%s seeded=%d", chat_id, ticker, len(rcept_nos))
        return len(rcept_nos)
    except Exception:
        log.exception("seed_dedup_for_new_ticker 실패: chat=%s ticker=%s", chat_id, ticker)
        return 0
