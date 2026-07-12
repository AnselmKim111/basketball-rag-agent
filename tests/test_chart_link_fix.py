"""차트 링크 누락 수정 + 최신성 가드 회귀 테스트.

사고: rs_leaders를 차트 루프에서 제외 → RS 리더 전용 종목이 메시지에서 흰색
(링크 없음) + YTD N/A. 수정: 표시 카테고리 화이트리스트(DISPLAY_CATEGORIES) —
표시 종목은 전부 링크/메타 생성, 미표시 신호(추세전환 ~94건/일)는 게시 중단.
"""
from __future__ import annotations

import pytest

import src.screener_bot as kr_bot
import src.us_screener_bot as us_bot
from src.screener_common import BADGE, fmt_won, render_caption


# ------------------------------------------------------------------
# 화이트리스트 계약 — formatter 표시 섹션과 1:1
# ------------------------------------------------------------------
def test_display_categories_include_rs_leaders():
    # rs_leaders 제외가 사고 원인 — 다시 빠지면 안 됨
    assert "rs_leaders" in kr_bot.DISPLAY_CATEGORIES
    assert "rs_leaders" in us_bot.DISPLAY_CATEGORIES


def test_display_categories_exclude_undisplayed_signals():
    # 미표시 신호(추세전환·거래량)는 채널 flood — 포함되면 안 됨
    for cat in ("volume_breakout", "ma_golden_cross", "rsi_oversold_recovery",
                "base_hold_after_breakout", "downtrend_exit"):
        assert cat not in kr_bot.DISPLAY_CATEGORIES
        assert cat not in us_bot.DISPLAY_CATEGORIES


def test_display_categories_kr_us_mirror():
    assert kr_bot.DISPLAY_CATEGORIES == us_bot.DISPLAY_CATEGORIES


def test_badge_has_rs_leaders():
    # 리더 전용 종목 캡션의 badge 줄이 비지 않게
    assert "rs_leaders" in BADGE


# ------------------------------------------------------------------
# 캡션 기준일 + stale 마커
# ------------------------------------------------------------------
def _caption(**over):
    kw = dict(
        market_label="🇰🇷", ticker="005930",
        item={"name": "삼성전자", "chg_pct": 2.1},
        cats=["rs_leaders"], turnover=8.5e11, ytd=15.0, eps=22.5,
        market_cap=4.5e14, fmt_money=fmt_won,
        news_url_template="https://finance.naver.com/item/news.naver?code={ticker}",
        header_use_name=True, show_ticker_in_name=True,
    )
    kw.update(over)
    return render_caption(**kw)


def test_caption_data_date_line():
    cap = _caption(data_date="2026-07-11")
    assert "✝ 기준일 : 2026-07-11" in cap
    assert "💪 상대강도 리더" in cap  # rs_leaders badge


def test_caption_stale_marker_passthrough():
    # 게시 루프가 stale 시 " ⚠️"를 붙여서 전달 — 캡션에 그대로 표기
    cap = _caption(data_date="2026-07-10 ⚠️")
    assert "✝ 기준일 : 2026-07-10 ⚠️" in cap


def test_caption_no_data_date_backward_compat():
    cap = _caption()
    assert "기준일" not in cap  # 기존 캡션 레이아웃 유지
