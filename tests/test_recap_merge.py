"""recap_bot 종목봇 합류 모드 — allowlist·cron chat id 폴백 검증."""
from __future__ import annotations

from src import recap_bot


def _clear(monkeypatch):
    for k in ("RECAP_ALLOWED_CHAT_IDS", "ALLOWED_CHAT_IDS", "RECAP_CHAT_ID", "REPORT_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)


def test_allowed_env_prefers_dedicated(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("RECAP_ALLOWED_CHAT_IDS", "1")
    assert recap_bot._allowed_env() == "RECAP_ALLOWED_CHAT_IDS"


def test_allowed_env_falls_back_to_company(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "*")
    assert recap_bot._allowed_env() == "ALLOWED_CHAT_IDS"


def test_cron_chat_id_chain(monkeypatch):
    _clear(monkeypatch)
    assert recap_bot.cron_chat_id() is None
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "*, -100123 ,abc")
    assert recap_bot.cron_chat_id() == "-100123"
    monkeypatch.setenv("REPORT_CHAT_ID", "555")
    assert recap_bot.cron_chat_id() == "555"
    monkeypatch.setenv("RECAP_CHAT_ID", "777")
    assert recap_bot.cron_chat_id() == "777"


def test_register_handlers_adds_only_recap_commands():
    added = []

    class FakeApp:
        def add_handler(self, h):
            added.append(h)

    recap_bot.register_handlers(FakeApp())
    # conftest가 telegram을 stub 처리 → 핸들러 수만 검증 (help/자유텍스트 미포함 = 3개)
    assert len(added) == 3
