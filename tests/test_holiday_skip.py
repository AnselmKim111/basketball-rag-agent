"""휴장일 발송 스킵 — 요일 필터 + 데이터 기반 휴장 판정 회귀 테스트.

daily_job 전체는 네트워크 재시도 sleep 때문에 단위 테스트 불가 →
(1) meta 라운드트립(스킵 판정의 저장소), (2) 소스 계약(cron 요일 필터·가드 존재)을 고정.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------
# meta 라운드트립 — last_sent_base_date 저장소 (KR/US 양쪽 DB)
# ------------------------------------------------------------------
@pytest.mark.parametrize("mod_name,db_file", [
    ("src.screener.db", "screener.db"),
    ("src.us_screener.db", "us_screener.db"),
])
def test_last_sent_base_date_roundtrip(monkeypatch, tmp_path, mod_name, db_file):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for m in list(sys.modules):
        if m in (mod_name, "src.state_store"):
            del sys.modules[m]
    import importlib
    db = importlib.import_module(mod_name)
    db.DB_PATH = tmp_path / db_file
    db._INITIALIZED = False
    db.ensure_schema()

    assert db.meta_get("last_sent_base_date") is None      # 첫 배포: 키 없음 → 발송 진행
    db.meta_set("last_sent_base_date", "2026-07-23")
    assert db.meta_get("last_sent_base_date") == "2026-07-23"  # 같은 날 재실행 → skip 판정
    db.meta_set("last_sent_base_date", "2026-07-24")
    assert db.meta_get("last_sent_base_date") == "2026-07-24"  # 새 거래일 → 갱신


# ------------------------------------------------------------------
# 소스 계약 — cron 요일 필터 + 휴장 가드/기록이 양 봇에 존재
# ------------------------------------------------------------------
def test_cron_day_of_week_filters():
    src = (REPO / "src/orchestrator.py").read_text()
    assert re.search(r'"day_of_week": "mon-fri", "hour": 16', src), "KR cron mon-fri 필터 누락"
    assert re.search(r'"day_of_week": "tue-sat", "hour": 7', src), "US cron tue-sat 필터 누락"


@pytest.mark.parametrize("bot_file", ["src/screener_bot.py", "src/us_screener_bot.py"])
def test_holiday_guard_and_mark_present(bot_file):
    src = (REPO / bot_file).read_text()
    # 가드: cron 경로에서 last_sent 비교 후 return
    assert 'meta_get("last_sent_base_date")' in src, f"{bot_file}: 휴장 가드 누락"
    assert "휴장 판정, skip" in src
    # 기록: 발송 성공 후 cron 경로에서만 meta_set
    assert 'meta_set("last_sent_base_date", base_date)' in src, f"{bot_file}: 발송 기록 누락"
    assert "override_chat_id is None and sent_count > 0" in src, \
        f"{bot_file}: /screen·self-test가 cron을 막지 않는 조건 누락"
