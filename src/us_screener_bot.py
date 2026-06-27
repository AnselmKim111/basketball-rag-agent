"""US ScreenerBot — 미국 주식 기술적 신호 스크리너 (NYSE+NASDAQ 보통주, 시총 $2B+).

매일 07:00 KST에 자동 실행 (미국 4PM ET 장마감 + 데이터 발행 버퍼). 사용자 명령:
  /start (가입), /stop (탈퇴), /help, /screen (즉시), /status, /backfill
  /list (admin), /block <chat_id> (admin), /unblock <chat_id> (admin)

한국 ScreenerBot과 완전 격리. 자체 SQLite DB(/data/us_screener.db), 자체 봇 토큰.
시장 무관 순수 함수(compute_signals_for_ticker)는 한국 코드에서 재사용.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from src.bot_helpers import (
    deny_message,
    is_authorized,
    send_text_chunked,
)
from src.us_screener import subscribers as subs

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ALLOWED_ENV = "US_SCREENER_ALLOWED_CHAT_IDS"  # admin chat_ids (콤마 구분)
CHAT_ID_ENV = "US_SCREENER_CHAT_ID"  # legacy: 단일 chat_id (admin 호환용)


def _is_admin(update: Update) -> bool:
    """관리자 권한 (env vars의 ALLOWED_CHAT_IDS / CHAT_ID에 등록된 chat_id)."""
    return is_authorized(update, ALLOWED_ENV)


def _is_subscribed_or_admin(update: Update) -> bool:
    """관리자 또는 활성 가입자."""
    if _is_admin(update):
        return True
    cid = str(update.effective_chat.id)
    try:
        return subs.is_subscribed(cid)
    except Exception:
        log.exception("subscriber check 실패")
        return False


WELCOME_TEXT = (
    "👋 *US ScreenerBot에 오신 걸 환영합니다!*\n\n"
    "🇺🇸 미국 주식(NYSE+NASDAQ 보통주, 시총 $2B+) 기술적 신호를 매일 07:00 KST에 자동 발송합니다.\n"
    "(미국 4PM ET 장마감 + 데이터 발행 버퍼)\n\n"
    "신호:\n"
    "  🚀 역사적 신고가  📈 52주 신고가\n"
    "  🎯 52주 돌파 직전\n"
    "  💎 VCP 돌파 (변동성 수축 후 돌파)\n\n"
    "명령:\n"
    "  /screen — 지금 즉시 신호 분석 (~3-5분)\n"
    "  /help — 도움말\n"
    "  /stop — 자동 발송 해제\n\n"
    "_시총 $2B+ 미국 보통주 (NYSE+NASDAQ), 섹터별 분류, 시총·상승률 복합 정렬, "
    "이중확인 통과만 발송_"
)

HELP_TEXT = (
    "🇺🇸 *US ScreenerBot* — 미국 주식 기술적 신호\n\n"
    "자동: 매일 07:00 KST (미국 4PM ET 장마감 기준)\n"
    "데이터: Nasdaq screener 1순위 + FDR/Stooq 폴백\n"
    "유니버스: NYSE+NASDAQ 보통주, 시총 ≥ $2B (동적 갱신)\n"
    "이중확인: base_date-anchored signals + 재 fetch cross-validation\n\n"
    "신호:\n"
    "  🚀 역사적 신고가 — 종가 > 보유 데이터(최대 1400일) 최고가\n"
    "  📈 52주 신고가 — 종가 > 과거 252영업일 최고가\n"
    "  🎯 52주 돌파 직전 — 종가 = 52주고점 95-99% + 5일 거래량 ≥ ×1.3\n"
    "  💎 VCP 돌파 — 50일 박스권 + ATR 30%+ 수축 + 거래량 dry-up + 돌파\n\n"
    "명령:\n"
    "  /start — 가입 (자동 발송 활성화)\n"
    "  /stop — 탈퇴\n"
    "  /screen — 즉시 실행\n"
    "  /help — 이 메시지\n"
)


# ------------------------------------------------------------------
# 가입/탈퇴 핸들러 (누구나 가능)
# ------------------------------------------------------------------
async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — 누구나 가입. 차단된 chat_id는 거부."""
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    username = user.username if user else None
    full_name = user.full_name if user else None
    loop = asyncio.get_running_loop()
    try:
        is_new = await loop.run_in_executor(
            None, lambda: subs.subscribe(chat_id, username, full_name)
        )
    except Exception:
        log.exception("[start] subscribe 실패")
        try:
            await update.message.reply_text("⚠️ 가입 실패 — 관리자에게 문의해 주세요.")
        except Exception:
            pass
        return

    try:
        await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        log.exception("[start] welcome 발송 실패")

    # admin에게 신규 가입 알림
    if is_new:
        try:
            admin_ids = _parse_chat_ids(ALLOWED_ENV, CHAT_ID_ENV)
            for aid in admin_ids:
                if aid == chat_id:
                    continue  # admin 본인 알림 skip
                try:
                    await context.bot.send_message(
                        chat_id=aid,
                        text=(
                            f"🆕 US ScreenerBot 신규 가입자\n"
                            f"  chat_id: {chat_id}\n"
                            f"  username: @{username or '(없음)'}\n"
                            f"  name: {full_name or '(없음)'}\n\n"
                            f"차단하려면: /block {chat_id}"
                        ),
                    )
                except Exception:
                    log.exception("admin 알림 실패 aid=%s", aid)
        except Exception:
            log.exception("[start] admin 알림 절차 실패")


async def _cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop — 자가 탈퇴."""
    chat_id = str(update.effective_chat.id)
    loop = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(None, lambda: subs.unsubscribe(chat_id))
    except Exception:
        log.exception("[stop] unsubscribe 실패")
        ok = False
    try:
        await update.message.reply_text(
            "👋 탈퇴 완료. 자동 발송이 중단됩니다." if ok
            else "ℹ️ 가입되어 있지 않습니다."
        )
    except Exception:
        pass


async def _cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — admin 전용. 가입자 목록 조회."""
    if not _is_admin(update):
        await deny_message(update, "US 스크리너봇")
        return
    loop = asyncio.get_running_loop()
    try:
        items = await loop.run_in_executor(None, subs.list_all)
    except Exception:
        log.exception("[list] 실패")
        items = []
    if not items:
        await update.message.reply_text("📭 가입자 없음")
        return
    lines = [f"👥 가입자 {len(items)}명:"]
    for it in items:
        flag = "🚫" if it["is_blocked"] else "✓"
        lines.append(
            f"{flag} {it['chat_id']} @{it['username'] or '?'} ({it['full_name'] or '?'}) "
            f"— {it['subscribed_at'][:10]}"
        )
    await send_text_chunked(context.bot, str(update.effective_chat.id), "\n".join(lines))


async def _cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/block <chat_id> — admin 전용."""
    if not _is_admin(update):
        await deny_message(update, "US 스크리너봇")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용: /block <chat_id>")
        return
    target = args[0].strip()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: subs.block(target))
    await update.message.reply_text(f"🚫 차단 완료: {target}")


async def _cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await deny_message(update, "US 스크리너봇")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용: /unblock <chat_id>")
        return
    target = args[0].strip()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: subs.unblock(target))
    await update.message.reply_text(f"✓ 차단 해제: {target}")


# ------------------------------------------------------------------
# 기존 핸들러 (가입자/admin 모두 사용)
# ------------------------------------------------------------------
async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /help는 누구나 — 봇 안내용
    try:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        log.exception("help reply 실패")


async def _cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_subscribed_or_admin(update):
        await update.message.reply_text(
            "🔒 가입자만 사용 가능합니다. /start 입력으로 가입하세요."
        )
        return
    try:
        await update.message.reply_text("🔄 스크리닝 즉시 실행 중... (~2-3분 소요)")
    except Exception:
        log.exception("screen 안내 실패")
    try:
        await us_screener_daily_job(context.bot, override_chat_id=str(update.effective_chat.id))
    except Exception:
        log.exception("[screen] 실행 실패")


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "US 스크리너봇")
        return
    loop = asyncio.get_running_loop()
    try:
        from src.us_screener import db as screener_db
        info = await loop.run_in_executor(None, screener_db.status)
        await send_text_chunked(context.bot, str(update.effective_chat.id), info)
    except Exception:
        log.exception("[status] 실패")
        try:
            await update.message.reply_text("⚠️ status 실패 — 로그 확인")
        except Exception:
            pass


def _diag_report(query: str) -> str:
    """한 종목을 파이프라인 게이트별로 추적 (blocking — executor에서 호출)."""
    from src.us_screener import db, signals

    q = query.strip().upper()
    row = db.get_ticker_row(q)
    if row:
        ticker = q
    else:
        cands = db.search_tickers_by_name(query.strip())
        if not cands:
            return f"❓ '{query}' 매칭 종목 없음 (유니버스 미포함 가능성 — universe refresh 필요)"
        if len(cands) > 1:
            lines = [f"🔎 '{query}' 후보 여러 개 — 심볼로 다시:"]
            lines += [f"  {t} {n}" for t, n in cands]
            return "\n".join(lines)
        ticker = cands[0][0]
        row = db.get_ticker_row(ticker)

    base_date = db.latest_date()
    min_cap = signals._get_float_env("SCREENER_MIN_MARKET_CAP", signals.DEFAULT_MIN_MARKET_CAP)
    out = [f"🩺 진단: {ticker} ({(row or {}).get('name') or '?'})", f"기준일(base_date): {base_date}"]

    if not row:
        out.append("① 유니버스: ❌ 없음 — tickers 테이블 미존재. universe refresh 또는 신규 상장.")
        return "\n".join(out)
    out.append(f"① 유니버스: ✅ 존재 / is_active={row['is_active']}"
               + ("" if row["is_active"] else "  ⚠️ 비활성 → 신호 계산 제외"))

    cap = row.get("market_cap")
    if cap is None:
        out.append("② 시총: NULL — 필터 우회(통과). fetch 실패 의심.")
    else:
        ok = cap >= min_cap
        out.append(f"② 시총: ${cap/1e9:,.1f}B / 필터 ${min_cap/1e9:,.1f}B → "
                   + ("✅ 통과" if ok else "❌ 탈락(skipped_cap)"))

    rows = db.load_ohlcv(ticker, days=1300)
    n = len(rows)
    latest = rows[-1]["date"] if rows else "없음"
    has_base = any(r["date"] == base_date for r in rows)
    out.append(f"③ 데이터: {n}행 / 최신일 {latest} / base_date row "
               + ("✅ 보유" if has_base else "❌ 누락(skipped_no_base)"))
    if n < 60:
        out.append("   ⚠️ <60행 → silent skip (skipped_short)")

    if has_base and n >= 60:
        try:
            sigs = signals.compute_signals_for_ticker(rows, base_date=base_date)
            fired = ", ".join(sigs.keys()) if sigs else "없음"
            out.append(f"④ 발화 신호: {fired}")
        except Exception as e:
            out.append(f"④ 신호 계산 실패: {e!r}")
        import pandas as pd
        df = pd.DataFrame(rows)
        bi = df.index[df["date"] == base_date].tolist()
        if bi:
            df = df.iloc[: bi[0] + 1]
            tc = int(df.iloc[-1]["close"])
            pch = int(df["close"].iloc[:-1].max()) if len(df) > 1 else 0
            phh = int(df["high"].iloc[:-1].max()) if len(df) > 1 else 0
            out.append(f"⑤ 종가신고가 진단(cents): 오늘={tc:,} / 과거최고종가={pch:,} / 과거최고장중={phh:,}")
            if tc > pch and tc <= phh:
                out.append("   → 종가 신고가지만 과거 장중고가에 막힘 (종가기준 수정으로 해결됨)")
            elif tc > pch:
                out.append("   → 종가 신고가 + 장중고가도 돌파")
            else:
                out.append("   → 종가 신고가 아님")
    return "\n".join(out)


async def _cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/diag <심볼|종목명> — 종목 누락 원인 파이프라인 추적 (admin)."""
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "US 스크리너봇")
        return
    chat_id = str(update.effective_chat.id)
    q = " ".join(context.args or []).strip()
    if not q:
        await send_text_chunked(context.bot, chat_id, "사용법: /diag NVDA  또는  /diag Nvidia")
        return
    loop = asyncio.get_running_loop()
    try:
        report = await loop.run_in_executor(None, lambda: _diag_report(q))
    except Exception:
        log.exception("[diag] 실패")
        report = "⚠️ 진단 실패 — 로그 확인"
    await send_text_chunked(context.bot, chat_id, report)


async def _cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_ENV):
        await deny_message(update, "US 스크리너봇")
        return
    chat_id = str(update.effective_chat.id)
    try:
        await update.message.reply_text("📥 1년치 백필 시작 (~10분 소요, 진행률 push)")
    except Exception:
        pass
    loop = asyncio.get_running_loop()

    def _progress(done: int, total: int, success: int) -> None:
        # blocking thread → 메인 loop로 안전하게 push
        try:
            asyncio.run_coroutine_threadsafe(
                send_text_chunked(
                    context.bot, chat_id,
                    f"📥 백필 진행 {done}/{total} (성공 {success})",
                ),
                loop,
            )
        except Exception:
            log.exception("progress push 실패")

    try:
        from src.us_screener import backfill
        result = await loop.run_in_executor(
            None, lambda: backfill.run_full_backfill(progress_cb=_progress)
        )
        await send_text_chunked(
            context.bot, chat_id,
            f"✅ 백필 완료: success={result['success']} fail={result['fail']} rows={result['rows']}",
        )
    except Exception:
        log.exception("[backfill] 실패")
        try:
            await send_text_chunked(context.bot, chat_id, "⚠️ 백필 실패 — 로그 확인")
        except Exception:
            pass


# ------------------------------------------------------------------
# 일일 스케줄 잡 (orchestrator가 호출)
# ------------------------------------------------------------------
def _parse_chat_ids(*env_keys: str) -> list[str]:
    """env 여러 개를 합쳐서 진짜 chat_id (숫자 token) 만 추출.

    걸러내는 케이스:
      - '*' (wildcard 인증용 sentinel — 발송 대상 아님)
      - '1813560888    (← 주석)' → '1813560888' 만 추출
      - 빈 문자열 / 공백
      - 음수·0 같은 비정상 값
    """
    import re
    out: list[str] = []
    seen: set[str] = set()
    for key in env_keys:
        raw = os.getenv(key, "") or ""
        for token in raw.split(","):
            # 숫자만 (보통 9~12자리 텔레그램 chat_id)
            m = re.search(r"-?\d{6,}", token)
            if not m:
                continue
            cid = m.group(0)
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
    return out


async def us_screener_daily_job(bot: Bot, override_chat_id: str | None = None) -> None:
    """매일 16:00 cron 또는 /screen 즉시 실행.

    override_chat_id: 명시되면 그 chat_id 1명에게만 (사용자 /screen 명령용).
    None이면 모든 가입자 + admin에게 broadcast (cron용).

    신호 계산은 한 번만 수행 + 결과를 모든 대상자에게 발송 (효율).
    진행 상황(universe 빌드/백필) 메시지는 첫 대상자(주로 admin)에게만 발송.
    """
    log.info("[scheduled] us_screener_daily_job 시작 override=%s", override_chat_id)

    # 발송 대상 chat_id 리스트 결정
    if override_chat_id:
        target_chat_ids = [str(override_chat_id)]
    else:
        # admin (env) + 가입자 (DB) union
        admin_ids = _parse_chat_ids(ALLOWED_ENV, CHAT_ID_ENV)
        try:
            from src.us_screener import subscribers as _subs
            sub_ids = _subs.list_active_chat_ids()
        except Exception:
            log.exception("[scheduled] subscribers 조회 실패")
            sub_ids = []
        # admin + subscribers union, 중복 제거, '*' 같은 비정상 값 자동 필터됨
        target_chat_ids = list(dict.fromkeys(admin_ids + sub_ids))

    if not target_chat_ids:
        log.error("발송 대상 chat_id 없음 — 스킵 (admin env 또는 subscribers 등록 필요)")
        return

    log.info("[scheduled] 발송 대상 %d명: %s", len(target_chat_ids), target_chat_ids)
    progress_chat = target_chat_ids[0]  # 진행 메시지는 첫 대상자(admin 우선)

    loop = asyncio.get_running_loop()
    try:
        from src.us_screener import backfill, db, formatter, incremental, signals, universe
    except Exception:
        log.exception("[scheduled] screener 모듈 import 실패")
        try:
            await send_text_chunked(bot, progress_chat, "⚠️ screener import 실패 — 로그 확인")
        except Exception:
            pass
        return

    # 진행 메시지 helper (admin에게만)
    chat_id = progress_chat  # 기존 코드 변수명 유지 (진행 메시지용)

    uni_audit: dict = {}
    try:
        # universe 보장 — 광역 보통주($2B floor) ~1100종목 동적 재생성.
        # 매번 refresh_universe로 신규 진입(RVMD급 미드캡)·floor 미달 비활성화 반영.
        await loop.run_in_executor(None, db.ensure_schema)
        try:
            uni_audit = await loop.run_in_executor(None, universe.refresh_universe)
            count = (uni_audit or {}).get("count", 0)
            log.info("[scheduled] universe 갱신 %s", uni_audit)
            # 헬스 워치독: 평소 ~1100종목($2B floor). 베이스라인 미만이면 admin에 ⚠
            try:
                from src.admin_alerts import alert_admin, BASELINES
                if count is not None and count < BASELINES["us_caps"]:
                    await alert_admin(bot, ("US_SCREENER_ALLOWED_CHAT_IDS", "US_SCREENER_CHAT_ID"),
                                      "⚠ US universe fetch 급감",
                                      f"평소 ~1100종목, 오늘 {count}종목 — Nasdaq/FDR 소스 또는 포맷 변경 의심")
            except Exception:
                log.exception("[scheduled] us 헬스 알림 실패")
        except Exception:
            log.exception("[scheduled] universe 갱신 실패 — 신호 계산은 진행")

        # 데이터 충분성 진단 (52주 신고가는 252일+ 필요)
        lengths = await loop.run_in_executor(None, db.ticker_data_lengths)
        log.info("[scheduled] ticker_data_lengths: %s", lengths)

        # 백필 트리거 조건:
        #   - DB 비었음 (첫 실행)
        #   - 강제 (US_SCREENER_FORCE_BACKFILL=1)
        #   - 252일+ 종목이 ohlcv 보유 종목의 30% 미만 (52주 신고가 산출 불가)
        #   - active universe 대비 ohlcv 60일+ 미보유 종목 10개 초과 (신규 종목 — NASDAQ100
        #     추가분 등 — 데이터 backfill 필요). backfill_done flag 무관하게 트리거.
        #   - max_len < 1000 (5년 데이터 미확보 — 역사적 신고가 ATH 계산 불가). flag 무관.
        force = os.getenv("US_SCREENER_FORCE_BACKFILL", "0") == "1"
        rc = await loop.run_in_executor(None, db.row_count)
        ge_252 = lengths.get("ge_252", 0)
        ge_60 = lengths.get("ge_60", 0)
        total_t = lengths.get("total_tickers", 0) or 1
        max_len = lengths.get("max_len", 0)
        total_active = await loop.run_in_executor(None, lambda: len(db.get_active_tickers()))
        insufficient = (ge_252 / total_t) < 0.30
        missing = total_active - ge_60  # ohlcv 60일+ 미보유 active 종목 (신규 상장/추가분)
        backfill_done = await loop.run_in_executor(None, lambda: db.meta_get("naver_backfill_done"))
        need_backfill = (
            (rc == 0) or force
            or (insufficient and not backfill_done)
            or (missing > 10)
            or (max_len < 1000)
        )

        if need_backfill:
            reason = (
                "첫 실행" if rc == 0 else ("강제" if force
                else (f"신규 종목 {missing}개 데이터 부족" if missing > 10
                      else f"252일+ 종목 부족 ({ge_252}/{total_t})"))
            )
            await send_text_chunked(
                bot, chat_id,
                f"📥 1년치 백필 시작 ({reason}, Naver 기반 ~6-15분)",
            )

            def _progress(done: int, total: int, success: int) -> None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        send_text_chunked(
                            bot, chat_id,
                            f"📥 백필 진행 {done}/{total} (성공 {success})",
                        ),
                        loop,
                    )
                except Exception:
                    log.exception("progress push 실패")

            result = await loop.run_in_executor(
                None, lambda: backfill.run_full_backfill(progress_cb=_progress)
            )
            await send_text_chunked(
                bot, chat_id,
                f"✅ 백필 완료: mode={result.get('mode')} success={result['success']} "
                f"fail={result['fail']} rows={result['rows']}",
            )
            # 백필 후 데이터 길이 재진단
            lengths2 = await loop.run_in_executor(None, db.ticker_data_lengths)
            log.info("[scheduled] 백필 후 ticker_data_lengths: %s", lengths2)

        # 증분 — 미국은 date-batch 소스가 없어 update_today가 항상 빈 결과.
        # KRX식 5분×6회 retry는 무의미 → 바로 최근 영업일(ET 기준) 데이터 보장.
        ensured = await loop.run_in_executor(None, incremental.ensure_recent_business_day_data)
        log.info("[scheduled] ensure_recent_business_day 결과: %s", ensured)

        # 진단: 대표 미국 종목 last 7일치 close 출력 (cent 단위 → /100 = $)
        try:
            for diag_t in ("AAPL", "MSFT", "NVDA", "BRKB", "BFB"):
                rows_diag = await loop.run_in_executor(None, lambda t=diag_t: db.load_ohlcv(t, days=7))
                if rows_diag:
                    snap = [(r["date"], round(r["close"] / 100, 2)) for r in rows_diag]
                    log.warning("[scheduled] DIAG %s last7($)=%s", diag_t, snap)
                else:
                    log.warning("[scheduled] DIAG %s NO DATA in DB", diag_t)
        except Exception:
            log.exception("[scheduled] DIAG load_ohlcv 실패")

        # 기준일 = DB의 가장 최근 OHLCV 날짜 (명시적 결정 → signals에 전달)
        base_date = await loop.run_in_executor(None, db.latest_date) or datetime.now(KST).strftime("%Y-%m-%d")
        log.info("[scheduled] base_date for signals: %s", base_date)

        # 신호 계산 — 모든 종목이 동일 base_date 강제, 미보유 종목 자동 skip
        results, stats = await loop.run_in_executor(
            None, lambda: signals.compute_all(base_date=base_date)
        )
        log.info("[scheduled] signals stats: %s", stats)

        # 재귀 커버리지 audit (admin 전용) — 유니버스 동적 재생성·자가치유 가시화.
        # 일반 발송 메시지는 미변경. "매 실행이 유니버스를 학습해 갭을 좁힌다"의 추적.
        try:
            from src.admin_alerts import alert_admin
            ua = uni_audit or {}
            audit_msg = (
                f"유니버스: {ua.get('count', '?')}종목 "
                f"(floor=${ua.get('floor', 0)/1e9:.1f}B) | "
                f"신규진입 {ua.get('new_entries', '?')} · 비활성화 {ua.get('dropped', '?')} · "
                f"시총결측 {ua.get('no_cap', '?')}\n"
                f"데이터부족(backfill 대상) {missing} · "
                f"base_date 누락 skip {stats.get('skipped_no_base', '?')} · "
                f"시총미달 skip {stats.get('skipped_cap', '?')}\n"
                f"신호 발생: {sum(len(v) for v in results.values())}종목"
            )
            await alert_admin(
                bot, ("US_SCREENER_ALLOWED_CHAT_IDS", "US_SCREENER_CHAT_ID"),
                "📊 US 커버리지 audit", audit_msg,
            )
        except Exception:
            log.exception("[scheduled] 커버리지 audit 발신 실패")

        if not results:
            await send_text_chunked(bot, chat_id, "⚠️ 신호 계산 결과 비어있음 — DB 확인")
            return

        # 이중확인: 신호 종목들의 base_date close를 Naver에서 다시 fetch → DB와 대조
        # 불일치(또는 fetch 실패) 종목은 메시지에서 제외 (잘못된 신호 영구 차단)
        try:
            from src.us_screener import validator
            results, val_stats = await loop.run_in_executor(
                None, lambda: validator.cross_validate(results, base_date)
            )
            log.info("[scheduled] validator stats: %s", val_stats)
            stats["validated"] = val_stats.get("validated", 0)
            stats["rejected"] = val_stats.get("rejected", 0)
        except Exception:
            log.exception("[scheduled] validator 실패 — 검증 없이 발송")
            stats["validated"] = -1
            stats["rejected"] = -1

        # 히스토리 저장 (검증 통과만)
        try:
            await loop.run_in_executor(None, lambda: db.save_signals(base_date, results))
        except Exception:
            log.exception("signal 히스토리 저장 실패")

        # 발송 — formatter에 base_date + stats 명시적으로 전달
        text = formatter.format_results(
            results, datetime.now(KST), base_date=base_date, stats=stats
        )
        # 모든 대상자에게 broadcast (한 번 계산 → N번 발송)
        sent_count = 0
        for cid in target_chat_ids:
            try:
                await send_text_chunked(bot, cid, text)
                sent_count += 1
            except Exception:
                log.exception("[scheduled] 발송 실패 cid=%s", cid)
        log.info("[scheduled] 발송 완료 (base_date=%s, sent=%d/%d)",
                 base_date, sent_count, len(target_chat_ids))
    except Exception:
        log.exception("[scheduled] us_screener_daily_job 실패")
        try:
            await send_text_chunked(bot, chat_id, "⚠️ 스크리너 작업 실패 — 로그 확인")
        except Exception:
            pass


# ------------------------------------------------------------------
# Self-test (CLAUDE.md 자동 검증 의무)
# ------------------------------------------------------------------
async def _self_test(bot: Bot) -> None:
    """US_SCREENER_TEST_MODE=1 시 부팅 후 1회 자동 실행.

    chat_id 결정: US_SCREENER_TEST_CHAT_ID → 통합 robust 파싱
    (CHAT_ID_ENV + ALLOWED_ENV에서 진짜 chat_id만 추출. wildcard '*'·주석 등 무시).
    """
    test_chat = os.getenv("US_SCREENER_TEST_CHAT_ID", "").strip()
    chat_id = None
    if test_chat:
        import re
        m = re.search(r"-?\d{6,}", test_chat)
        if m:
            chat_id = m.group(0)
    if not chat_id:
        ids = _parse_chat_ids(CHAT_ID_ENV, ALLOWED_ENV)
        if ids:
            chat_id = ids[0]
    if not chat_id:
        log.warning("[self-test] chat_id 없음 — 스킵")
        return
    log.info("=" * 60)
    log.info("[self-test] screener 자동 검증 시작 chat_id=%s", chat_id)
    log.info("=" * 60)
    await asyncio.sleep(15)  # 폴링 안정화 대기
    try:
        await us_screener_daily_job(bot, override_chat_id=chat_id)
    except Exception:
        log.exception("[self-test] 파이프라인 최상위 예외")
    log.info("[self-test] 종료")


# ------------------------------------------------------------------
# Entry point (orchestrator가 호출)
# ------------------------------------------------------------------
US_SCREENER_COMMANDS = [
    ("start", "🚀 가입 (자동 발송 활성화)"),
    ("screen", "📈 즉시 스크리닝 실행"),
    ("stop", "탈퇴 (자동 발송 해제)"),
    ("help", "도움말"),
]


def build_us_screener_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    # 누구나 (가입/도움말/screen은 가입자만 내부 가드)
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("stop", _cmd_stop))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("screen", _cmd_screen))
    # admin 전용
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("diag", _cmd_diag))
    app.add_handler(CommandHandler("backfill", _cmd_backfill))
    app.add_handler(CommandHandler("list", _cmd_list))
    app.add_handler(CommandHandler("block", _cmd_block))
    app.add_handler(CommandHandler("unblock", _cmd_unblock))

    if os.getenv("US_SCREENER_TEST_MODE", "0") == "1":
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_self_test(app.bot))
        except RuntimeError:
            pass
    return app
