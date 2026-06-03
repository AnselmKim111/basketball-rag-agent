"""차트 채널 캡션 공통 템플릿. KR/US 차이는 인자로 주입.

KR 예: market_label='🇰🇷', fmt_money=fmt_won, news_url_template='https://finance.naver.com/item/news.naver?code={ticker}'
US 예: market_label='🇺🇸', fmt_money=fmt_usd, news_url_template='https://finviz.com/quote.ashx?t={ticker}'
"""
from __future__ import annotations

from typing import Callable

from src.bot_helpers import html_escape
from src.screener_common.badges import BADGE
from src.screener_common.format import fmt_pct


def render_caption(
    *,
    market_label: str,
    ticker: str,
    item: dict,
    cats: list[str],
    turnover,
    ytd,
    eps,
    market_cap,
    fmt_money: Callable,
    news_url_template: str,
    earnings_date: str | None = None,
    header_use_name: bool = False,
    show_ticker_in_name: bool = False,
    eps_label: str = "최근분기 EPS YoY",
) -> str:
    """차트 캡션 (HTML parse_mode 가정).

    header_use_name: 첫줄에 종목명 사용 (KR), False면 ticker (US).
    show_ticker_in_name: 종목명 줄에 (ticker)도 함께 (KR 관례).
    earnings_date 있으면 badge 아래 ⚡ 라인 삽입 (US 어닝 마커).
    """
    name = html_escape(item.get("name") or ticker)
    chg = item.get("chg_pct") or 0.0
    badge = " ".join(BADGE[c] for c in cats if c in BADGE)
    header_value = name if header_use_name else html_escape(ticker)
    name_line_value = f"{name} ({html_escape(ticker)})" if show_ticker_in_name else name
    lines = [
        f"{market_label} {header_value} ({chg:+.1f}%)",
        badge,
        "",
        f"✝ 종목명 : {name_line_value}",
        f"✝ 시가총액 : {fmt_money(market_cap)}",
        f"✝ 거래대금 : {fmt_money(turnover)}",
        f"✝ 연초대비 상승률 : {fmt_pct(ytd)}",
        f"✝ {eps_label} : {fmt_pct(eps)}",
        f'✝ <a href="{news_url_template.format(ticker=ticker)}">최신 종목 뉴스 조회</a>',
    ]
    if earnings_date:
        lines.insert(3, f"⚡ 다음 어닝 : {html_escape(earnings_date)}")
    return "\n".join(lines)
