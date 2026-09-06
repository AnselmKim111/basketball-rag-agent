"""Telethon 사용자 세션 생성을 Railway 컨테이너에서 env 주도로 수행.

배경: channel_relay MTProto 백엔드는 `TG_SESSION_STRING`(사용자 계정 세션)이 필요한데,
Claude Code 컨테이너는 TLS 재종단 프록시라 MTProto 접속 불가, 사용자 PC도 없음.
→ 네트워크가 자유로운 Railway 컨테이너가 부팅 시 env를 보고 2단계 로그인을 대신 수행.

상태 머신 (부팅마다 1회, 백그라운드 task):
  - `TG_SESSION_STRING` 있음            → 아무것도 안 함 (잔여 TG_LOGIN_* env·상태파일 정리)
  - `TG_LOGIN_PHONE` 있음 + 상태파일 없음 → send_code → 상태파일(볼륨) 저장 → 텔레그램 알림
  - 상태파일 있음 + `TG_LOGIN_CODE` 있음 → sign_in → `TG_SESSION_STRING` upsert(재배포 유발)
                                          → `TG_LOGIN_PHONE/CODE/PASSWORD` 삭제 → 상태파일 삭제
  - 상태파일 있음 + 코드 없음            → 대기 (로그만)

운영 절차 (Claude가 Railway API로 수행, 사용자는 코드 전달만):
  1) `TG_LOGIN_PHONE=+8210…` upsert → 재배포 → 컨테이너가 코드 요청
  2) 사용자가 앱에서 받은 코드 → `TG_LOGIN_CODE=12345` upsert → 재배포 → 컨테이너가 로그인
     (2단계 비밀번호 계정이면 `TG_LOGIN_PASSWORD`도 함께)
  3) 컨테이너가 `TG_SESSION_STRING` upsert → 재배포 → 릴레이 활성
세션 문자열·상태파일은 로그·채팅에 절대 출력하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

STATE_FILENAME = "tg_login_state.json"
LOGIN_ENVS = ("TG_LOGIN_PHONE", "TG_LOGIN_CODE", "TG_LOGIN_PASSWORD")
Notify = Callable[[str], Awaitable[None]]


def _state_path() -> Path:
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key, "").strip()
        if v:
            return Path(v) / STATE_FILENAME
    return Path(STATE_FILENAME)


def decide(env: dict[str, str], state_exists: bool) -> str:
    """부팅 시 할 일 결정 — 순수 함수 (테스트 대상).

    반환: "noop" | "cleanup" | "send_code" | "sign_in" | "wait" | "missing_creds"
    """
    if env.get("TG_SESSION_STRING", "").strip():
        leftovers = any(env.get(k, "").strip() for k in LOGIN_ENVS)
        return "cleanup" if (leftovers or state_exists) else "noop"
    phone = env.get("TG_LOGIN_PHONE", "").strip()
    code = env.get("TG_LOGIN_CODE", "").strip()
    if not phone and not state_exists:
        return "noop"
    api_id = env.get("TG_API_ID", "").strip()
    api_hash = env.get("TG_API_HASH", "").strip()
    if not (api_id.isdigit() and api_hash):
        return "missing_creds"
    if state_exists and code:
        return "sign_in"
    if state_exists:
        return "wait"
    return "send_code"


def _notify_chat_id() -> str | None:
    v = os.getenv("REPORT_CHAT_ID") or os.getenv("MODEL_ROUTER_CHAT_ID")
    if v:
        return v.strip()
    for tok in os.getenv("ALLOWED_CHAT_IDS", "").split(","):
        tok = tok.strip()
        if tok and tok != "*" and tok.lstrip("-").isdigit():
            return tok
    return None


async def _send_code(phone: str, api_id: int, api_hash: str, path: Path) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        state = {"phone": phone, "phone_code_hash": sent.phone_code_hash,
                 "session": client.session.save()}
    finally:
        await client.disconnect()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    try:
        path.chmod(0o600)
    except OSError:
        pass


async def _sign_in(code: str, password: str, api_id: int, api_hash: str,
                   path: Path) -> str:
    """성공 시 세션 문자열 반환. 실패는 telethon 예외 그대로 전파."""
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    state = json.loads(path.read_text())
    client = TelegramClient(StringSession(state["session"]), api_id, api_hash)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=state["phone"], code=code,
                                 phone_code_hash=state["phone_code_hash"])
        except SessionPasswordNeededError:
            if not password:
                raise
            await client.sign_in(password=password)
        me = await client.get_me()
        log.info("[tg_login] 로그인 성공: %s (@%s)", getattr(me, "first_name", "?"),
                 getattr(me, "username", None) or "-")
        return client.session.save()
    finally:
        await client.disconnect()


def _cleanup_env_vars(names: tuple[str, ...]) -> None:
    from src.model_router import railway_env
    for n in names:
        if os.environ.get(n):
            ok, msg = railway_env.delete_variable(n)
            log.info("[tg_login] env %s 삭제: %s", n, msg)


async def run_boot_login(notify: Notify | None = None) -> str:
    """부팅 시 1회 호출. 수행한 action 문자열 반환 (로그·테스트용)."""
    path = _state_path()
    action = decide(dict(os.environ), path.exists())
    log.info("[tg_login] action=%s (state=%s)", action, path)

    async def say(msg: str) -> None:
        if notify is None:
            return
        try:
            await notify(msg)
        except Exception:
            log.exception("[tg_login] 알림 실패")

    if action == "noop":
        return action
    if action == "missing_creds":
        log.warning("[tg_login] TG_API_ID/TG_API_HASH 없음 — 로그인 불가")
        return action
    if action == "cleanup":
        _cleanup_env_vars(LOGIN_ENVS)
        path.unlink(missing_ok=True)
        return action
    if action == "wait":
        log.info("[tg_login] 코드 대기 중 — TG_LOGIN_CODE upsert 필요")
        return action

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"].strip()
    phone = os.environ.get("TG_LOGIN_PHONE", "").strip()

    if action == "send_code":
        try:
            await _send_code(phone, api_id, api_hash, path)
        except Exception as e:
            log.exception("[tg_login] send_code 실패")
            await say(f"❌ 텔레그램 로그인 코드 요청 실패: {type(e).__name__}: {str(e)[:200]}")
            return "send_code_failed"
        await say("📨 텔레그램 로그인 코드를 보냈습니다. 앱에 온 코드를 Claude에게 전달하세요 "
                  "(Claude가 TG_LOGIN_CODE로 반영 → 자동 로그인).")
        return action

    # sign_in
    code = os.environ["TG_LOGIN_CODE"].strip()
    password = os.environ.get("TG_LOGIN_PASSWORD", "").strip()
    try:
        session_str = await _sign_in(code, password, api_id, api_hash, path)
    except Exception as e:
        name = type(e).__name__
        log.warning("[tg_login] sign_in 실패: %s: %s", name, str(e)[:200])
        if "Expired" in name:
            # 코드 만료 → 상태 폐기 + 코드 env 삭제 → 즉시 새 코드 요청
            path.unlink(missing_ok=True)
            _cleanup_env_vars(("TG_LOGIN_CODE",))
            try:
                await _send_code(phone, api_id, api_hash, path)
                await say("⌛ 코드 만료 → 새 코드를 다시 보냈습니다. 새 코드를 전달하세요.")
                return "code_expired_resent"
            except Exception:
                log.exception("[tg_login] 재요청 실패")
                await say("❌ 코드 만료 후 재요청 실패 — TG_LOGIN_PHONE 재설정 필요")
                return "code_expired"
        if "Password" in name:
            await say("🔐 2단계 비밀번호가 필요합니다 — TG_LOGIN_PASSWORD 설정 필요 (Claude에게 전달).")
            return "password_needed"
        _cleanup_env_vars(("TG_LOGIN_CODE",))
        await say(f"❌ 코드 로그인 실패({name}) — 코드를 다시 확인해 전달하세요.")
        return "sign_in_failed"

    from src.model_router import railway_env
    # 삭제(재배포 없음) → upsert(재배포 유발) 순서: 새 컨테이너에는 세션만 남는다.
    _cleanup_env_vars(LOGIN_ENVS)
    ok, msg = railway_env.upsert_variable("TG_SESSION_STRING", session_str)
    path.unlink(missing_ok=True)
    if ok:
        log.info("[tg_login] TG_SESSION_STRING upsert: %s", msg)
        await say("✅ 텔레그램 세션 생성 완료 → Railway 반영됨. 재배포 후 DSInvResearch 릴레이 활성.")
        return "signed_in"
    log.error("[tg_login] TG_SESSION_STRING upsert 실패: %s", msg)
    await say(f"⚠️ 로그인은 됐지만 Railway 반영 실패: {msg}")
    return "upsert_failed"


def schedule_boot_login(bot=None) -> asyncio.Task | None:
    """orchestrator에서 호출 — 백그라운드 task로 실행 (봇 기동 차단 없음)."""
    if decide(dict(os.environ), _state_path().exists()) == "noop":
        return None
    chat_id = _notify_chat_id()

    async def notify(msg: str) -> None:
        if bot is None or not chat_id:
            return
        await bot.send_message(chat_id=chat_id, text=msg)

    return asyncio.create_task(run_boot_login(notify))
