"""OpenRouter API fetcher — /models (가격) + /activity (사용량).

management key 필요 (`OPENROUTER_MANAGEMENT_KEY`). 없으면 활동 데이터 없이 graceful degrade.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"


def _mgmt_key() -> str | None:
    return os.getenv("OPENROUTER_MANAGEMENT_KEY")


def _inf_key() -> str | None:
    return os.getenv("OPENROUTER_API_KEY")


def fetch_models() -> dict[str, dict]:
    """모든 활성 모델 → {model_id: {in_price, out_price, ctx_length, created_at, name}}.

    인증 불요. 실패 시 빈 dict.
    """
    key = _mgmt_key() or _inf_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(f"{BASE_URL}/models", headers=headers)
            r.raise_for_status()
            data = r.json().get("data", [])
    except Exception:
        log.exception("[model_router.fetcher] /models fetch 실패")
        return {}

    out = {}
    for m in data:
        mid = m.get("id", "")
        if not mid or mid.startswith("~"):  # ~latest 별칭 제외
            continue
        pr = m.get("pricing", {})
        try:
            in_price = float(pr.get("prompt", 0)) * 1e6   # $/M tokens
            out_price = float(pr.get("completion", 0)) * 1e6
        except (ValueError, TypeError):
            continue
        out[mid] = {
            "id": mid,
            "name": m.get("name", mid),
            "in_price": in_price,
            "out_price": out_price,
            "ctx_length": m.get("context_length", 0),
            "created_at": m.get("created", 0),
        }
    log.info("[model_router.fetcher] fetched %d models", len(out))
    return out


def fetch_activity(days: int = 30) -> dict[str, dict]:
    """30일 모델별 사용량 → {model_id: {req, cost, in_tokens, out_tokens}}.

    management key 필요. 없으면 빈 dict.
    """
    key = _mgmt_key()
    if not key:
        log.warning("[model_router.fetcher] OPENROUTER_MANAGEMENT_KEY 미설정 — activity skip")
        return {}
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{BASE_URL}/activity",
                      params={"days": days},
                      headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            rows = r.json().get("data", [])
    except Exception:
        log.exception("[model_router.fetcher] /activity fetch 실패")
        return {}

    agg = defaultdict(lambda: {"req": 0, "cost": 0.0, "in_tokens": 0, "out_tokens": 0})
    for row in rows:
        mid = row.get("model", "")
        if not mid:
            continue
        agg[mid]["req"] += int(row.get("requests", 0))
        agg[mid]["cost"] += float(row.get("usage", 0))
        agg[mid]["in_tokens"] += int(row.get("prompt_tokens", 0))
        agg[mid]["out_tokens"] += int(row.get("completion_tokens", 0))
    log.info("[model_router.fetcher] activity %d models", len(agg))
    return dict(agg)
