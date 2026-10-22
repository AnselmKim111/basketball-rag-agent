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
  - Railway API 불가면 로그인 흐름 자체를 시작하지 않음 (사용자 폰에 코드 안 보냄)
    → PC에서 `scripts/make_tg_session.py`(대화형). "불가" = 토큰·서비스 id 없음 **또는** 토큰
    인증 probe 'invalid'(`railway_env.probe()` — 폐기된 토큰이 Variables에 남은 경우).
    probe 'unknown'(네트워크 예외·5xx·429 — 일시 장애)도 시작하지 않지만, 무효로 단정해 env
    수동 삭제를 시키지 않고 "재배포로 재시도"만 안내 (env·상태파일 그대로 → 다음 부팅에 이어감).
  - Railway env 삭제가 실패하면(토큰 폐기 등) 종료 메시지에 남은 env 이름을 붙여 수동 삭제
    안내 (값은 절대 미포함) — 비밀번호가 유출된 저장소에 조용히 남지 않게.

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

RAILWAY_UNAVAILABLE_MSG = ("⚠ Railway 토큰 없음/무효 — 세션 생성은 PC에서 "
                           "scripts/make_tg_session.py(대화형)로 하세요")
# probe 'unknown' (일시 장애) — 수동 삭제 안내 없음 (재배포하면 같은 env로 이어서 진행)
RAILWAY_TRANSIENT_MSG = "⚠ Railway API 일시 장애 — 로그인 흐름 보류, 재배포로 재시도"


def _manual_note(names: list[str] | tuple[str, ...]) -> str:
    """삭제 못 한 env 수동 삭제 안내 — 이름만 (값 절대 미포함). 없으면 빈 문자열."""
    if not names:
        return ""
    return f" ⚠ Railway Variables에서 직접 삭제 필요: {', '.join(names)}"


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


def _railway_state() -> str:
    """Railway API 쓰기 가능 여부 — 'ok' | 'invalid' | 'unknown' (railway_env.probe와 같은 값).

    invalid: 자기 서비스 id·토큰 설정 없음(네트워크 호출 없음) 또는 토큰 폐기·다른 프로젝트.
    unknown: probe 일시 장애(네트워크 예외·5xx·429·JSON 아님) 또는 확인 중 예외.
    available()은 설정 존재만 보므로 폐기된 토큰도 통과 → probe()로 실제 인증 확인
    (확정 판정만 프로세스 캐시 — unknown은 다음 호출에서 재확인).
    """
    if _own_service_ids() is None:
        return "invalid"
    try:
        from src.model_router import railway_env
        if not railway_env.available():
            return "invalid"
        state = railway_env.probe()
    except Exception:
        log.exception("[tg_login] railway_env 확인 실패")
        return "unknown"
    return state if state in ("ok", "invalid", "unknown") else "unknown"


def _railway_ready() -> bool:
    """Railway API 쓰기 가능 + 자기 서비스 id 확인 (probe 'ok'). False면 env 변경을 시도하지 않는다."""
    return _railway_state() == "ok"


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
    """Railway(이 서비스)에서 env 삭제 → 성공한 것만 os.environ에서도 제거.

    삭제 *못 한* 이름 반환 (호출부가 _manual_note로 수동 삭제 안내 — 이름만).
    """
    present = [n for n in names if os.environ.get(n)]
    if not present:
        return []
    if not _railway_ready():
        log.warning("[tg_login] Railway API 불가 — env 수동 삭제 필요: %s", present)
        return present
    from src.model_router import railway_env
    failed: list[str] = []
    for n in present:
        try:
            ok, msg = _railway_call(railway_env.delete_variable, n, allow_fan_out=True)
        except Exception as e:
            ok, msg = False, type(e).__name__
        if ok:
            log.info("[tg_login] env %s 삭제: %s", n, msg)
            os.environ.pop(n, None)
        else:
            log.warning("[tg_login] env %s 삭제 실패 — Railway에서 수동 삭제 필요: %s", n, msg)
            failed.append(n)
    return failed


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


def _finish(path: Path, names: tuple[str, ...]) -> list[str]:
    """종료 경로 공통 정리 — 비밀 env 삭제 + 상태파일(auth key) 삭제. 삭제 못 한 env 이름 반환."""
    failed = _cleanup_env_vars(names)
    _unlink_state(path)
    return failed


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
        railway = _railway_state() if leftovers else "ok"
        if railway == "unknown":
            # 세션은 이미 있으므로 잔여 env 수동 삭제는 부작용 없음 — 단 "무효"로 단정하지 않음
            log.warning("[tg_login] Railway API 일시 장애 — 잔여 env 정리 보류: %s", leftovers)
            await say(f"⚠ Railway API 일시 장애 — 남은 env({', '.join(leftovers)}) 정리 보류, "
                      "재배포로 재시도 (급하면 Railway Variables에서 직접 삭제)")
            return action
        if railway != "ok":
            log.warning("[tg_login] Railway API 불가 — 잔여 env 수동 삭제 필요: %s", leftovers)
            await say(f"⚠ Railway 토큰 없음/무효 — 남은 env({', '.join(leftovers)})는 "
                      "Railway Variables에서 직접 삭제하세요")
            return action
        failed = _cleanup_env_vars(LOGIN_ENVS)
        if failed:
            await say("🧹 텔레그램 세션은 있으나 잔여 로그인 env의 Railway 삭제 실패."
                      + _manual_note(failed))
        return action

    # 이하(send_code/wait/sign_in)는 결과 세션을 Railway에 넣어야 끝나는 흐름 — API 불가면
    # 시작하지 않는다 (사용자 폰에 쓸모없는 코드 요청 금지).
    railway = _railway_state()
    if railway == "unknown":
        # 일시 장애 — 토큰이 무효라고 단정하지 않는다: env·상태파일은 그대로 두고(재배포 시 같은
        # 단계부터 재개) 수동 삭제 안내도 하지 않음.
        log.warning("[tg_login] Railway API 일시 장애(토큰 확인 불가) — 로그인 흐름 보류 "
                    "(action=%s)", action)
        await say(RAILWAY_TRANSIENT_MSG)
        return "railway_unknown"
    if railway != "ok":
        log.warning("[tg_login] Railway API 불가(토큰 없음/무효·서비스 id 없음) — 로그인 흐름 생략 "
                    "(action=%s)", action)
        # 컨테이너 흐름은 못 끝나므로 남은 TG_LOGIN_*는 사용자가 지워야 한다 (안 지우면 재배포마다
        # 이 알림 반복, 토큰 복구 시 의도치 않은 코드 발송). 이름만.
        left = [n for n in LOGIN_ENVS if os.environ.get(n)]
        await say(RAILWAY_UNAVAILABLE_MSG + _manual_note(left))
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
            left = _finish(path, SECRET_LOGIN_ENVS)
            await say(f"❌ 텔레그램 로그인 코드 요청 실패: {type(e).__name__}: {str(e)[:200]}"
                      + _manual_note(left))
            return "send_code_failed"
        # 이전 시도의 코드가 env에 남아 있으면 다음 부팅에 새 hash로 오입력됨 → 삭제
        left = _cleanup_env_vars(("TG_LOGIN_CODE",))
        await say("📨 텔레그램 로그인 코드를 보냈습니다. 앱에 온 코드를 Claude에게 전달하세요 "
                  "(Claude가 TG_LOGIN_CODE로 반영 → 자동 로그인)." + _manual_note(left))
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
        note = _manual_note(_finish(path, SECRET_LOGIN_ENVS))
        if "Expired" in name:
            # 코드 만료 → 즉시 새 코드 요청 (새 상태파일 = 다음 코드 대기)
            try:
                await _send_code(phone, api_id, api_hash, path)
            except Exception:
                log.exception("[tg_login] 재요청 실패")
                _unlink_state(path)
                await say("❌ 코드 만료 후 재요청 실패 — TG_LOGIN_PHONE 재설정 필요" + note)
                return "code_expired"
            await say("⌛ 코드 만료 → 새 코드를 다시 보냈습니다. 새 코드를 전달하세요"
                      " (2단계 비밀번호 계정이면 TG_LOGIN_PASSWORD도 함께)." + note)
            return "code_expired_resent"
        if "PasswordNeeded" in name:
            await say("🔐 2단계 비밀번호가 필요합니다 — TG_LOGIN_PASSWORD 설정 필요 (Claude에게 전달). "
                      "이번 코드는 폐기됨 → 재배포 시 새 코드 요청." + note)
            return "password_needed"
        if "Password" in name:
            # PasswordHashInvalidError 등 — 비밀번호를 줬는데 틀림
            await say("🔐 2단계 비밀번호가 틀렸습니다 — 올바른 TG_LOGIN_PASSWORD로 다시 설정하세요. "
                      "이번 코드는 폐기됨 → 재배포 시 새 코드 요청." + note)
            return "password_wrong"
        await say(f"❌ 코드 로그인 실패({name}) — 이번 코드는 폐기됨 → 재배포 시 새 코드 요청 "
                  "(TG_LOGIN_PHONE 유지)." + note)
        return "sign_in_failed"

    # 삭제(재배포 없음) → upsert(재배포 유발) 순서: 새 컨테이너에는 세션만 남는다.
    # 상태파일의 session은 방금 인가된 auth key와 같으므로 upsert 전에 삭제.
    note = _manual_note(_finish(path, LOGIN_ENVS))
    ok, msg = _upsert_session(session_str)
    if ok:
        log.info("[tg_login] TG_SESSION_STRING upsert: %s", msg)
        await say("✅ 텔레그램 세션 생성 완료 → Railway 반영됨. 재배포 후 DSInvResearch 릴레이 활성."
                  + note)
        return "signed_in"
    log.error("[tg_login] TG_SESSION_STRING upsert 실패: %s", msg)
    logged_out = await _log_out(session_str, api_id, api_hash)
    await say(f"⚠️ 로그인은 됐지만 Railway 반영 실패: {msg} — "
              + ("세션은 즉시 로그아웃(폐기)했습니다. " if logged_out else
                 "세션 로그아웃 실패 → 텔레그램 설정 → 기기에서 'IdeaBot relay (Railway)' 종료 필요. ")
              + "TG_LOGIN_PHONE 재설정 후 재시도." + note)
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
