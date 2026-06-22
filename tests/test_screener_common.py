"""screener_common — KR/US 공통 추출 모듈 단위 테스트.

미러 drift 사고 이력 때문에 공통 모듈의 출력 계약을 고정한다.
"""
from __future__ import annotations

from src.screener_common import (
    BADGE,
    fmt_pct,
    fmt_usd,
    fmt_won,
    permalink,
    render_caption,
)


# ------------------------------------------------------------------
# permalink
# ------------------------------------------------------------------
def test_permalink_public_channel():
    assert permalink("@uschartkim", 42) == "https://t.me/uschartkim/42"


def test_permalink_private_channel():
    assert permalink("-1003578486245", 7) == "https://t.me/c/3578486245/7"


def test_permalink_invalid():
    assert permalink("uschartkim", 1) is None  # @ 없음 + -100 아님
    assert permalink("12345", 1) is None


# ------------------------------------------------------------------
# 포맷터
# ------------------------------------------------------------------
def test_fmt_pct():
    assert fmt_pct(5.23) == "+5.2%"
    assert fmt_pct(-2.5) == "-2.5%"
    assert fmt_pct(None) == "N/A"
    assert fmt_pct("x") == "N/A"


def test_fmt_won():
    assert fmt_won(1.5e12) == "1.5조원"
    assert fmt_won(8.5e11) == "8,500억원"
    assert fmt_won(5_000) == "5,000원"
    assert fmt_won(None) == "N/A"
    assert fmt_won(0) == "N/A"


def test_fmt_usd():
    assert fmt_usd(3.4e12) == "$3400.0B"
    assert fmt_usd(2.5e8) == "$250M"
    assert fmt_usd(999) == "$999"
    assert fmt_usd(None) == "N/A"


def test_badge_keys():
    # 신호 5종 전부 매핑 존재 (signals.py와 카테고리명 계약)
    for cat in ("high_all", "high_52w", "vcp_breakout",
                "volume_breakout", "near_breakout_52w"):
        assert cat in BADGE


# ------------------------------------------------------------------
# render_caption — KR/US 캡션 계약
# ------------------------------------------------------------------
def _kr_caption(**over):
    kw = dict(
        market_label="🇰🇷",
        ticker="005930",
        item={"name": "삼성전자", "chg_pct": 2.1, "market_cap": 4.5e14},
        cats=["high_all"],
        turnover=8.5e11,
        ytd=15.0,
        eps=22.5,
        market_cap=4.5e14,
        fmt_money=fmt_won,
        news_url_template="https://finance.naver.com/item/news.naver?code={ticker}",
        header_use_name=True,
        show_ticker_in_name=True,
        eps_label="최근 EPS YoY",
    )
    kw.update(over)
    return render_caption(**kw)


def test_kr_caption_layout():
    cap = _kr_caption()
    lines = cap.split("\n")
    assert lines[0] == "🇰🇷 삼성전자 (+2.1%)"          # 헤더는 종목명 (KR 관례)
    assert "💥 역사적 신고가 돌파" in lines[1]
    assert "✝ 종목명 : 삼성전자 (005930)" in cap
    assert "450.0조원" in cap
    assert "최근 EPS YoY : +22.5%" in cap
    assert "code=005930" in cap


def test_us_caption_with_earnings_marker():
    cap = render_caption(
        market_label="🇺🇸",
        ticker="NVDA",
        item={"name": "NVIDIA", "chg_pct": 5.2},
        cats=["high_52w"],
        turnover=5e10,
        ytd=45.0,
        eps=89.0,
        market_cap=3.4e12,
        fmt_money=fmt_usd,
        news_url_template="https://finviz.com/quote.ashx?t={ticker}",
        earnings_date="2026-06-08",
    )
    lines = cap.split("\n")
    assert lines[0] == "🇺🇸 NVDA (+5.2%)"               # 헤더는 ticker (US 관례)
    assert lines[3] == "⚡ 다음 어닝 : 2026-06-08"        # badge 다음 빈 줄 다음
    assert "✝ 종목명 : NVIDIA" in cap
    assert "t=NVDA" in cap


def test_caption_html_escapes_name():
    # "삼성E&A" 같은 이름 — HTML 모드에서 & < > 이스케이프 필수
    cap = _kr_caption(item={"name": "삼성E&A <테스트>", "chg_pct": 1.0})
    assert "&amp;" in cap
    assert "&lt;테스트&gt;" in cap
    assert "<테스트>" not in cap


def test_caption_no_earnings_marker_when_absent():
    cap = _kr_caption()
    assert "⚡" not in cap
