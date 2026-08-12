"""발송 정리 회귀 — 백필 메시지 무음화·회고 제거·운영노트 footer·force one-shot."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("pandas")

REPO = Path(__file__).resolve().parent.parent


def _empty_results():
    return {k: [] for k in ("near_breakout_52w", "high_all", "high_52w", "high_26w",
                            "vcp_breakout", "volume_surge", "rs_leaders")}


# ------------------------------------------------------------------
# 운영노트 footer
# ------------------------------------------------------------------
def test_ops_notes_footer_kr():
    from src.screener.formatter import format_results
    msg = format_results(_empty_results(), datetime.now(),
                         ops_notes=["백필 543종목", "검증제외 2"])
    assert "<i>〔운영: 백필 543종목 · 검증제외 2〕</i>" in msg
    assert msg.rstrip().endswith("</i>")          # 맨 끝 한 줄


def test_ops_notes_absent_when_empty():
    from src.screener.formatter import format_results
    msg = format_results(_empty_results(), datetime.now(), ops_notes=[])
    assert "〔운영" not in msg
    msg2 = format_results(_empty_results(), datetime.now())
    assert "〔운영" not in msg2


def test_ops_notes_footer_us():
    from src.us_screener.formatter import format_results
    msg = format_results({}, datetime.now(), ops_notes=["백필 12종목"])
    assert "〔운영: 백필 12종목〕" in msg


# ------------------------------------------------------------------
# 회고 미표시 (retro 미전달)
# ------------------------------------------------------------------
def test_no_retro_section_by_default():
    from src.screener.formatter import format_results
    msg = format_results(_empty_results(), datetime.now())
    assert "지난 회고" not in msg


# ------------------------------------------------------------------
# 소스 계약 — cron 백필 무음 + force one-shot + 휴장 무음
# ------------------------------------------------------------------
@pytest.mark.parametrize("bot_file", ["src/screener_bot.py", "src/us_screener_bot.py"])
def test_cron_backfill_silent_and_oneshot(bot_file):
    src = (REPO / bot_file).read_text()
    # cron 경로 백필 진행 push 제거 — "백필 진행" send는 /backfill 명령 1곳만
    assert src.count('f"📥 백필 진행') == 1, f"{bot_file}: cron 백필 진행 push 잔존"
    # force one-shot 토큰 소비
    assert 'meta_get("force_backfill_consumed")' in src, f"{bot_file}: one-shot 게이트 누락"
    assert 'meta_set("force_backfill_consumed", force_token)' in src, f"{bot_file}: 토큰 소비 누락"
    # 휴장 판정 별도 알림 제거 (무음 로그만)
    assert "휴장 판정, 무음 skip" in src, f"{bot_file}: 휴장 무음화 누락"
    assert "휴장 판정 — 발송 skip" not in src, f"{bot_file}: 휴장 alert_admin 잔존"
    # 회고 호출 제거
    assert "retro_mod.signal_returns" not in src, f"{bot_file}: 회고 호출 잔존"
    # 운영노트 전달
    assert "ops_notes=ops_notes" in src, f"{bot_file}: ops_notes 미전달"
