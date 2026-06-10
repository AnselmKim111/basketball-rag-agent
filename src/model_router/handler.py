"""텔레그램 핸들러 + 주간 cron job.

명령:
  /model_eval        — 즉시 재평가 (cron 안 기다림)
  /model_approve <id|all>  — 승인 후 Railway env upsert
  /model_reject <id|all>
  /model_status      — 현재 env + pending + 최근 이력
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import approval_store, railway_env
from .candidates import TIER_ENV
from .recommender import build_recommendations

log = logging.getLogger(__name__)


def _ttl_hours() -> int:
    try:
        return int(os.getenv("MODEL_ROUTER_TTL_HOURS", "12"))
    except ValueError:
        return 12


def _is_admin(chat_id: int, allowed_env: str = "REPORT_ALLOWED_CHAT_IDS") -> bool:
    allowed = os.getenv(allowed_env, "") or os.getenv("REPORT_CHAT_ID", "")
    if allowed == "*":
        return True
    ids = {x.strip() for x in allowed.split(",") if x.strip()}
    return str(chat_id) in ids


def _format_eval_message(recs: list[dict], pending: list[dict],
                        auto_applied: list[dict]) -> str:
    parts = ["🤖 *주간 모델 가성비 재평가*", ""]

    if auto_applied:
        parts.append("*[자동 적용됨]*")
        for r in auto_applied:
            parts.append(f"✓ `{r['env_name']}`: {r['old_model']} → *{r['new_model']}*")
            parts.append(f"   {r['reason']}")
        parts.append("")

    if pending:
        parts.append(f"*[승인 대기 — {_ttl_hours()}h 내 응답]*")
        for p in pending:
            sav = f" · 비용 {p['savings_pct']}% 절감" if p.get("savings_pct") else ""
            parts.append(f"\n{p['id']}. `{p['env_name']}`: {p['old_model'] or '(미설정)'} → *{p['new_model']}*")
            parts.append(f"   {p['reason']}{sav}")
            parts.append(f"   점수 {p['score_old']:.2f} → {p['score_new']:.2f}")
        parts.append("")
        parts.append("승인: `/model_approve <id|all>`")
        parts.append("거부: `/model_reject <id|all>`")
    elif not auto_applied:
        parts.append("✨ 변경 권고 없음 — 현 모델이 최적")

    return "\n".join(parts)


async def _apply_change(env_name: str, new_model: str) -> tuple[bool, str]:
    """Railway env upsert wrapper. Layer A canary 게이트로 보호."""
    from . import canary
    result = canary.run_canary(env_name, new_model)
    if not result.get("skipped") and not result["passed"]:
        fail_summary = "; ".join(result["failures"][:3])
        log.warning("[apply_change] %s canary 실패 — 변경 거부: %s", env_name, fail_summary)
        return False, f"❌ Canary 실패 ({result['sentinels']}건, {len(result['failures'])} fail): {fail_summary}"
    return railway_env.upsert_variable(env_name, new_model)


async def model_health_job(bot: Bot, override_chat_id: str | None = None) -> None:
    """Layer D — 시간당 health 검사 + rollback. cron 등록 (시간당)."""
    from . import rollback
    log.info("[model_health] 시작")
    actions = rollback.check_and_rollback()
    if not actions:
        return  # 정상 — silent
    chat_id = override_chat_id or os.getenv("REPORT_CHAT_ID")
    if not chat_id:
        return
    msg = rollback.format_rollback_message(actions)
    try:
        await bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN)
        log.info("[model_health] rollback %d건 알림", len(actions))
    except Exception:
        log.exception("[model_health] 알림 실패")


async def model_eval_job(bot: Bot, override_chat_id: str | None = None) -> None:
    """cron entrypoint — 주간 재평가 + admin 알림."""
    log.info("[model_router.cron] 시작")
    chat_id = override_chat_id or os.getenv("REPORT_CHAT_ID")
    if not chat_id:
        log.warning("[model_router.cron] REPORT_CHAT_ID 미설정 — 알림 skip")
        return

    # 1. expire old pending (이전 주 미응답)
    expired = approval_store.expire_old()
    if expired:
        log.info("[model_router.cron] %d expired", len(expired))

    # 2. 추천 생성
    recs = build_recommendations()
    if not recs:
        await bot.send_message(chat_id, "🤖 *주간 모델 재평가*: 변경 권고 없음", parse_mode=ParseMode.MARKDOWN)
        return

    # 3. automatic 즉시 적용
    auto_applied = []
    for r in recs:
        if r["classification"] != "automatic":
            continue
        ok, msg = await _apply_change(r["env_name"], r["new_model"])
        r["apply_result"] = msg
        if ok:
            auto_applied.append(r)
            approval_store.append_history({**r, "status": "auto_applied",
                                           "resolved_at": int(datetime.now().timestamp())})

    # 4. suggest는 pending에 저장
    pending = approval_store.stage_recommendations(recs, ttl_hours=_ttl_hours())

    # 5. 텔레그램 알림
    msg = _format_eval_message(recs, pending, auto_applied)
    try:
        await bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN)
        log.info("[model_router.cron] 알림 발송 (auto=%d pending=%d)", len(auto_applied), len(pending))
    except Exception:
        log.exception("[model_router.cron] 알림 발송 실패")


async def model_eval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("권한 없음")
        return
    await update.message.reply_text("⏳ 재평가 중 (10-15초)...")
    await model_eval_job(context.bot, override_chat_id=str(chat_id))


async def model_approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("권한 없음")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용: `/model_approve <id|all>`", parse_mode=ParseMode.MARKDOWN)
        return

    target = args[0].lower()
    items = approval_store.load_pending()
    pendings = [i for i in items if i["status"] == "pending"]
    if not pendings:
        await update.message.reply_text("대기 중인 추천 없음")
        return

    if target == "all":
        targets = pendings
    else:
        try:
            tid = int(target)
        except ValueError:
            await update.message.reply_text("id가 숫자 또는 `all`이어야 함")
            return
        targets = [p for p in pendings if p["id"] == tid]
        if not targets:
            await update.message.reply_text(f"id={tid} 대기 항목 없음")
            return

    results = []
    for p in targets:
        ok, msg = await _apply_change(p["env_name"], p["new_model"])
        if ok:
            approval_store.mark_status(p["id"], "approved", note=msg)
            results.append(f"✅ {p['id']} `{p['env_name']}` → *{p['new_model']}* — {msg}")
        else:
            results.append(f"❌ {p['id']} `{p['env_name']}` 실패 — {msg}")
    await update.message.reply_text("\n".join(results), parse_mode=ParseMode.MARKDOWN)


async def model_reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("권한 없음")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용: `/model_reject <id|all>`", parse_mode=ParseMode.MARKDOWN)
        return
    target = args[0].lower()
    items = approval_store.load_pending()
    pendings = [i for i in items if i["status"] == "pending"]
    if not pendings:
        await update.message.reply_text("대기 중인 추천 없음")
        return

    if target == "all":
        targets = pendings
    else:
        try:
            tid = int(target)
            targets = [p for p in pendings if p["id"] == tid]
        except ValueError:
            await update.message.reply_text("id가 숫자 또는 `all`이어야 함")
            return
    for p in targets:
        approval_store.mark_status(p["id"], "rejected", note="사용자 거부")
    await update.message.reply_text(f"❎ {len(targets)}건 거부 완료")


async def model_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("권한 없음")
        return

    parts = ["🤖 *모델 상태*", "", "*현재 env*:"]
    seen = set()
    for tier, envs in TIER_ENV.items():
        for env_name in envs:
            if env_name in seen:
                continue
            seen.add(env_name)
            val = os.getenv(env_name, "(미설정)")
            parts.append(f"  `{env_name}` = {val}")

    pending = [i for i in approval_store.load_pending() if i["status"] == "pending"]
    parts.append("")
    parts.append(f"*대기 중 추천*: {len(pending)}건")
    for p in pending[:5]:
        parts.append(f"  {p['id']}. `{p['env_name']}` → {p['new_model']}")

    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN)


MODEL_ROUTER_COMMANDS = [
    ("model_eval", "모델 가성비 즉시 재평가"),
    ("model_approve", "추천 승인 (Railway env 적용)"),
    ("model_reject", "추천 거부"),
    ("model_status", "현재 모델 + 대기 추천 보기"),
]
