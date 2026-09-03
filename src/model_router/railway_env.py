"""Railway GraphQL variableUpsert — 승인된 모델 변경을 환경변수에 자동 반영.

필요 env: `RAILWAY_PROJECT_ACCESS_TOKEN`, `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID`,
`RAILWAY_SERVICE_IDS` (콤마 구분 — 여러 서비스에 동시 upsert).
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"

UPSERT_MUTATION = """mutation upsert($input: VariableUpsertInput!){variableUpsert(input:$input)}"""


def _config() -> dict | None:
    tok = os.getenv("RAILWAY_PROJECT_ACCESS_TOKEN")
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
        log.warning("[model_router.railway_env] Railway env 누락: %s — upsert 불가", missing)
        return None
    return {
        "token": tok, "project_id": pid, "env_id": eid,
        "service_ids": [s.strip() for s in sids.split(",") if s.strip()],
    }


def upsert_variable(name: str, value: str) -> tuple[bool, str]:
    """env var을 모든 등록된 서비스에 upsert. (success, message) 반환."""
    cfg = _config()
    if not cfg:
        return False, ("Railway 설정 누락 — RAILWAY_PROJECT_ACCESS_TOKEN을 Variables에 추가 ""(PROJECT/ENV/SERVICE ID는 Railway 자동 주입). 토큰: railway.app → Project → Tokens")

    ok_count = 0
    errors = []
    for sid in cfg["service_ids"]:
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
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(GRAPHQL_URL,
                           json=payload,
                           headers={"Project-Access-Token": cfg["token"],
                                    "Content-Type": "application/json"})
            data = r.json()
            if r.status_code == 200 and data.get("data", {}).get("variableUpsert"):
                ok_count += 1
            else:
                errors.append(f"sid={sid[:8]}: {data.get('errors') or data}")
        except Exception as e:
            errors.append(f"sid={sid[:8]}: {e}")

    if ok_count == len(cfg["service_ids"]):
        return True, f"✅ {ok_count}개 서비스 upsert 성공"
    if ok_count > 0:
        return True, f"⚠ {ok_count}/{len(cfg['service_ids'])} 서비스 성공 — 일부 실패: {'; '.join(errors)}"
    return False, f"❌ 전부 실패: {'; '.join(errors)}"
