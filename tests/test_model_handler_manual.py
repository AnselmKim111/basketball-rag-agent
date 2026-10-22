"""model_eval_job 수동 적용 경로 — history 기록 (2026-10-22 리뷰 반영).

Railway 토큰 미설정이면 automatic 추천도 '[수동 적용 필요]'로만 안내된다. /model_approve의
수동 경로처럼 history에 'approved'로 남겨야, 사용자가 대시보드에서 반영한 뒤 Layer D
(rollback._previous_model_for_env)가 이전 모델을 찾아 롤백할 수 있다.
토큰이 남아 있지만 폐기된 사고 상태(probe 'invalid')도 같은 수동 경로 — 예전엔 upsert 401 →
railway_failed라 history 없음·pending 잔류·eval은 '변경 권고 없음'.
반대로 Railway API 일시 장애(probe 'unknown' — 네트워크·5xx)는 수동/무효가 아니라 실패:
history 'approved' 금지, /model_approve 항목 pending 유지 (재승인 가능).
네트워크 미사용 (canary·추천·Railway 전부 mock, probe는 가짜 httpx).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from src.model_router import approval_store, canary, handler, railway_env, rollback
from src.model_router.candidates import TIER_ENV

ENV = "OPENROUTER_MODEL"
OLD, NEW = "deepseek/deepseek-v4-old", "deepseek/deepseek-v4-new"


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((str(chat_id), text))


def _rec(classification: str = "automatic") -> dict:
    return {"env_name": ENV, "old_model": OLD, "new_model": NEW,
            "classification": classification, "reason": "가성비 개선",
            "score_old": 0.5, "score_new": 0.7, "savings_pct": 30}


@pytest.fixture
def iso(monkeypatch, tmp_path):
    monkeypatch.setattr(approval_store, "_store_dir", lambda: tmp_path)
    for envs in TIER_ENV.values():
        for e in envs:
            monkeypatch.delenv(e, raising=False)
    monkeypatch.delenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", raising=False)
    monkeypatch.setenv("REPORT_CHAT_ID", "42")
    monkeypatch.setattr(railway_env, "probe", lambda *a, **kw: railway_env.PROBE_OK)  # 네트워크 금지
    canary_calls: list = []
    monkeypatch.setattr(canary, "run_canary",
                        lambda env, model: canary_calls.append((env, model)) or {"skipped": True})
    monkeypatch.setattr(handler, "build_recommendations", lambda: [_rec()])
    return tmp_path


def _history(tmp_path) -> list[dict]:
    p = tmp_path / "history.json"
    return json.loads(p.read_text()) if p.exists() else []


def test_eval_manual_automatic_rec_recorded_for_layer_d(iso, monkeypatch):
    monkeypatch.setattr(railway_env, "available", lambda: False)

    def _no_upsert(*a, **kw):
        raise AssertionError("토큰 없으면 upsert 시도 금지")

    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    bot = _Bot()
    asyncio.run(handler.model_eval_job(bot))

    # 알림은 기존대로 '[수동 적용 필요]'
    assert len(bot.sent) == 1 and bot.sent[0][0] == "42"
    assert "수동 적용 필요" in bot.sent[0][1] and NEW in bot.sent[0][1]
    # history: /model_approve 수동 경로와 같은 status 'approved' + 안내 note + resolved_at
    hist = _history(iso)
    assert len(hist) == 1
    h = hist[0]
    assert h["status"] == "approved"
    assert (h["env_name"], h["old_model"], h["new_model"]) == (ENV, OLD, NEW)
    assert "수동 적용" in h["note"] and isinstance(h["resolved_at"], int)
    # automatic은 pending에 안 들어감 (기존 계약)
    assert approval_store.load_pending() == []
    # 사용자가 대시보드에서 NEW로 바꾼 뒤 Layer D가 롤백 대상(OLD)을 찾을 수 있어야 함
    assert rollback._previous_model_for_env(ENV, NEW) == OLD


def test_eval_applied_path_unchanged(iso, monkeypatch):
    """토큰 있고 upsert 성공 — 기존처럼 'auto_applied' 1건만 (manual 기록과 중복 없음)."""
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable",
                        lambda name, value, service_ids=None: (True, "✅ upsert 성공"))
    asyncio.run(handler.model_eval_job(_Bot()))
    hist = _history(iso)
    assert [h["status"] for h in hist] == ["auto_applied"]
    assert rollback._previous_model_for_env(ENV, NEW) == OLD


def test_eval_canary_failure_not_recorded(iso, monkeypatch):
    """canary 실패(변경 거부)는 history에 남기지 않음 — 롤백 대상 오염 금지."""
    monkeypatch.setattr(railway_env, "available", lambda: False)
    monkeypatch.setattr(canary, "run_canary", lambda env, model: {
        "passed": False, "failures": ["json parse"], "sentinels": 1})
    bot = _Bot()
    asyncio.run(handler.model_eval_job(bot))
    assert _history(iso) == []
    assert rollback._previous_model_for_env(ENV, NEW) is None


@pytest.mark.parametrize("flag", ["auth_error", "credit_error"])
def test_eval_canary_key_failure_reported_not_dropped(iso, monkeypatch, flag):
    """canary가 키/크레딧 장애로 실패 → '변경 권고 없음'이 아니라 [적용 보류] 섹션으로 안내."""
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(canary, "run_canary", lambda env, model: {
        "passed": False, "failures": ["401"], "sentinels": 0, flag: True})
    bot = _Bot()
    asyncio.run(handler.model_eval_job(bot))
    text = bot.sent[-1][1]
    assert "변경 권고 없음" not in text
    assert "적용 보류" in text and ENV in text
    assert _history(iso) == []


# ---------------------------------------------------------------- 폐기 토큰 (사고 상태)

_REAL_PROBE = railway_env.probe  # iso fixture가 덮기 전 원본


def _revoked_railway(monkeypatch, transient: bool = False) -> list[str]:
    """토큰은 설정돼 있으나 폐기 — 실제 probe가 가짜 GraphQL 'Not Authorized'를 받음.
    transient=True면 토큰은 유효하지만 Railway API 일시 장애(ConnectError) → probe 'unknown'.
    반환: GraphQL 호출 쿼리 목록 (upsert mutation이 섞이면 안 됨)."""
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", "tok-revoked-fake-0000000000")
    monkeypatch.delenv("RAILWAY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_IDS", raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid-fake")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid-fake")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "sid-fake")
    monkeypatch.setattr(railway_env, "probe", _REAL_PROBE)
    railway_env.reset_token_cache()
    queries: list[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"errors": [{"message": "Not Authorized"}], "data": None}

    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            queries.append((json or {}).get("query", ""))
            if transient:
                raise httpx.ConnectError("blip", request=httpx.Request("POST", url))
            return _Resp()

    monkeypatch.setattr(railway_env, "httpx", SimpleNamespace(Client=_Client))
    return queries


@pytest.fixture
def revoked(iso, monkeypatch):
    """iso 이후 적용 (iso의 probe='ok' 스텁을 실제 함수로 되돌림)."""
    q = _revoked_railway(monkeypatch)
    yield q
    railway_env.reset_token_cache()


def test_eval_revoked_token_recorded_as_manual(iso, revoked):
    bot = _Bot()
    asyncio.run(handler.model_eval_job(bot))
    assert revoked and all("mutation" not in q for q in revoked), "upsert 시도 금지"
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "수동 적용 필요" in text and NEW in text
    assert "변경 권고 없음" not in text
    hist = _history(iso)
    assert [h["status"] for h in hist] == ["approved"]
    assert rollback._previous_model_for_env(ENV, NEW) == OLD


def test_approve_revoked_token_marks_approved(iso, revoked, monkeypatch):
    monkeypatch.delenv("REPORT_ALLOWED_CHAT_IDS", raising=False)  # admin = REPORT_CHAT_ID(42)
    approval_store.save_pending([{
        "id": 1, "env_name": ENV, "old_model": OLD, "new_model": NEW, "status": "pending",
    }])
    replies: list[str] = []

    class _M:
        async def reply_text(self, text, **kw):
            replies.append(text)

    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=42), message=_M())
    asyncio.run(handler.model_approve_cmd(upd, SimpleNamespace(args=["1"])))
    joined = "\n".join(replies)
    assert all("mutation" not in q for q in revoked), "upsert 시도 금지"
    assert "수동" in joined and NEW in joined and "전부 실패" not in joined
    assert approval_store.load_pending()[0]["status"] == "approved"
    assert rollback._previous_model_for_env(ENV, NEW) == OLD


# ---------------------------------------------------------------- Railway API 일시 장애


@pytest.fixture
def transient(iso, monkeypatch):
    q = _revoked_railway(monkeypatch, transient=True)
    yield q
    railway_env.reset_token_cache()


def test_approve_transient_railway_stays_pending(iso, transient, monkeypatch):
    """일시 장애 → 승인 항목 소비 금지 (pending 유지·history 없음), '무효' 오안내 없음."""
    monkeypatch.delenv("REPORT_ALLOWED_CHAT_IDS", raising=False)
    approval_store.save_pending([{
        "id": 1, "env_name": ENV, "old_model": OLD, "new_model": NEW, "status": "pending",
    }])
    replies: list[str] = []

    class _M:
        async def reply_text(self, text, **kw):
            replies.append(text)

    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=42), message=_M())
    asyncio.run(handler.model_approve_cmd(upd, SimpleNamespace(args=["1"])))
    joined = "\n".join(replies)
    assert transient and all("mutation" not in q for q in transient), "upsert 시도 금지"
    assert "Railway API 일시 장애" in joined and NEW in joined
    assert "무효" not in joined
    assert approval_store.load_pending()[0]["status"] == "pending"
    assert _history(iso) == []


def test_eval_transient_railway_not_recorded_as_manual(iso, transient):
    """주간 eval도 일시 장애를 '[수동 적용 필요 — 토큰 미설정/무효]'·'approved'로 기록하지 않음."""
    bot = _Bot()
    asyncio.run(handler.model_eval_job(bot))
    assert transient and all("mutation" not in q for q in transient)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "무효" not in text
    # 반영 실패를 '변경 권고 없음'으로 숨기지 않음 — 재시도 안내
    assert "적용 실패" in text and NEW in text and "/model_eval" in text
    assert "변경 권고 없음" not in text
    assert _history(iso) == []

