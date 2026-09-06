"""railway_env._config — 토큰 alias·SERVICE_ID 폴백 검증 (2026-09-06 실측 사고 재발 방지)."""
from __future__ import annotations

from src.model_router import railway_env as re_


def _base(monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid")
    monkeypatch.delenv("RAILWAY_SERVICE_IDS", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("RAILWAY_ACCESS_TOKEN", raising=False)


def test_token_alias_accepted(monkeypatch):
    """사용자가 RAILWAY_ACCESS_TOKEN 이름으로 넣어도 동작."""
    _base(monkeypatch)
    monkeypatch.setenv("RAILWAY_ACCESS_TOKEN", "tok-alias")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "sid1")
    cfg = re_._config()
    assert cfg and cfg["token"] == "tok-alias"
    assert cfg["service_ids"] == ["sid1"]


def test_canonical_token_wins(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("RAILWAY_PROJECT_ACCESS_TOKEN", "tok-canon")
    monkeypatch.setenv("RAILWAY_ACCESS_TOKEN", "tok-alias")
    monkeypatch.setenv("RAILWAY_SERVICE_IDS", "a, b,")
    cfg = re_._config()
    assert cfg["token"] == "tok-canon"
    assert cfg["service_ids"] == ["a", "b"]


def test_missing_token_returns_none(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "sid1")
    assert re_._config() is None
