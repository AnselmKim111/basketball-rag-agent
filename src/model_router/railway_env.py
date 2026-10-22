"""Railway GraphQL variableUpsert — 승인된 모델 변경을 환경변수에 자동 반영.

필요 env: `RAILWAY_PROJECT_ACCESS_TOKEN` (alias `RAILWAY_ACCESS_TOKEN`), `RAILWAY_PROJECT_ID`,
`RAILWAY_ENVIRONMENT_ID` (둘 다 Railway 자동 주입), `RAILWAY_SERVICE_IDS` (콤마 구분 — 여러
서비스에 동시 upsert; 미설정 시 자동 주입 `RAILWAY_SERVICE_ID` 단일).

반환 메시지·로그에 변수 값(value)은 절대 넣지 않음 — TG_SESSION_STRING 같은 비밀도 이
경로로 쓰이고, 메시지는 텔레그램으로 나간다 (2026-10-21 Railway env 유출 사고 후속).

`available()`은 설정 존재 여부만 본다 (호출 없음 — Layer D·handler가 사용, 일시 장애로
"없음" 처리하면 안 됨). 토큰이 실제로 살아 있는지(폐기 여부)는 `probe()` — 인증 GraphQL 1회,
프로세스+토큰 단위 캐시, 'ok' | 'invalid'(설정 누락·폐기·다른 프로젝트) | 'unknown'(일시 장애).
`token_valid()` == (probe() == 'ok'). 폐기된 토큰이 Variables에 남은 채로 쓰이면 안 되는
경로(tg_login: 사용자 폰에 코드 발송)가 사용 — tg_login은 'unknown'이면 "무효"가 아니라
"일시 장애·재배포로 재시도"로 안내 (사용자에게 env 수동 삭제를 시키지 않음).
"""
from __future__ import annotations

import hashlib
import logging
import os

import httpx

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"

UPSERT_MUTATION = """mutation upsert($input: VariableUpsertInput!){variableUpsert(input:$input)}"""


def _config(quiet: bool = False) -> dict | None:
    # 사용자가 Variables에 `RAILWAY_ACCESS_TOKEN` 이름으로 넣는 실수가 실측됨(2026-09-06)
    # — Project Token이면 이름과 무관하게 동작하므로 alias로 수용.
    tok = os.getenv("RAILWAY_PROJECT_ACCESS_TOKEN") or os.getenv("RAILWAY_ACCESS_TOKEN")
    # PROJECT/ENVIRONMENT/SERVICE ID는 Railway가 컨테이너에 자동 주입 —
    # 사용자가 손수 넣어야 하는 건 사실상 토큰 하나뿐.
    pid = os.getenv("RAILWAY_PROJECT_ID")
    eid = os.getenv("RAILWAY_ENVIRONMENT_ID")
    sids = os.getenv("RAILWAY_SERVICE_IDS", "") or os.getenv("RAILWAY_SERVICE_ID", "")
    missing = [n for n, v in (("RAILWAY_PROJECT_ACCESS_TOKEN", tok),
                              ("RAILWAY_PROJECT_ID", pid),
                              ("RAILWAY_ENVIRONMENT_ID", eid),
                              ("RAILWAY_SERVICE_ID(S)", sids)) if not v]
    if missing:
        if not quiet:
            log.warning("[model_router.railway_env] Railway env 누락: %s — upsert 불가", missing)
        return None
    return {
        "token": tok, "project_id": pid, "env_id": eid,
        "service_ids": [s.strip() for s in sids.split(",") if s.strip()],
    }


def available() -> bool:
    """Railway API 쓰기 가능 설정(토큰·ID) 여부. False면 호출부는 수동 적용 안내.

    설정 존재만 확인 (네트워크 호출 없음) — 폐기된 토큰도 True. 실제 유효성은 token_valid().
    """
    return _config(quiet=True) is not None


# 토큰 자기조회 — 비밀값 미반환 (variables 쿼리는 서비스 env 값 전부를 돌려주므로 금지)
TOKEN_PROBE_QUERY = "query{projectToken{projectId environmentId}}"
TOKEN_PROBE_TIMEOUT_S = 10
# probe 판정 — 'ok'·'invalid'는 확정(캐시), 'unknown'은 일시 장애(캐시 안 함)
PROBE_OK, PROBE_INVALID, PROBE_UNKNOWN = "ok", "invalid", "unknown"
# (sha256(토큰)+project_id) → 확정 판정('ok'|'invalid'). 토큰 원문은 캐시 key로도 보관하지 않음.
_token_valid_cache: dict[str, str] = {}


def reset_token_cache() -> None:
    """probe/token_valid 캐시 초기화 (테스트용)."""
    _token_valid_cache.clear()


def _token_cache_key(cfg: dict) -> str:
    return hashlib.sha256(f"{cfg['token']}\0{cfg['project_id']}".encode()).hexdigest()


def probe(force: bool = False) -> str:
    """Railway 토큰 인증 상태 — 'ok' | 'invalid' | 'unknown' (인증 GraphQL 1회, 프로세스+토큰 캐시).

    ok: HTTP 200 + errors 없음 + data.projectToken.projectId == RAILWAY_PROJECT_ID.
    invalid (확정): 설정 누락(네트워크 호출 없음)·HTTP 401/403·HTTP 200인데 GraphQL 에러
      ("Not Authorized" 등)·projectToken 없음·다른 프로젝트 토큰.
    unknown (일시 장애): 네트워크 예외·5xx·429·그 밖의 HTTP·JSON 아님(또는 dict 아님).
    캐시는 확정 판정(ok/invalid)만 — unknown은 다음 호출에서 재확인. force=True면 캐시 무시.
    토큰 원문은 로그·반환값 어디에도 넣지 않음.
    """
    cfg = _config(quiet=True)
    if not cfg:
        return PROBE_INVALID
    key = _token_cache_key(cfg)
    if not force and key in _token_valid_cache:
        return _token_valid_cache[key]
    secrets = (cfg["token"],)
    try:
        with httpx.Client(timeout=TOKEN_PROBE_TIMEOUT_S) as c:
            r = c.post(GRAPHQL_URL,
                       json={"query": TOKEN_PROBE_QUERY},
                       headers={"Project-Access-Token": cfg["token"],
                                "Content-Type": "application/json"})
    except Exception as e:
        log.warning("[model_router.railway_env] 토큰 확인 실패(네트워크): %s", type(e).__name__)
        return PROBE_UNKNOWN
    status = getattr(r, "status_code", None)
    try:
        data = r.json()
    except Exception:
        data = None
    pt = None
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        pt = data["data"].get("projectToken")
    if status in (401, 403):
        state = PROBE_INVALID
    elif status != 200 or not isinstance(data, dict):
        # 5xx·429·기타 HTTP, 또는 200인데 JSON이 아님(프록시·게이트웨이 HTML 등) → 판정 불가
        state = PROBE_UNKNOWN
    elif (not data.get("errors") and isinstance(pt, dict)
          and pt.get("projectId") == cfg["project_id"]):
        state = PROBE_OK
    else:
        state = PROBE_INVALID
    if state != PROBE_OK:
        if isinstance(pt, dict) and pt.get("projectId") and pt.get("projectId") != cfg["project_id"]:
            why = "다른 프로젝트의 토큰"
        else:
            why = _error_summary(r, data, secrets)
            if status == 200 and not isinstance(data, dict):
                why += " (JSON 아님)"
        log.warning("[model_router.railway_env] Railway 토큰 %s — %s",
                    "무효(폐기?)" if state == PROBE_INVALID else "확인 실패(일시 장애?)",
                    _scrub(why, secrets))
    if state != PROBE_UNKNOWN:
        _token_valid_cache[key] = state
    return state


def token_valid(force: bool = False) -> bool:
    """Railway 토큰이 실제로 인증되는지 — probe() == 'ok' (캐시 규칙 동일).

    False: invalid(설정 누락·폐기·다른 프로젝트) 또는 unknown(일시 장애 — fail closed, 캐시 안 함).
    둘을 구분해야 하는 호출부(사용자 안내 문구가 달라지는 tg_login)는 probe()를 직접 사용.
    """
    return probe(force) == PROBE_OK


def _target_sids(cfg: dict, service_ids: list[str] | None) -> list[str]:
    """None = 등록된 전 서비스(RAILWAY_SERVICE_IDS). 지정 시 그 서비스만."""
    if service_ids is None:
        return cfg["service_ids"]
    return [s.strip() for s in service_ids if s and s.strip()]


def _scrub(text: str, secrets: tuple[str | None, ...]) -> str:
    """에러 문자열에서 value·토큰 원문 제거 (GraphQL 에러가 input을 echo할 수 있음)."""
    out = text
    for s in secrets:
        if s and len(s) >= 4:
            out = out.replace(s, "‹redacted›")
    return out


def _error_summary(r, data, secrets: tuple[str | None, ...] = ()) -> str:
    """GraphQL 응답 → 에러 요약 (message 필드만, 응답 원문 dump 금지).

    secrets는 자르기 *전에* 제거 — 160자로 먼저 자르면 echo된 값의 앞부분 조각이 정확일치
    치환을 빠져나간다 (TG_SESSION_STRING ~353자).
    """
    msgs: list[str] = []
    errs = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errs, list):
        for e in errs[:3]:
            m = e.get("message") if isinstance(e, dict) else None
            if m:
                msgs.append(_scrub(str(m), secrets)[:160])
    status = getattr(r, "status_code", "?")
    return f"HTTP {status}" + (f" {'; '.join(msgs)}" if msgs else "")


def _post(cfg: dict, payload: dict, field: str, sid: str,
          secrets: tuple[str | None, ...]) -> str | None:
    """mutation 1건 실행 → 성공 시 None, 실패 시 (값 제거된) 에러 문자열."""
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(GRAPHQL_URL,
                       json=payload,
                       headers={"Project-Access-Token": cfg["token"],
                                "Content-Type": "application/json"})
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code == 200 and (data.get("data") or {}).get(field):
            return None
        return _scrub(f"sid={sid[:8]}: {_error_summary(r, data, secrets)}", secrets)
    except Exception as e:
        # scrub 후 자르기 (자른 뒤 scrub하면 값 조각이 남음)
        return _scrub(f"sid={sid[:8]}: {type(e).__name__}: {_scrub(str(e), secrets)[:160]}",
                      secrets)


def upsert_variable(name: str, value: str,
                    service_ids: list[str] | None = None) -> tuple[bool, str]:
    """env var을 서비스들에 upsert. (success, message) 반환 — message에 value 없음.

    service_ids=None이면 등록된 전 서비스(RAILWAY_SERVICE_IDS), 지정하면 그 서비스만
    (비밀값을 필요한 서비스에만 넣어 노출면 축소).
    """
    cfg = _config()
    if not cfg:
        return False, ("Railway 설정 누락 — RAILWAY_PROJECT_ACCESS_TOKEN을 Variables에 추가 ""(PROJECT/ENV/SERVICE ID는 Railway 자동 주입). 토큰: railway.app → Project → Tokens")
    sids = _target_sids(cfg, service_ids)
    if not sids:
        return False, "대상 service id 없음"

    secrets = (value, cfg["token"])
    ok_count = 0
    errors = []
    for sid in sids:
        payload = {
            "query": UPSERT_MUTATION,
            "variables": {"input": {
                "projectId": cfg["project_id"],
                "environmentId": cfg["env_id"],
                "serviceId": sid,
                "name": name,
                "value": value,
            }},
        }
        err = _post(cfg, payload, "variableUpsert", sid, secrets)
        if err is None:
            ok_count += 1
        else:
            errors.append(err)

    if ok_count == len(sids):
        return True, f"✅ {ok_count}개 서비스 upsert 성공"
    if ok_count > 0:
        return True, f"⚠ {ok_count}/{len(sids)} 서비스 성공 — 일부 실패: {'; '.join(errors)}"
    return False, f"❌ 전부 실패: {'; '.join(errors)}"


DELETE_MUTATION = """mutation del($input: VariableDeleteInput!){variableDelete(input:$input)}"""


def delete_variable(name: str,
                    service_ids: list[str] | None = None) -> tuple[bool, str]:
    """env var을 서비스들에서 삭제. (success, message) 반환.

    service_ids=None이면 등록된 전 서비스(RAILWAY_SERVICE_IDS), 지정하면 그 서비스만.
    주의(CLAUDE.md §6 실측): variableDelete는 재배포를 트리거하지 않음 — 실행 중
    컨테이너에는 남아 있으므로, 이후 upsert(재배포 유발)나 명시 재배포가 필요.
    """
    cfg = _config()
    if not cfg:
        return False, "Railway 설정 누락"
    sids = _target_sids(cfg, service_ids)
    if not sids:
        return False, "대상 service id 없음"
    # 삭제 대상 값도 에러 echo에서 제거 (현재 컨테이너 env에 있을 때)
    secrets = (os.environ.get(name), cfg["token"])
    ok_count = 0
    errors = []
    for sid in sids:
        payload = {
            "query": DELETE_MUTATION,
            "variables": {"input": {
                "projectId": cfg["project_id"],
                "environmentId": cfg["env_id"],
                "serviceId": sid,
                "name": name,
            }},
        }
        err = _post(cfg, payload, "variableDelete", sid, secrets)
        if err is None:
            ok_count += 1
        else:
            errors.append(err)
    if ok_count == len(sids):
        return True, f"✅ {ok_count}개 서비스 삭제"
    return ok_count > 0, f"{ok_count}/{len(sids)} 삭제 — {'; '.join(errors)}"
