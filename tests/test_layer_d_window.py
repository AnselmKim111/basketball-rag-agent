"""Layer D health 창 — :17 cron이 직전 시간 bucket을 보는지 (2026-10-22 리뷰 반영).

recent_stats가 bucket *시작* 시각으로 자르면 h1:17 cron의 cutoff(h0:17)보다 h0 bucket
시작(h0:00)이 앞서 통째로 빠짐 → 현재 17분치만 보고, h0:50 실패 15건이 samples=0.
창과 *겹치는* bucket 전부 집계 → 실제 창 60~120분.
대신 창에 이미 끝난 장애 꼬리가 남으므로, 현재 bucket만으로 샘플 ≥ SIGNAL_MIN_SAMPLES이고
정상이면 'recovered' — 회복된 모델 롤백 금지 (2026-10-22 sim_f two: 복구 17분 뒤 불필요 롤백).
가상 시계 (health.time 교체), 네트워크 미사용.
"""
from __future__ import annotations

import json
import time as _rt

import pytest

from src.model_router import approval_store, health, railway_env, rollback
from src.model_router.candidates import TIER_ENV

H0 = 1_789_999_200.0  # UTC 정시 (bucket 경계)
FLASH = "deepseek/deepseek-v4-flash"


class _FakeTime:
    """health 모듈의 time 대체 — time()/gmtime()만 가상 시계, 나머지는 실제 time."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __getattr__(self, name):
        return getattr(_rt, name)

    def time(self) -> float:
        return self.now

    def gmtime(self, t=None):
        return _rt.gmtime(self.now if t is None else t)


@pytest.fixture
def clock(monkeypatch, tmp_path):
    monkeypatch.setattr(health, "_store_dir", lambda: tmp_path)
    monkeypatch.setattr(approval_store, "_store_dir", lambda: tmp_path)
    for envs in TIER_ENV.values():
        for e in envs:
            monkeypatch.delenv(e, raising=False)
    monkeypatch.delenv("MODEL_ROUTER_EXCLUDE_PROVIDERS", raising=False)
    monkeypatch.setattr(rollback, "_auth_recently_failed", lambda: False)
    ft = _FakeTime(H0)
    monkeypatch.setattr(health, "time", ft)
    return ft


def _at(ft: _FakeTime, hour: int, minute: int) -> None:
    ft.now = H0 + hour * 3600 + minute * 60


def _fail(model: str, n: int) -> None:
    for _ in range(n):
        health.record_call(model, False, 1000)


def test_failures_at_h0_50_visible_at_h1_17(clock):
    _at(clock, 0, 50)
    _fail(FLASH, 15)
    _at(clock, 1, 17)  # model_health_hourly cron 분
    st = health.recent_stats(FLASH)
    assert st["samples"] == 15 and st["fail_rate"] == 1.0
    need, reason = health.should_rollback(FLASH, env_name="OPENROUTER_MODEL")
    assert need is True and "fail_rate" in reason


def test_window_combines_previous_and_current_bucket(clock):
    _at(clock, 0, 40)
    _fail(FLASH, 6)
    _at(clock, 1, 5)
    for _ in range(4):
        health.record_call(FLASH, True, 100)
    _at(clock, 1, 17)
    st = health.recent_stats(FLASH)
    assert st["samples"] == 10 and st["fail_rate"] == pytest.approx(0.6)


@pytest.mark.parametrize("now_h, now_m", [(1, 17), (1, 0), (1, 59)])
def test_bucket_entirely_before_window_excluded(clock, now_h, now_m):
    """2시간 전 bucket(h-1)은 창 [now-1h, now]와 안 겹침 → 제외 (경계 h1:00 포함)."""
    _at(clock, -1, 50)
    _fail(FLASH, 15)
    _at(clock, now_h, now_m)
    assert health.recent_stats(FLASH)["samples"] == 0


def test_multi_hour_window(clock):
    """hours=2 — 창과 겹치는 bucket 3개(h-1·h0·h1)를 h1:17에서 집계."""
    for h in (-2, -1, 0, 1):
        _at(clock, h, 5)
        _fail(FLASH, 1)
    _at(clock, 1, 17)
    assert health.recent_stats(FLASH, hours=2)["samples"] == 3


def test_failing_models_floor_uses_window(clock):
    _at(clock, 0, 55)
    _fail("openai/gpt-5.2", health.SIGNAL_MIN_SAMPLES)
    _at(clock, 1, 17)
    assert health.failing_models(None, min_samples=health.SIGNAL_MIN_SAMPLES) == ["openai/gpt-5.2"]


def test_layer_d_rolls_back_on_previous_hour_failures(clock, monkeypatch, tmp_path):
    """end-to-end: h0:50 실패 → h1:17 cron에서 롤백 (예전엔 'insufficient samples (0)')."""
    monkeypatch.setenv("OPENROUTER_MODEL", FLASH)
    (tmp_path / "history.json").write_text(json.dumps([
        {"env_name": "OPENROUTER_MODEL", "status": "approved",
         "old_model": "moonshotai/kimi-k2.6", "new_model": FLASH}]))
    calls: list = []
    monkeypatch.setattr(railway_env, "available", lambda: True)
    monkeypatch.setattr(railway_env, "probe", lambda *a, **kw: railway_env.PROBE_OK)
    monkeypatch.setattr(railway_env, "upsert_variable",
                        lambda n, v, service_ids=None: (calls.append((n, v)), (True, "ok"))[1])
    _at(clock, 0, 50)
    _fail(FLASH, 15)
    _at(clock, 1, 17)
    actions = rollback.check_and_rollback()
    assert calls == [("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")]
    assert actions[0]["applied"] is True


def _ok(model: str, n: int, latency_ms: int = 100) -> None:
    for _ in range(n):
        health.record_call(model, True, latency_ms)


def test_recovered_current_bucket_healthy_no_rollback(clock, monkeypatch, tmp_path):
    """장애가 직전 bucket 중간(h0:30~h0:45)에 끝나고 현재 bucket(h1:00~h1:17)이 정상 →
    창 실패율(15/25=60%)이 임계를 넘어도 롤백 안 함."""
    monkeypatch.setenv("OPENROUTER_MODEL", FLASH)
    (tmp_path / "history.json").write_text(json.dumps([
        {"env_name": "OPENROUTER_MODEL", "status": "approved",
         "old_model": "moonshotai/kimi-k2.6", "new_model": FLASH}]))

    def _no_upsert(*a, **kw):
        raise AssertionError("회복된 모델 롤백 금지")

    monkeypatch.setattr(railway_env, "upsert_variable", _no_upsert)
    _at(clock, 0, 30)
    _fail(FLASH, 15)
    _at(clock, 1, 5)
    _ok(FLASH, 10)
    _at(clock, 1, 17)
    assert health.recent_stats(FLASH)["fail_rate"] > health.ROLLBACK_FAIL_RATE
    assert health.should_rollback(FLASH, env_name="OPENROUTER_MODEL") == (
        False, "recovered (current bucket healthy)")
    assert rollback.check_and_rollback() == []
    assert health.recent_stats(FLASH)["samples"] == 25, "판정만 — 증거 유지"


@pytest.mark.parametrize("cur_ok, cur_fail", [
    (0, 0),   # 현재 bucket 비어 있음 (sim_f_window) → 창 판정 유지
    (2, 0),   # 하한(3) 미만 → 창 판정 유지
    (3, 2),   # 현재 bucket도 실패 중(40%)
])
def test_current_bucket_not_proof_of_recovery(clock, cur_ok, cur_fail):
    _at(clock, 0, 50)
    _fail(FLASH, 15)
    _at(clock, 1, 5)
    _ok(FLASH, cur_ok)
    _fail(FLASH, cur_fail)
    _at(clock, 1, 17)
    need, reason = health.should_rollback(FLASH, env_name="OPENROUTER_MODEL")
    assert need is True and "fail_rate" in reason


def test_recovery_check_includes_latency(clock):
    """지연 롤백: 현재 bucket도 느리면 회복 아님, 빨라졌으면 회복."""
    limit_ms = health.p95_threshold_s("INTENT_ROUTER_MODEL") * 1000
    _at(clock, 0, 40)
    _ok(FLASH, 12, latency_ms=limit_ms + 5000)
    _at(clock, 1, 5)
    _ok(FLASH, 3, latency_ms=limit_ms + 5000)
    _at(clock, 1, 17)
    need, reason = health.should_rollback(FLASH, env_name="INTENT_ROUTER_MODEL")
    assert need is True and "p95" in reason
    _ok(FLASH, 60, latency_ms=100)  # 현재 bucket p95 정상화 (창 p95는 여전히 초과)
    assert health.recent_stats(FLASH)["p95_latency_ms"] > limit_ms
    assert health.should_rollback(FLASH, env_name="INTENT_ROUTER_MODEL") == (
        False, "recovered (current bucket healthy)")

