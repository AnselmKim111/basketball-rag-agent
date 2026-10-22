"""Layer D 롤백 가드 + model_router 하드닝 — 2026-10-21 Railway env 유출 사고 후속.

- 401 breaker 최근 작동 → 판정 skip (upsert·메시지 없음)
- 프로바이더 2곳+ 모델 동시 실패 → systemic (롤백 안 함, 6h 재알림 dedup — 새 프로바이더만 재알림)
  · 집계는 health 전 모델·실패 ≥3 + 실패율 초과 (저볼륨 티어·env 밖 chain 모델도 증거,
    평시 우발 실패 1/3·1/4는 제외)
  · 롤백이 필요한 env가 없으면 systemic 판정·알림 자체 생략
- 한 프로바이더 모델 2개 동시 실패(deepseek flash+pro) → env별 정상 롤백
- 같은 모델을 쓰는 env 2개 → 같은 회차에 둘 다 롤백 (reset은 루프 뒤 모델당 1회)
- 이전 모델이 지금 실패 중(실패 ≥3) → 그 env 롤백 보류 (held)
- Railway 토큰 없음/무효(probe 'invalid') → upsert 시도 없이 수동 안내
- Railway API 일시 장애(probe 'unknown') → 증거 유지·다음 시간 재시도 ('무효' 안내 금지)
- 제외 프로바이더(anthropic) 모델로 롤백 금지
- railway_env: service_ids 지정·available()·에러 메시지에 value 미포함
- handler: 토큰 없으면 수동 안내, Railway 일시 장애는 railway_failed(pending 유지),
  canary 401은 '품질 미달' 아님, 제어 명령은 '*' 불인정, 401 알림 미전달 → 다음 차단 때 재알림
- fetcher: /models에 Authorization 헤더 없음
네트워크 미사용 (httpx·canary·upsert 전부 mock).
"""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

from src.model_router import (
    approval_store, canary, fetcher, handler, health, railway_env, rollback,
)
from src.model_router.candidates import TIER_ENV

ALL_ENVS = sorted({e for envs in TIER_ENV.values() for e in envs})


def _sm():
    """현재 src.summarizer — 다른 테스트(test_idea_sourcing)가 sys.modules에서 빼고
    재import시키므로, rollback의 지연 import와 같은 객체를 매번 조회."""
    return importlib.import_module("src.summarizer")


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    """store를 tmp로, TIER env 전부 비움, breaker 초기화."""
    monkeypatch.setattr(health, "_store_dir", lambda: tmp_path)
    monkeypatch.setattr(approval_store, "_store_dir", lambda: tmp_path)
    for e in ALL_ENVS:
        monkeypatch.delenv(e, raising=False)
    monkeypatch.delenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", raising=False)
    # 토큰 probe 기본 'ok' (네트워크 금지) — 폐기·일시 장애 케이스는 테스트에서 덮음
    monkeypatch.setattr(railway_env, "probe", lambda *a, **kw: railway_env.PROBE_OK)
    _sm().reset_auth_breaker()
    yield tmp_path
    _sm().reset_auth_breaker()


def _fail(model: str, n: int = 10) -> None:
    for _ in range(n):
        health.record_call(model, False, 100)


def _history(tmp_path, items: list[dict]) -> None:
    (tmp_path / "history.json").write_text(json.dumps(items))


class _UpsertSpy:
    def __init__(self, ok: bool = True, msg: str = "✅ 1개 서비스 upsert 성공") -> None:
        self.calls: list[tuple] = []
        self.ok, self.msg = ok, msg

    def __call__(self, name, value, service_ids=None):
        self.calls.append((name, value))
        return self.ok, self.msg


def _no_upsert(*a, **kw):
    raise AssertionError("upsert 호출되면 안 됨")


# ---------------------------------------------------------------- Layer D


def test_skip_when_auth_breaker_tripped(monkeypatch, _iso):
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash")
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    _sm().trip_auth_breaker()
    assert rollback.check_and_rollback() == []


def test_breaker_guard_expires_after_2h(monkeypatch, _iso):
    clock = [5000.0]
    monkeypatch.setattr(_sm(), "_clock", lambda: clock[0])
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash")
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    _sm().trip_auth_breaker()
    clock[0] += rollback.AUTH_GUARD_S + 1
    actions = rollback.check_and_rollback()
    assert spy.calls == [("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")]
    assert actions[0]["applied"] is True


def test_systemic_two_providers_no_upsert(monkeypatch, _iso):
    """서로 다른 프로바이더 모델이 동시에 실패 → 플랫폼/키 장애로 보고 롤백 보류."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("EARNINGS_SYNTHESIS_MODEL", "openai/gpt-5.2")
    _fail("deepseek/deepseek-v4-flash")
    _fail("openai/gpt-5.2")
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    actions = rollback.check_and_rollback()
    assert len(actions) == 1
    assert actions[0]["applied"] is False and actions[0]["note"] == "systemic"
    assert "deepseek" in actions[0]["reason"] and "openai" in actions[0]["reason"]
    msg = rollback.format_rollback_message(actions)
    assert "플랫폼/키 장애" in msg


def test_same_provider_two_models_rolls_back(monkeypatch, _iso):
    """한 프로바이더 장애(deepseek flash+pro 동시 실패)는 systemic 아님 — 운영 티어 구성
    그대로 env별 이전 모델(다른 프로바이더)로 롤백해야 Layer D가 의미 있음."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_NARROW_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro")
    _fail("deepseek/deepseek-v4-flash", 15)
    _fail("deepseek/deepseek-v4-pro", 15)
    _history(_iso, [
        {"env_name": "OPENROUTER_MODEL", "status": "approved",
         "old_model": "moonshotai/kimi-k2.6", "new_model": "deepseek/deepseek-v4-flash"},
        {"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
         "old_model": "openai/gpt-5.1", "new_model": "deepseek/deepseek-v4-pro"},
    ])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    actions = rollback.check_and_rollback()
    assert ("OPENROUTER_MODEL", "moonshotai/kimi-k2.6") in spy.calls
    assert ("IDEA_SYNTHESIS_MODEL", "openai/gpt-5.1") in spy.calls
    assert all(a.get("note") != "systemic" for a in actions)
    assert not (_iso / "systemic_alert.json").exists()


def test_systemic_alert_dedup_6h(monkeypatch, _iso):
    """장애가 이어져도 같은 systemic 알림은 6h에 1번 (매시간 cron 재알림 방지)."""
    clock = [1_000_000.0]
    monkeypatch.setattr(rollback, "_now", lambda: clock[0])
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("EARNINGS_SYNTHESIS_MODEL", "openai/gpt-5.2")
    _fail("deepseek/deepseek-v4-flash")
    _fail("openai/gpt-5.2")
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    assert [a["note"] for a in rollback.check_and_rollback()] == ["systemic"]
    clock[0] += 3600
    assert rollback.check_and_rollback() == [], "1h 뒤 같은 집합 — 재알림 생략"
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODEL", "moonshotai/kimi-k2.6")
    _fail("moonshotai/kimi-k2.6")
    actions = rollback.check_and_rollback()
    assert [a["note"] for a in actions] == ["systemic"], "실패 집합 변경 → 새 알림"
    clock[0] += rollback.SYSTEMIC_ALERT_DEDUP_S + 1
    assert [a["note"] for a in rollback.check_and_rollback()] == ["systemic"], "6h 경과 → 재알림"


def _reset_all_health() -> None:
    for m in list(health._load()):
        health.reset(m)


def test_systemic_dedup_model_set_flips_one_alert(monkeypatch, _iso):
    """장애 중 모델별 호출량이 샘플 하한을 넘나들어 실패 *모델* 집합이 매시간 바뀌어도
    실패 *프로바이더* 집합이 늘지 않으면 6h 내 알림 1회 (2026-10-22 시뮬: 3h에 3회).
    폴백 env(kimi)는 매시간 롤백 하한 이상 실패 — systemic 판정은 롤백 대상이 있을 때만 돎."""
    clock = [2_000_000.0]
    monkeypatch.setattr(rollback, "_now", lambda: clock[0])
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODEL", "moonshotai/kimi-k2.6")
    hours = [
        ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro", "moonshotai/kimi-k2.6"],
        ["deepseek/deepseek-v4-flash", "moonshotai/kimi-k2.6"],
        ["deepseek/deepseek-v4-pro", "moonshotai/kimi-k2.6", "moonshotai/kimi-k2.5"],
        ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro", "moonshotai/kimi-k2.6"],
    ]
    alerts = 0
    for models in hours:
        _reset_all_health()
        for m in models:
            _fail(m, health.ROLLBACK_MIN_SAMPLES if m == "moonshotai/kimi-k2.6" else 5)
        acts = rollback.check_and_rollback()
        assert all(a["note"] == "systemic" for a in acts)
        alerts += len(acts)
        clock[0] += 3600
    assert alerts == 1
    stored = json.loads((_iso / "systemic_alert.json").read_text())
    assert stored["providers"] == ["deepseek", "moonshotai"] and "models" not in stored


def test_systemic_dedup_new_provider_realerts_and_accumulates(monkeypatch, _iso):
    """새 프로바이더 합류 → 재알림, 기록은 합집합 — 이후 원래 집합으로 돌아가도 재알림 없음."""
    clock = [3_000_000.0]
    monkeypatch.setattr(rollback, "_now", lambda: clock[0])
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    for env, m in (("OPENROUTER_MODEL", "a/x"), ("IDEA_NARROW_MODEL", "b/y"),
                   ("IDEA_SYNTHESIS_MODEL", "c/z")):
        monkeypatch.setenv(env, m)  # 매시간 롤백 대상 존재 → systemic 판정이 돎
    seq = [(["a/x", "b/y"], 1), (["a/x", "c/z"], 1), (["a/x", "b/y"], 0), (["b/y", "c/z"], 0)]
    for models, expect in seq:
        _reset_all_health()
        for m in models:
            _fail(m, health.ROLLBACK_MIN_SAMPLES)
        assert len(rollback.check_and_rollback()) == expect, models
        clock[0] += 3600


def test_systemic_dedup_legacy_models_record(monkeypatch, _iso):
    """구버전 기록({ts, models})도 프로바이더로 해석해 dedup (배포 직후 중복 알림 방지)."""
    clock = [4_000_000.0]
    monkeypatch.setattr(rollback, "_now", lambda: clock[0])
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    (_iso / "systemic_alert.json").write_text(json.dumps(
        {"ts": clock[0] - 3600, "models": ["deepseek/deepseek-v4-flash", "openai/gpt-5.2"]}))
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro")  # 롤백 대상 있음
    _fail("deepseek/deepseek-v4-pro", health.ROLLBACK_MIN_SAMPLES)
    _fail("openai/gpt-5.1", 5)
    assert rollback.check_and_rollback() == []


def test_systemic_counts_low_volume_second_provider(monkeypatch, _iso):
    """두 번째 프로바이더 모델이 저볼륨(4건, 롤백 하한 10 미만)이어도 systemic —
    예전엔 미집계라 IDEA_SYNTHESIS가 실패 중인 openai/gpt-5.1로 롤백됐음."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro")
    monkeypatch.setenv("EARNINGS_SYNTHESIS_MODEL", "openai/gpt-5.2")
    _fail("deepseek/deepseek-v4-flash", 15)
    _fail("deepseek/deepseek-v4-pro", 15)
    _fail("openai/gpt-5.2", health.SIGNAL_MIN_SAMPLES + 1)
    _history(_iso, [
        {"env_name": "OPENROUTER_MODEL", "status": "approved",
         "old_model": "moonshotai/kimi-k2.6", "new_model": "deepseek/deepseek-v4-flash"},
        {"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
         "old_model": "openai/gpt-5.1", "new_model": "deepseek/deepseek-v4-pro"},
    ])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    actions = rollback.check_and_rollback()
    assert [a["note"] for a in actions] == ["systemic"]
    assert actions[0]["providers"] == ["deepseek", "openai"]


def test_systemic_counts_models_outside_env(monkeypatch, _iso):
    """env에 없는 chain 2차 모델(qwen)의 실패도 프로바이더 증거로 집계."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash", 15)
    _fail("qwen/qwen3.7-plus", health.SIGNAL_MIN_SAMPLES)
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    assert [a["note"] for a in rollback.check_and_rollback()] == ["systemic"]


def test_systemic_ignores_below_signal_floor(monkeypatch, _iso):
    """다른 프로바이더 실패가 SIGNAL_MIN_SAMPLES 미만(2건)이면 증거 부족 — 정상 롤백."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash", 15)
    _fail("openai/gpt-5.2", health.SIGNAL_MIN_SAMPLES - 1)
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    rollback.check_and_rollback()
    assert spy.calls == [("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")]


def test_failing_models_all_recorded_with_floor(_iso):
    _fail("a/one", 3)
    _fail("b/two", 2)
    for _ in range(5):
        health.record_call("c/ok", True, 100)
    assert health.failing_models(None, min_samples=3) == ["a/one"]
    assert health.failing_models(None) == []  # 기본 하한(10)은 롤백 판정과 동일


def test_shared_model_envs_all_rolled_back(monkeypatch, _iso):
    """운영 배치 그대로 — OPENROUTER+NARROW가 v4-flash, IDEA+REPORT_SYNTHESIS가 v4-pro 공유.
    env 하나 롤백하며 reset하면 같은 모델의 다른 env가 판정에서 빠졌음 (전부 롤백돼야 함)."""
    env = {"OPENROUTER_MODEL": "deepseek/deepseek-v4-flash",
           "IDEA_NARROW_MODEL": "deepseek/deepseek-v4-flash",
           "IDEA_SYNTHESIS_MODEL": "deepseek/deepseek-v4-pro",
           "REPORT_SYNTHESIS_MODEL": "deepseek/deepseek-v4-pro"}
    prev = {"OPENROUTER_MODEL": "moonshotai/kimi-k2.6", "IDEA_NARROW_MODEL": "qwen/qwen3.7-plus",
            "IDEA_SYNTHESIS_MODEL": "openai/gpt-5.1", "REPORT_SYNTHESIS_MODEL": "openai/gpt-5.1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _fail("deepseek/deepseek-v4-flash", 15)
    _fail("deepseek/deepseek-v4-pro", 15)
    _history(_iso, [{"env_name": e, "status": "approved", "old_model": prev[e],
                     "new_model": env[e]} for e in env])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    resets: list[str] = []
    orig_reset = health.reset
    monkeypatch.setattr(health, "reset", lambda m: (resets.append(m), orig_reset(m)))
    actions = rollback.check_and_rollback()
    assert sorted(spy.calls) == sorted(prev.items())
    assert all(a["applied"] for a in actions) and len(actions) == 4
    assert sorted(resets) == ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"]
    assert health.recent_stats("deepseek/deepseek-v4-flash")["samples"] == 0


def test_shared_model_history_missing_does_not_hide_other_env(monkeypatch, _iso):
    """앞 env가 'history 없음'으로 reset해도 같은 모델의 뒤 env는 같은 회차에 롤백."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")   # history 없음
    monkeypatch.setenv("IDEA_NARROW_MODEL", "deepseek/deepseek-v4-flash")  # history 있음
    _fail("deepseek/deepseek-v4-flash", 15)
    _history(_iso, [{"env_name": "IDEA_NARROW_MODEL", "status": "approved",
                     "old_model": "qwen/qwen3.7-plus",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    actions = rollback.check_and_rollback()
    assert spy.calls == [("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus")]
    assert [a["note"] for a in actions if a["env_name"] == "OPENROUTER_MODEL"] == ["history 없음"]
    assert health.recent_stats("deepseek/deepseek-v4-flash")["samples"] == 0


def test_shared_model_upsert_failure_keeps_evidence(monkeypatch, _iso):
    """공유 모델의 env 하나라도 upsert 실패면 그 모델 샘플 유지 — 다음 시간 재시도 근거."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_NARROW_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash", 15)
    _history(_iso, [
        {"env_name": "OPENROUTER_MODEL", "status": "approved",
         "old_model": "moonshotai/kimi-k2.6", "new_model": "deepseek/deepseek-v4-flash"},
        {"env_name": "IDEA_NARROW_MODEL", "status": "approved",
         "old_model": "qwen/qwen3.7-plus", "new_model": "deepseek/deepseek-v4-flash"},
    ])
    calls: list = []

    def _upsert(name, value, service_ids=None):
        calls.append(name)
        return (name == "OPENROUTER_MODEL"), ("✅ ok" if name == "OPENROUTER_MODEL" else "❌ HTTP 500")

    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _upsert)
    actions = rollback.check_and_rollback()
    assert calls == ["OPENROUTER_MODEL", "IDEA_NARROW_MODEL"]
    assert [a["applied"] for a in actions] == [True, False]
    assert health.recent_stats("deepseek/deepseek-v4-flash")["samples"] == 15


def test_hold_when_previous_model_failing(monkeypatch, _iso):
    """이전 모델도 지금 실패 중(샘플 ≥3)이면 롤백 보류 — 실패 중인 모델로 옮기지 않음."""
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro")
    _fail("deepseek/deepseek-v4-pro", 15)
    _fail("deepseek/deepseek-v3.2", health.SIGNAL_MIN_SAMPLES)  # 같은 프로바이더 → systemic 아님
    _history(_iso, [{"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
                     "old_model": "deepseek/deepseek-v3.2",
                     "new_model": "deepseek/deepseek-v4-pro"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)

    def _no_probe(*a, **kw):
        raise AssertionError("보류만 있으면 토큰 probe 불필요")

    monkeypatch.setattr(railway_env, "probe", _no_probe)
    actions = rollback.check_and_rollback()
    assert len(actions) == 1
    a = actions[0]
    assert a["note"] == "held" and a["applied"] is False
    assert a["to"] == "deepseek/deepseek-v3.2"
    assert health.recent_stats("deepseek/deepseek-v4-pro")["samples"] == 15, "증거 유지"
    msg = rollback.format_rollback_message(actions)
    assert "롤백 보류" in msg and "이전 모델도 실패" in msg


@pytest.mark.parametrize("prev_ok, prev_fail", [(5, 0), (0, 2), (2, 1)])
def test_no_hold_when_previous_healthy_or_thin(monkeypatch, _iso, prev_ok, prev_fail):
    """이전 모델이 정상이거나 실패 *건수*가 하한(3) 미만이면 정상 롤백 — 2/1(1/3 = 33%)
    같은 우발 실패 1건으로 필요한 롤백을 보류하지 않음."""
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro")
    _fail("deepseek/deepseek-v4-pro", 15)
    for _ in range(prev_ok):
        health.record_call("deepseek/deepseek-v3.2", True, 100)
    _fail("deepseek/deepseek-v3.2", prev_fail)
    _history(_iso, [{"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
                     "old_model": "deepseek/deepseek-v3.2",
                     "new_model": "deepseek/deepseek-v4-pro"}])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    rollback.check_and_rollback()
    assert spy.calls == [("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v3.2")]


def test_same_model_two_envs_is_not_systemic(monkeypatch, _iso):
    """같은 모델이 env 2개에 쓰이면 distinct 1개 — 모델 문제로 정상 롤백."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_NARROW_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash")
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    actions = rollback.check_and_rollback()
    assert ("OPENROUTER_MODEL", "moonshotai/kimi-k2.6") in spy.calls
    assert all(a.get("note") != "systemic" for a in actions)


def test_systemic_needs_min_samples(monkeypatch, _iso):
    monkeypatch.setenv("OPENROUTER_MODEL", "a/one")
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "b/two")
    _fail("a/one", health.ROLLBACK_MIN_SAMPLES)
    _fail("b/two", health.ROLLBACK_MIN_SAMPLES - 1)  # 샘플 부족 — 집계 제외
    assert health.failing_models(["a/one", "b/two", "a/one"]) == ["a/one"]


def test_recent_stats_non_utc_timezone(monkeypatch, _iso):
    """bucket은 UTC — 컨테이너 TZ가 KST여도 방금 기록이 1h 창에 잡혀야 함."""
    import os as _os
    import time as _t
    if not hasattr(_t, "tzset"):
        pytest.skip("tzset 미지원 플랫폼")
    orig = _os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Seoul")
    _t.tzset()
    try:
        _fail("a/one", 3)
        assert health.recent_stats("a/one")["samples"] == 3
    finally:
        if orig is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", orig)
        _t.tzset()


def test_no_token_manual_instruction(monkeypatch, _iso):
    monkeypatch.setenv("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus")
    _fail("qwen/qwen3.7-plus")
    _history(_iso, [{"env_name": "IDEA_NARROW_MODEL", "status": "approved",
                     "old_model": "deepseek/deepseek-v4-flash",
                     "new_model": "qwen/qwen3.7-plus"}])
    monkeypatch.setattr(railway_env, "available", lambda: False)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    actions = rollback.check_and_rollback()
    assert len(actions) == 1
    a = actions[0]
    assert a["applied"] is False and a["note"] == "manual"
    assert a["to"] == "deepseek/deepseek-v4-flash"
    assert "IDEA_NARROW_MODEL" in a["manual"] and "deepseek/deepseek-v4-flash" in a["manual"]
    msg = rollback.format_rollback_message(actions)
    assert "Variables" in msg and "deepseek/deepseek-v4-flash" in msg
    assert "롤백 불가" not in msg


def test_revoked_token_manual_instruction(monkeypatch, _iso):
    """토큰은 있으나 폐기(probe 'invalid' — 사고 상태) → upsert 시도 없이 수동 안내.
    probe는 회차당 1번 (env 2개여도)."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_NARROW_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash", 15)
    _history(_iso, [
        {"env_name": "OPENROUTER_MODEL", "status": "approved",
         "old_model": "moonshotai/kimi-k2.6", "new_model": "deepseek/deepseek-v4-flash"},
        {"env_name": "IDEA_NARROW_MODEL", "status": "approved",
         "old_model": "qwen/qwen3.7-plus", "new_model": "deepseek/deepseek-v4-flash"},
    ])
    probes: list = []
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "probe",
                        lambda *a, **kw: probes.append(1) or railway_env.PROBE_INVALID)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    actions = rollback.check_and_rollback()
    assert [a["note"] for a in actions] == ["manual", "manual"]
    assert probes == [1]
    msg = rollback.format_rollback_message(actions)
    assert "수동 롤백 필요" in msg and "미설정/무효" in msg
    assert health.recent_stats("deepseek/deepseek-v4-flash")["samples"] == 0


def test_no_token_probe_when_nothing_to_roll_back(monkeypatch, _iso):
    """평시(롤백 대상 없음)엔 매시간 Railway probe 호출 안 함."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    for _ in range(15):
        health.record_call("deepseek/deepseek-v4-flash", True, 100)

    def _no_probe(*a, **kw):
        raise AssertionError("probe 금지")

    monkeypatch.setattr(railway_env, "available", _no_probe)
    monkeypatch.setattr(railway_env, "probe", _no_probe)
    monkeypatch.setattr(railway_env, "token_valid", _no_probe)
    assert rollback.check_and_rollback() == []


def test_upsert_failure_includes_manual_hint(monkeypatch, _iso):
    monkeypatch.setenv("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus")
    _fail("qwen/qwen3.7-plus")
    _history(_iso, [{"env_name": "IDEA_NARROW_MODEL", "status": "approved",
                     "old_model": "deepseek/deepseek-v4-flash",
                     "new_model": "qwen/qwen3.7-plus"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable",
                        _UpsertSpy(ok=False, msg="❌ 전부 실패: sid=abc: HTTP 401"))
    actions = rollback.check_and_rollback()
    msg = rollback.format_rollback_message(actions)
    assert "롤백 불가" in msg and "Variables" in msg


def test_excluded_provider_not_restored(monkeypatch, _iso):
    monkeypatch.setenv("IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro")
    _fail("deepseek/deepseek-v4-pro")
    _history(_iso, [{"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
                     "old_model": "anthropic/claude-sonnet-4.6",
                     "new_model": "deepseek/deepseek-v4-pro"}])
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    assert rollback._previous_model_for_env(
        "IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro") is None
    actions = rollback.check_and_rollback()
    assert actions[0]["to"] is None and actions[0]["applied"] is False


def test_excluded_provider_skipped_for_older_candidate(_iso):
    _history(_iso, [
        {"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
         "old_model": "openai/gpt-5.1", "new_model": "deepseek/deepseek-v4-pro"},
        {"env_name": "IDEA_SYNTHESIS_MODEL", "status": "auto_applied",
         "old_model": "anthropic/claude-sonnet-4.6", "new_model": "deepseek/deepseek-v4-pro"},
    ])
    assert rollback._previous_model_for_env(
        "IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro") == "openai/gpt-5.1"


def test_exclusion_disabled_allows_restore(monkeypatch, _iso):
    monkeypatch.setenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", "")
    _history(_iso, [{"env_name": "IDEA_SYNTHESIS_MODEL", "status": "approved",
                     "old_model": "anthropic/claude-sonnet-4.6",
                     "new_model": "deepseek/deepseek-v4-pro"}])
    assert rollback._previous_model_for_env(
        "IDEA_SYNTHESIS_MODEL", "deepseek/deepseek-v4-pro") == "anthropic/claude-sonnet-4.6"


# ---------------------------------------------------------------- systemic 실패 건수 하한


def _noise() -> None:
    """평시 저볼륨 모델의 우발 실패 — gpt-5.2 earnings 1/3 timeout, qwen chain 2차 1/4 빈 응답."""
    for ok in (True, True, False):
        health.record_call("openai/gpt-5.2", ok, 90000)
    for ok in (True, True, True, False):
        health.record_call("qwen/qwen3.7-plus", ok, 5000)


def test_is_failing_min_fails_floor(_iso):
    _noise()
    assert health.is_failing("openai/gpt-5.2", min_samples=3) is True  # 샘플 하한만 — 1/3 = 33%
    assert health.is_failing("openai/gpt-5.2", min_samples=3, min_fails=3) is False
    assert health.recent_stats("openai/gpt-5.2")["fails"] == 1
    _fail("a/one", 3)
    assert health.failing_models(None, min_samples=3, min_fails=3) == ["a/one"]


def test_systemic_noise_nothing_needed_no_alert(monkeypatch, _iso):
    """평시 (sim_systemic_fp normal): flash 300/3 정상 + 두 프로바이더 우발 실패 1건씩 →
    롤백 대상 없음 → systemic 경보 없음 (예전엔 '플랫폼/키 장애 의심' 오경보)."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("EARNINGS_SYNTHESIS_MODEL", "openai/gpt-5.2")
    for _ in range(300):
        health.record_call("deepseek/deepseek-v4-flash", True, 2000)
    _fail("deepseek/deepseek-v4-flash", 3)
    _noise()
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    assert rollback.check_and_rollback() == []
    assert not (_iso / "systemic_alert.json").exists()


def test_systemic_skipped_when_no_env_needs_rollback(monkeypatch, _iso):
    """두 프로바이더가 실패 하한을 넘어도 롤백할 env가 없으면 systemic 판정·알림 생략."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    for _ in range(50):
        health.record_call("deepseek/deepseek-v4-flash", True, 2000)
    _fail("openai/gpt-5.2", 5)
    _fail("qwen/qwen3.7-plus", 5)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    assert rollback.check_and_rollback() == []
    assert not (_iso / "systemic_alert.json").exists()


def test_single_provider_outage_with_noise_rolls_back(monkeypatch, _iso):
    """deepseek 단독 장애(40/40) + 평시 우발 실패 1건씩 → systemic 아님, env별 롤백 진행
    (예전엔 systemic 보류로 Layer D가 롤백 못 함 — HEAD 대비 회귀)."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_NARROW_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("EARNINGS_SYNTHESIS_MODEL", "openai/gpt-5.2")
    _fail("deepseek/deepseek-v4-flash", 40)
    _noise()
    _history(_iso, [{"env_name": "OPENROUTER_MODEL", "status": "approved",
                     "old_model": "moonshotai/kimi-k2.6",
                     "new_model": "deepseek/deepseek-v4-flash"}])
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    actions = rollback.check_and_rollback()
    assert spy.calls == [("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")]
    assert all(a.get("note") != "systemic" for a in actions)
    assert not (_iso / "systemic_alert.json").exists()


# ---------------------------------------------------------------- Railway 일시 장애 (probe 'unknown')


def test_railway_transient_keeps_evidence_and_retries(monkeypatch, _iso):
    """probe 'unknown'(네트워크·5xx) → '무효' 안내 없이 증거 유지, 다음 시간 정상 upsert."""
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("IDEA_NARROW_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash", 15)
    _history(_iso, [
        {"env_name": "OPENROUTER_MODEL", "status": "approved",
         "old_model": "moonshotai/kimi-k2.6", "new_model": "deepseek/deepseek-v4-flash"},
        {"env_name": "IDEA_NARROW_MODEL", "status": "approved",
         "old_model": "qwen/qwen3.7-plus", "new_model": "deepseek/deepseek-v4-flash"},
    ])
    state = [railway_env.PROBE_UNKNOWN]
    probes: list = []
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "probe", lambda *a, **kw: probes.append(1) or state[0])
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    actions = rollback.check_and_rollback()
    assert probes == [1], "probe는 회차당 1번"
    assert [a["applied"] for a in actions] == [False, False]
    assert all("일시 장애" in a["note"] and a["manual"] for a in actions)
    assert all(a["note"] != "manual" for a in actions)
    msg = rollback.format_rollback_message(actions)
    assert "일시 장애" in msg and "Variables" in msg
    assert "무효" not in msg and "미설정" not in msg
    assert health.recent_stats("deepseek/deepseek-v4-flash")["samples"] == 15, "증거 유지"
    # 다음 시간: Railway 복구 → 같은 증거로 정상 롤백
    state[0] = railway_env.PROBE_OK
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    actions = rollback.check_and_rollback()
    assert sorted(spy.calls) == [("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus"),
                                 ("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")]
    assert all(a["applied"] for a in actions)


def test_apply_change_railway_transient_is_failure(monkeypatch):
    """probe 'unknown' → manual이 아니라 railway_failed (승인 항목 소비·'approved' 기록 금지)."""
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "probe", lambda *a, **kw: railway_env.PROBE_UNKNOWN)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    status, msg = asyncio.run(handler._apply_change("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus"))
    assert status == "railway_failed"
    assert "Railway API 일시 장애" in msg and "재시도 또는 수동 적용" in msg
    assert "IDEA_NARROW_MODEL" in msg and "qwen/qwen3.7-plus" in msg
    assert "무효" not in msg


def test_approve_cmd_railway_transient_stays_pending(monkeypatch, _iso):
    _admin_env(monkeypatch, allowed="42")
    approval_store.save_pending([{
        "id": 1, "env_name": "IDEA_NARROW_MODEL", "old_model": "a/old",
        "new_model": "qwen/qwen3.7-plus", "status": "pending",
    }])
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "probe", lambda *a, **kw: railway_env.PROBE_UNKNOWN)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    msg = _Msg()
    asyncio.run(handler.model_approve_cmd(_update(42, msg), SimpleNamespace(args=["1"])))
    joined = "\n".join(msg.replies)
    assert "일시 장애" in joined and "재승인" in msg.replies[-1]
    assert approval_store.load_pending()[0]["status"] == "pending"
    assert not (_iso / "history.json").exists()


# ---------------------------------------------------------------- railway_env


class _Resp:
    def __init__(self, status: int, data) -> None:
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


def _fake_httpx(responses: list[_Resp], posts: list[dict]):
    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            posts.append(json)
            return responses[min(len(posts) - 1, len(responses) - 1)]

    return SimpleNamespace(Client=_Client)


def _railway_env(monkeypatch, sids="s1,s2,s3", token="tok-fake-123"):
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", token)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid")
    monkeypatch.setenv("RAILWAY_SERVICE_IDS", sids)


def test_available(monkeypatch):
    for n in ("RAILWAY_PROJECT_ACCESS_TOKEN", "RAILWAY_ACCESS_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "s1")
    monkeypatch.delenv("RAILWAY_SERVICE_IDS", raising=False)
    assert railway_env.available() is False
    monkeypatch.setenv("RAILWAY_ACCESS_TOKEN", "tok-fake")
    assert railway_env.available() is True


def test_upsert_service_ids_subset(monkeypatch):
    _railway_env(monkeypatch)
    posts: list[dict] = []
    monkeypatch.setattr(railway_env, "httpx",
                        _fake_httpx([_Resp(200, {"data": {"variableUpsert": True}})], posts))
    ok, _ = railway_env.upsert_variable("X", "v", service_ids=["s2"])
    assert ok
    assert [p["variables"]["input"]["serviceId"] for p in posts] == ["s2"]
    posts.clear()
    ok, _ = railway_env.upsert_variable("X", "v")  # None = 전 서비스 (기존 동작)
    assert [p["variables"]["input"]["serviceId"] for p in posts] == ["s1", "s2", "s3"]


def test_delete_service_ids_subset(monkeypatch):
    _railway_env(monkeypatch)
    posts: list[dict] = []
    monkeypatch.setattr(railway_env, "httpx",
                        _fake_httpx([_Resp(200, {"data": {"variableDelete": True}})], posts))
    ok, msg = railway_env.delete_variable("X", service_ids=["s3", "s1"])
    assert ok and "2개" in msg
    assert [p["variables"]["input"]["serviceId"] for p in posts] == ["s3", "s1"]


def test_upsert_error_never_echoes_value(monkeypatch):
    _railway_env(monkeypatch, sids="s1")
    secret = "1SESSIONSTRINGfake0123456789abcdef"
    echo = {"errors": [{"message": f"invalid value {secret} for TG_SESSION_STRING"}],
            "data": None, "input": {"value": secret}}
    posts: list[dict] = []
    monkeypatch.setattr(railway_env, "httpx", _fake_httpx([_Resp(400, echo)], posts))
    ok, msg = railway_env.upsert_variable("TG_SESSION_STRING", secret)
    assert not ok
    assert secret not in msg and "tok-fake-123" not in msg
    assert "HTTP 400" in msg


def test_upsert_exception_never_echoes_value(monkeypatch):
    _railway_env(monkeypatch, sids="s1")
    secret = "supersecretvalue-fake-xyz"

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError(f"bad payload {secret}")

    monkeypatch.setattr(railway_env, "httpx", SimpleNamespace(Client=_Boom))
    ok, msg = railway_env.upsert_variable("SOME_KEY", secret)
    assert not ok and secret not in msg


# ---------------------------------------------------------------- fetcher


def test_fetch_models_sends_no_authorization(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fakefakefake")
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "mgmt-fake")
    seen: list[dict] = []

    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            seen.append({"url": url, **kw})
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": [{"id": "a/b", "pricing": {"prompt": "0.000001",
                                                                 "completion": "0.000002"}}]},
            )

    monkeypatch.setattr(fetcher, "httpx", SimpleNamespace(Client=_Client))
    out = fetcher.fetch_models()
    assert "a/b" in out
    assert seen and not (seen[0].get("headers") or {}).get("Authorization")
    assert "headers" not in seen[0] or not seen[0]["headers"]


# ---------------------------------------------------------------- canary


def test_canary_breaker_open_no_network(monkeypatch):
    monkeypatch.setattr(canary, "auth_breaker_open", lambda: True)

    def _no_call(*a, **kw):
        raise AssertionError("breaker 열림 — LLM 호출 금지")

    monkeypatch.setattr(canary, "chat_with_retry", _no_call)
    monkeypatch.setattr(canary, "get_client", _no_call)
    monkeypatch.delenv("MODEL_ROUTER_SKIP_CANARY", raising=False)
    r = canary.run_canary("OPENROUTER_MODEL", "x/y")
    assert r["passed"] is False and r["auth_error"] is True


def test_canary_auth_error_flagged(monkeypatch):
    calls = []

    def _auth_fail(*a, **kw):
        calls.append(1)
        raise canary.OpenRouterAuthError("401")

    monkeypatch.setattr(canary, "auth_breaker_open", lambda: False)
    monkeypatch.setattr(canary, "chat_with_retry", _auth_fail)
    monkeypatch.setattr(canary, "get_client", lambda: object())
    monkeypatch.delenv("MODEL_ROUTER_SKIP_CANARY", raising=False)
    r = canary.run_canary("OPENROUTER_MODEL", "x/y")
    assert r["passed"] is False and r["auth_error"] is True
    assert len(calls) == 1


# ---------------------------------------------------------------- handler


def test_apply_change_manual_without_railway(monkeypatch):
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: False)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    status, msg = asyncio.run(handler._apply_change("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus"))
    assert status == "manual"
    assert "IDEA_NARROW_MODEL" in msg and "qwen/qwen3.7-plus" in msg
    assert "대시보드 Variables에서 수정" in msg


def test_apply_change_manual_with_revoked_token(monkeypatch):
    """토큰은 있으나 폐기(사고 상태) → railway_failed가 아니라 manual (history 기록 경로)."""
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "probe", lambda *a, **kw: railway_env.PROBE_INVALID)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    status, msg = asyncio.run(handler._apply_change("IDEA_NARROW_MODEL", "qwen/qwen3.7-plus"))
    assert status == "manual"
    assert "IDEA_NARROW_MODEL" in msg and "qwen/qwen3.7-plus" in msg


def test_apply_change_canary_auth_is_not_quality(monkeypatch):
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": False, "failures": ["[0] 401"],
                                      "sentinels": 1, "auth_error": True})
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    status, msg = asyncio.run(handler._apply_change("OPENROUTER_MODEL", "x/y"))
    assert status == "key_failed"
    assert "키" in msg and "품질" not in msg


def test_apply_change_quality_failure_unchanged(monkeypatch):
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": False, "failures": ["[0] validator: x"],
                                      "sentinels": 1})
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    status, msg = asyncio.run(handler._apply_change("OPENROUTER_MODEL", "x/y"))
    assert status == "canary_failed" and "Canary 실패" in msg


def test_apply_change_applied(monkeypatch):
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: True)
    spy = _UpsertSpy()
    monkeypatch.setattr(railway_env, "upsert_variable", spy)
    status, _ = asyncio.run(handler._apply_change("OPENROUTER_MODEL", "x/y"))
    assert status == "applied" and spy.calls == [("OPENROUTER_MODEL", "x/y")]


def _admin_env(monkeypatch, report_allowed="", report_chat="", allowed=""):
    for n, v in (("REPORT_ALLOWED_CHAT_IDS", report_allowed), ("REPORT_CHAT_ID", report_chat),
                 ("ALLOWED_CHAT_IDS", allowed)):
        if v:
            monkeypatch.setenv(n, v)
        else:
            monkeypatch.delenv(n, raising=False)


def test_is_admin_wildcard_not_for_control_plane(monkeypatch):
    _admin_env(monkeypatch, allowed="*")
    assert handler._is_admin(999) is True  # 읽기 명령(/model_status)은 기존 동작
    assert handler._is_admin(999, control_plane=True) is False


def test_is_admin_control_plane_explicit_ids(monkeypatch):
    _admin_env(monkeypatch, report_allowed="*", allowed="*,-100123,456")
    assert handler._is_admin(-100123, control_plane=True) is True
    assert handler._is_admin(456, control_plane=True) is True
    assert handler._is_admin(999, control_plane=True) is False
    _admin_env(monkeypatch, allowed="111,222")
    assert handler._is_admin(111, control_plane=True) is True
    assert handler._is_admin(333, control_plane=True) is False


class _Msg:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _update(chat_id: int, msg: _Msg):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=msg)


def test_approve_cmd_wildcard_denied(monkeypatch):
    _admin_env(monkeypatch, allowed="*")
    msg = _Msg()
    asyncio.run(handler.model_approve_cmd(_update(5, msg), SimpleNamespace(args=["all"])))
    assert msg.replies and "권한 없음" in msg.replies[0]


def test_approve_cmd_manual_flow(monkeypatch, _iso):
    _admin_env(monkeypatch, allowed="42")
    approval_store.save_pending([{
        "id": 1, "env_name": "IDEA_NARROW_MODEL", "old_model": "a/old",
        "new_model": "qwen/qwen3.7-plus", "status": "pending",
    }])
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": True, "failures": [], "sentinels": 1})
    monkeypatch.setattr(railway_env, "available", lambda: False)
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    msg = _Msg()
    asyncio.run(handler.model_approve_cmd(_update(42, msg), SimpleNamespace(args=["1"])))
    joined = "\n".join(msg.replies)
    assert "수동" in joined and "qwen/qwen3.7-plus" in joined
    assert "전부 실패" not in joined
    assert approval_store.load_pending()[0]["status"] == "approved"


def test_approve_cmd_auth_failure_hint(monkeypatch, _iso):
    _admin_env(monkeypatch, allowed="42")
    approval_store.save_pending([{
        "id": 1, "env_name": "OPENROUTER_MODEL", "old_model": "a/old",
        "new_model": "b/new", "status": "pending",
    }])
    monkeypatch.setattr(canary, "run_canary",
                        lambda e, m: {"passed": False, "failures": ["[0] 401"],
                                      "sentinels": 1, "auth_error": True})
    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    msg = _Msg()
    asyncio.run(handler.model_approve_cmd(_update(42, msg), SimpleNamespace(args=["1"])))
    final = msg.replies[-1]
    assert "전부 실패" in final and "키" in final
    assert "품질 미달" not in final
    assert approval_store.load_pending()[0]["status"] == "pending"


def test_eval_message_manual_section():
    rec = {"env_name": "OPENROUTER_MODEL", "old_model": "a/old", "new_model": "b/new",
           "reason": "x"}
    out = handler._format_eval_message([rec], [], [], [rec])
    assert "수동 적용 필요" in out and "b/new" in out
    assert "변경 권고 없음" not in out


def test_model_health_job_silent_on_breaker(monkeypatch, _iso):
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    _fail("deepseek/deepseek-v4-flash")
    monkeypatch.setenv("REPORT_CHAT_ID", "42")
    _sm().trip_auth_breaker()
    sent: list = []

    class _Bot:
        async def send_message(self, *a, **kw):
            sent.append(a)

    asyncio.run(handler.model_health_job(_Bot()))
    assert sent == []


def test_make_auth_alert_hook_schedules_on_loop(monkeypatch):
    _admin_env(monkeypatch)
    monkeypatch.setenv("REPORT_CHAT_ID", "42")
    sent: list[tuple] = []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    loop = asyncio.new_event_loop()
    try:
        hook = handler.make_auth_alert_hook(_Bot(), loop)
        assert hook is not None
        hook("🔑 401")  # 다른 스레드(루프 미실행)에서 호출돼도 예약만 하고 즉시 반환
        loop.run_until_complete(asyncio.sleep(0.01))
    finally:
        loop.close()
    assert sent == [("42", "🔑 401")]


def test_make_auth_alert_hook_none_without_target(monkeypatch):
    _admin_env(monkeypatch)
    monkeypatch.delenv("MODEL_ROUTER_CHAT_ID", raising=False)
    assert handler.make_auth_alert_hook(object(), None) is None



# ---------------------------------------------------------------- 401 알림 미전달 → 재알림


def _hook_env(monkeypatch):
    _admin_env(monkeypatch)
    monkeypatch.delenv("MODEL_ROUTER_CHAT_ID", raising=False)
    monkeypatch.setenv("REPORT_CHAT_ID", "42")
    sm = _sm()
    clock = [10_000.0]
    monkeypatch.setattr(sm, "_clock", lambda: clock[0])
    monkeypatch.setattr(sm, "_auth_alert_hook", None)  # teardown 때 원래 hook 복원
    return sm, clock


async def _wait(cond, n: int = 200) -> bool:
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


def test_auth_alert_send_failure_realerts_next_trip(monkeypatch):
    """첫 발송이 비동기 실패(TimedOut) → 기록 해제 → cooldown 뒤 재차단(6h 미만)에 재발송.
    예전엔 _auth_alerted_at이 남아 6h 동안 관리자가 키 장애를 모름 (sim_alert_fail)."""
    sm, clock = _hook_env(monkeypatch)
    attempts: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            attempts.append(text)
            if len(attempts) == 1:
                raise TimeoutError("Timed out")

    async def scenario():
        sm.set_auth_alert_hook(handler.make_auth_alert_hook(_Bot(), asyncio.get_running_loop()))
        await asyncio.to_thread(sm.trip_auth_breaker)
        assert await _wait(lambda: len(attempts) == 1 and sm._auth_alerted_at is None)
        clock[0] += sm._auth_cooldown_s() + 1  # half-open probe 401 → 재차단
        await asyncio.to_thread(sm.trip_auth_breaker)
        assert await _wait(lambda: len(attempts) == 2)
        await asyncio.sleep(0.02)  # done-callback 실행 여유
        # 두 번째는 전달 성공 → 기록 유지 (다음 재차단은 6h dedup)
        assert sm._auth_alerted_at is not None
        clock[0] += sm._auth_cooldown_s() + 1
        await asyncio.to_thread(sm.trip_auth_breaker)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert len(attempts) == 2


def test_auth_alert_success_keeps_dedup(monkeypatch):
    sm, clock = _hook_env(monkeypatch)
    sent: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    async def scenario():
        sm.set_auth_alert_hook(handler.make_auth_alert_hook(_Bot(), asyncio.get_running_loop()))
        await asyncio.to_thread(sm.trip_auth_breaker)
        assert await _wait(lambda: len(sent) == 1)
        await asyncio.sleep(0.02)
        clock[0] += sm._auth_cooldown_s() + 1
        await asyncio.to_thread(sm.trip_auth_breaker)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert len(sent) == 1


@pytest.mark.parametrize("branch", ["coroutine", "loop_closed"])
def test_auth_alert_early_return_clears_record(monkeypatch, branch):
    """hook의 조기 반환 2곳(코루틴 생성 실패·루프 전달 실패)도 기록 해제 → 다음 차단 때 재시도."""
    sm, clock = _hook_env(monkeypatch)
    calls: list[str] = []

    class _Bot:
        def send_message(self, chat_id, text, **kw):
            calls.append(text)
            if branch == "coroutine":
                raise RuntimeError("bot closed")

            async def _send():
                return None
            return _send()

    loop = asyncio.new_event_loop()
    loop.close()  # loop_closed 분기: run_coroutine_threadsafe가 RuntimeError
    sm.set_auth_alert_hook(handler.make_auth_alert_hook(_Bot(), loop))
    sm.trip_auth_breaker()
    assert sm._auth_alerted_at is None
    clock[0] += sm._auth_cooldown_s() + 1
    sm.trip_auth_breaker()
    assert len(calls) == 2
