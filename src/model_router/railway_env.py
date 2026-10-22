"""Railway GraphQL variableUpsert — 승인된 모델 변경을 환경변수에 자동 반영.

필요 env: `RAILWAY_PROJECT_ACCESS_TOKEN` (alias `RAILWAY_ACCESS_TOKEN`), `RAILWAY_PROJECT_ID`,
`RAILWAY_ENVIRONMENT_ID` (둘 다 Railway 자동 주입), `RAILWAY_SERVICE_IDS` (콤마 구분 — 여러
서비스에 동시 upsert; 미설정 시 자동 주입 `RAILWAY_SERVICE_ID` 단일).

반환 메시지·로그에 변수 값(value)은 절대 넣지 않음 — TG_SESSION_STRING 같은 비밀도 이
경로로 쓰이고, 메시지는 텔레그램으로 나간다 (2026-10-21 Railway env 유출 사고 후속).
"""
from __future__ import annotations

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
    """Railway API 쓰기 가능 설정(토큰·ID) 여부. False면 호출부는 수동 적용 안내."""
    return _config(quiet=True) is not None


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


def _error_summary(r, data) -> str:
    """GraphQL 응답 → 에러 요약 (message 필드만, 응답 원문 dump 금지)."""
    msgs: list[str] = []
    errs = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errs, list):
        for e in errs[:3]:
            m = e.get("message") if isinstance(e, dict) else None
            if m:
                msgs.append(str(m)[:160])
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
        return _scrub(f"sid={sid[:8]}: {_error_summary(r, data)}", secrets)
    except Exception as e:
        return _scrub(f"sid={sid[:8]}: {type(e).__name__}: {str(e)[:160]}", secrets)


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
