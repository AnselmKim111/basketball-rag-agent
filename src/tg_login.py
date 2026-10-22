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

보안 규칙 (2026-10-21 Railway env 유출 사고 후속):
  - 종료 경로(성공·코드 만료·비밀번호 필요/오류·로그인 실패·예외)마다 `TG_LOGIN_CODE`·
    `TG_LOGIN_PASSWORD` 삭제(전화번호는 성공 시에만) + 상태파일 삭제. 상태파일의 session은
    로그인 성공 시 그대로 인가되는 auth key라 남기면 안 된다. 예외: 코드를 보내고 기다리는 중.
  - Railway 변경은 이 서비스(`RAILWAY_SERVICE_ID`)에만 — 개인 계정 세션을 다른 서비스로 퍼뜨리지 않음.
  - Railway API 불가(토큰 폐기 등)면 로그인 흐름 자체를 시작하지 않음 (사용자 폰에 코드 안 보냄)
    → PC에서 `scripts/make_tg_session.py`(대화형).

운영 절차 (Claude가 Railway API로 수행, 사용자는 코드 전달만):
  1) `TG_LOGIN_PHONE=+8210…` upsert → 재배포 → 컨테이너가 코드 요청
     (2단계 비밀번호 계정이면 `TG_LOGIN_PASSWORD`를 이때 또는 코드와 함께)
  2) 사용자가 앱에서 받은 코드 → `TG_LOGIN_CODE=12345` upsert → 재배포 → 컨테이너가 로그인
  3) 컨테이너가 `TG_SESSION_STRING` upsert → 재배포 → 릴레이 활성
세션 문자열·상태파일은 로그·채팅에 절대 출력하지 않는다.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

from src.log_redact import redact

log = logging.getLogger(__name__)

STATE_FILENAME = "tg_login_state.json"
LOGIN_ENVS = ("TG_LOGIN_PHONE", "TG_LOGIN_CODE", "TG_LOGIN_PASSWORD")
# 종료 경로마다 지우는 비밀 env (전화번호는 성공 시에만 — 실패 후 재시도에 필요)
SECRET_LOGIN_ENVS = ("TG_LOGIN_CODE", "TG_LOGIN_PASSWORD")
Notify = Callable[[str], Awaitable[None]]

# Telethon 클라이언트 식별 정보 — 사용자가 텔레그램 설정 → 기기(Devices)에서 이 세션을 알아보고
# 종료할 수 있게 (channel_relay도 같은 값 사용).
CLIENT_DEVICE_KWARGS = {
    "device_model": "IdeaBot relay (Railway)",
    "system_version": "Railway container",
    "app_version": "IdeaBot channel_relay",
}

RAILWAY_UNAVAILABLE_MSG = ("⚠ Railway 토큰 없음 — 세션 생성은 PC에서 "
                           "scripts/make_tg_session.py(대화형)로 하세요")


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


# ------------------------------------------------------------------
# Railway env — 이 서비스(RAILWAY_SERVICE_ID)로 한정
# ------------------------------------------------------------------
def _own_service_ids() -> list[str] | None:
    """Railway 자동 주입 `RAILWAY_SERVICE_ID` 단일 — 없으면 None (대상 서비스 불명)."""
    sid = os.environ.get("RAILWAY_SERVICE_ID", "").strip()
    return [sid] if sid else None


def _railway_ready() -> bool:
    """Railway API 쓰기 가능 + 자기 서비스 id 확인. False면 로그인 흐름을 시작하지 않는다."""
    if _own_service_ids() is None:
        return False
    try:
        from src.model_router import railway_env
        return bool(railway_env.available())
    except Exception:
        log.exception("[tg_login] railway_env 확인 실패")
        return False


def _supports_service_ids(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "service_ids" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _railway_call(fn, *args, allow_fan_out: bool) -> tuple[bool, str]:
    """railway_env 함수를 자기 서비스로 한정해 호출.

    service_ids 미지원(구버전 railway_env)이면 인자 없이 호출 — 단 allow_fan_out=False(세션
    upsert)는 RAILWAY_SERVICE_IDS가 자기 서비스뿐일 때만 (다른 서비스로 퍼지는 것 차단).
    """
    sids = _own_service_ids()
    if _supports_service_ids(fn):
        return fn(*args, service_ids=sids)
    if not allow_fan_out:
        fan = [s.strip() for s in os.environ.get("RAILWAY_SERVICE_IDS", "").split(",")
               if s.strip()]
        if fan and fan != (sids or []):
            return False, "railway_env가 service_ids 미지원 — 다른 서비스로 퍼질 수 있어 거부"
    return fn(*args)


def _cleanup_env_vars(names: tuple[str, ...]) -> list[str]:
    """Railway(이 서비스)에서 env 삭제 → 성공한 것만 os.environ에서도 제거. 삭제된 이름 반환."""
    present = [n for n in names if os.environ.get(n)]
    if not present:
        return []
    if not _railway_ready():
        log.warning("[tg_login] Railway API 불가 — env 수동 삭제 필요: %s", present)
        return []
    from src.model_router import railway_env
    deleted: list[str] = []
    for n in present:
        try:
            ok, msg = _railway_call(railway_env.delete_variable, n, allow_fan_out=True)
        except Exception as e:
            ok, msg = False, type(e).__name__
        log.info("[tg_login] env %s 삭제: %s", n, msg)
        if ok:
            os.environ.pop(n, None)
            deleted.append(n)
    return deleted


def _upsert_session(session_str: str) -> tuple[bool, str]:
    from src.model_router import railway_env
    try:
        return _railway_call(railway_env.upsert_variable, "TG_SESSION_STRING", session_str,
                             allow_fan_out=False)
    except Exception as e:
        return False, type(e).__name__


def _unlink_state(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("[tg_login] 상태파일 삭제 실패: %s", path)


def _finish(path: Path, names: tuple[str, ...]) -> None:
    """종료 경로 공통 정리 — 비밀 env 삭제 + 상태파일(auth key) 삭제."""
    _cleanup_env_vars(names)
    _unlink_state(path)


# ------------------------------------------------------------------
# Telethon
# ------------------------------------------------------------------
async def _send_code(phone: str, api_id: int, api_hash: str, path: Path) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash, **CLIENT_DEVICE_KWARGS)
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
    client = TelegramClient(StringSession(state["session"]), api_id, api_hash,
                            **CLIENT_DEVICE_KWARGS)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=state["phone"], code=code,
                                 phone_code_hash=state["phone_code_hash"])
        except SessionPasswordNeededError:
            if not password:
                raise
            await client.sign_in(password=password)
        # 계정 이름·@username은 로그에 남기지 않음 (개인 계정 식별 정보)
        log.info("[tg_login] 로그인 성공")
        return client.session.save()
    finally:
        await client.disconnect()


async def _log_out(session_str: str, api_id: int, api_hash: str) -> bool:
    """Railway 반영 실패 시 방금 만든 세션을 폐기 — 어디에도 저장 못 한 인가 세션을 남기지 않음."""
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(session_str), api_id, api_hash,
                                **CLIENT_DEVICE_KWARGS)
        await client.connect()
        try:
            return bool(await client.log_out())
        finally:
            await client.disconnect()
    except Exception as e:
        log.warning("[tg_login] 세션 로그아웃 실패: %s", type(e).__name__)
        return False


async def run_boot_login(notify: Notify | None = None) -> str:
    """부팅 시 1회 호출. 수행한 action 문자열 반환 (로그·테스트용)."""
    path = _state_path()
    action = decide(dict(os.environ), path.exists())
    log.info("[tg_login] action=%s (state=%s)", action, path)

    async def say(msg: str) -> None:
        if notify is None:
            return
        try:
            await notify(redact(msg))
        except Exception:
            log.exception("[tg_login] 알림 실패")

    if action == "noop":
        return action
    if action == "missing_creds":
        log.warning("[tg_login] TG_API_ID/TG_API_HASH 없음 — 로그인 불가")
        return action
    if action == "cleanup":
        leftovers = [n for n in LOGIN_ENVS if os.environ.get(n)]
        _unlink_state(path)
        if leftovers and not _railway_ready():
            log.warning("[tg_login] Railway API 불가 — 잔여 env 수동 삭제 필요: %s", leftovers)
            await say(f"⚠ Railway 토큰 없음 — 남은 env({', '.join(leftovers)})는 "
                      "Railway Variables에서 직접 삭제하세요")
            return action
        _cleanup_env_vars(LOGIN_ENVS)
        return action

    # 이하(send_code/wait/sign_in)는 결과 세션을 Railway에 넣어야 끝나는 흐름 — API 불가면
    # 시작하지 않는다 (사용자 폰에 쓸모없는 코드 요청 금지).
    if not _railway_ready():
        log.warning("[tg_login] Railway API 불가(토큰/서비스 id 없음) — 로그인 흐름 생략 "
                    "(action=%s)", action)
        await say(RAILWAY_UNAVAILABLE_MSG)
        return "railway_unavailable"
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
            _finish(path, SECRET_LOGIN_ENVS)
            await say(f"❌ 텔레그램 로그인 코드 요청 실패: {type(e).__name__}: {str(e)[:200]}")
            return "send_code_failed"
        # 이전 시도의 코드가 env에 남아 있으면 다음 부팅에 새 hash로 오입력됨 → 삭제
        _cleanup_env_vars(("TG_LOGIN_CODE",))
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
        # 어떤 실패든 이번 코드·비밀번호·상태파일(auth key)은 폐기
        _finish(path, SECRET_LOGIN_ENVS)
        if "Expired" in name:
            # 코드 만료 → 즉시 새 코드 요청 (새 상태파일 = 다음 코드 대기)
            try:
                await _send_code(phone, api_id, api_hash, path)
            except Exception:
                log.exception("[tg_login] 재요청 실패")
                _unlink_state(path)
                await say("❌ 코드 만료 후 재요청 실패 — TG_LOGIN_PHONE 재설정 필요")
                return "code_expired"
            await say("⌛ 코드 만료 → 새 코드를 다시 보냈습니다. 새 코드를 전달하세요"
                      " (2단계 비밀번호 계정이면 TG_LOGIN_PASSWORD도 함께).")
            return "code_expired_resent"
        if "PasswordNeeded" in name:
            await say("🔐 2단계 비밀번호가 필요합니다 — TG_LOGIN_PASSWORD 설정 필요 (Claude에게 전달). "
                      "이번 코드는 폐기됨 → 재배포 시 새 코드 요청.")
            return "password_needed"
        if "Password" in name:
            # PasswordHashInvalidError 등 — 비밀번호를 줬는데 틀림
            await say("🔐 2단계 비밀번호가 틀렸습니다 — 올바른 TG_LOGIN_PASSWORD로 다시 설정하세요. "
                      "이번 코드는 폐기됨 → 재배포 시 새 코드 요청.")
            return "password_wrong"
        await say(f"❌ 코드 로그인 실패({name}) — 이번 코드는 폐기됨 → 재배포 시 새 코드 요청 "
                  "(TG_LOGIN_PHONE 유지).")
        return "sign_in_failed"

    # 삭제(재배포 없음) → upsert(재배포 유발) 순서: 새 컨테이너에는 세션만 남는다.
    # 상태파일의 session은 방금 인가된 auth key와 같으므로 upsert 전에 삭제.
    _finish(path, LOGIN_ENVS)
    ok, msg = _upsert_session(session_str)
    if ok:
        log.info("[tg_login] TG_SESSION_STRING upsert: %s", msg)
        await say("✅ 텔레그램 세션 생성 완료 → Railway 반영됨. 재배포 후 DSInvResearch 릴레이 활성.")
        return "signed_in"
    log.error("[tg_login] TG_SESSION_STRING upsert 실패: %s", msg)
    logged_out = await _log_out(session_str, api_id, api_hash)
    await say(f"⚠️ 로그인은 됐지만 Railway 반영 실패: {msg} — "
              + ("세션은 즉시 로그아웃(폐기)했습니다. " if logged_out else
                 "세션 로그아웃 실패 → 텔레그램 설정 → 기기에서 'IdeaBot relay (Railway)' 종료 필요. ")
              + "TG_LOGIN_PHONE 재설정 후 재시도.")
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
