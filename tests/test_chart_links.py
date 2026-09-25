"""메시지의 모든 티커/종목명은 차트로 연결된다 (2026-09-25 사용자 지시 "기본이잖아").

회귀 원인: 검증 발송 때 US_SCREENER_CHART_MAX=10 → 104종목 중 94개가 링크 없이 발송.
KR은 소형주 버킷이 차트 게시 대상에서 빠져 링크 없음. 이제 상한 기본 없음 +
게시 못 한 종목은 외부 차트(Naver/Yahoo) 폴백 → 링크 없는 이름 0.
"""
from __future__ import annotations

import asyncio
import re
import types
from datetime import datetime

import pytest

pytest.importorskip("pandas")


def _rows(n: int = 30, base: str = "2026-09-24") -> list[dict]:
    return [{"date": base, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1}] * n


def _plain_names(msg: str) -> list[str]:
    """<a> 밖에 남은 종목명 토큰 (업종 괄호·구분자 제외)."""
    stripped = re.sub(r"<a href=\"[^\"]+\">[^<]*</a>", "", msg)
    return [n for n in re.findall(r"종목\d+", stripped)]


def _kr_results():
    big, small = 500_000_000_000, 50_000_000_000
    nh = [{"ticker": f"{i:06d}", "name": f"종목{i}", "chg_pct": 10 - i * 0.01,
           "market_cap": big, "sector": "반도체" if i % 2 else "기계"} for i in range(1, 31)]
    near = [{"ticker": f"{i:06d}", "name": f"종목{i}", "chg_pct": 1.0,
             "market_cap": big, "sector": "화학"} for i in range(100, 110)]
    sm = [{"ticker": f"{i:06d}", "name": f"종목{i}", "chg_pct": 5.0 - i * 0.01,
           "market_cap": small, "sector": "기타"} for i in range(200, 220)]
    return {"high_52w": nh, "near_breakout_52w": near, "high_52w_small": sm}


def test_kr_every_shown_name_is_linked_without_channel(monkeypatch):
    import src.screener_bot as kr_bot
    from src.screener import db, formatter, fundamentals
    monkeypatch.delenv("SCREENER_CHART_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SCREENER_CHART_CHANNEL_ID", raising=False)
    monkeypatch.setattr(db, "load_ohlcv", lambda t, days=260: _rows())
    monkeypatch.setattr(db, "meta_get", lambda k: None)
    monkeypatch.setattr(fundamentals, "eps_yoy_map", lambda ts, bd: {})
    results = _kr_results()
    links, _ = asyncio.run(kr_bot._post_charts_and_meta(results, "2026-09-24"))
    shown = {it["ticker"] for its in formatter.shown_items(results).values() for it in its}
    assert shown and set(links) == shown
    assert links["000200"].startswith("https://m.stock.naver.com/fchart/domestic/stock/")
    msg = formatter.format_results(results, datetime(2026, 9, 24, 16, 5), links=links)
    assert "(소형주/" in msg and "외 " in msg          # 생략분 존재하는 케이스
    assert _plain_names(msg) == []                      # 찍힌 이름은 전부 <a>


def test_kr_posts_all_and_falls_back_on_failure(monkeypatch):
    import src.screener_bot as kr_bot
    import src.bot_helpers as bh
    from src.screener import chart, db, formatter, fundamentals
    monkeypatch.setenv("SCREENER_CHART_BOT_TOKEN", "x")
    monkeypatch.setenv("SCREENER_CHART_CHANNEL_ID", "@krchartkim")
    monkeypatch.delenv("SCREENER_CHART_MAX", raising=False)
    monkeypatch.setattr(db, "load_ohlcv", lambda t, days=260: _rows())
    monkeypatch.setattr(db, "meta_get", lambda k: None)
    monkeypatch.setattr(db, "meta_set", lambda k, v: None)
    monkeypatch.setattr(fundamentals, "eps_yoy_map", lambda ts, bd: {})
    monkeypatch.setattr(chart, "render_candle_volume", lambda t, rows, title="": b"png")

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(kr_bot.asyncio, "sleep", _no_sleep)
    posted = []

    async def _send(bot, channel, png, cap, mode):
        posted.append(cap)
        if len(posted) == 3:          # 한 건 게시 실패
            return None
        return types.SimpleNamespace(message_id=len(posted))
    monkeypatch.setattr(bh, "send_channel_photo", _send)
    monkeypatch.setattr(kr_bot, "_permalink", lambda ch, mid: f"https://t.me/krchartkim/{mid}")
    results = _kr_results()
    links, _ = asyncio.run(kr_bot._post_charts_and_meta(results, "2026-09-24"))
    shown = {it["ticker"] for its in formatter.shown_items(results).values() for it in its}
    assert len(posted) == len(shown) > 40               # 상한 없이 전부 게시 (예전 cap 120·소형주 제외)
    assert set(links) == shown
    tg = [u for u in links.values() if u.startswith("https://t.me/")]
    assert len(tg) == len(shown) - 1                    # 실패 1건만 Naver 폴백
    small_shown = {it["ticker"] for it in formatter.shown_items(results)["new_high_small"]}
    assert len(small_shown) == formatter.SMALL_SHOW_MAX and small_shown <= set(links)


def test_us_every_displayed_ticker_is_linked(monkeypatch):
    import src.us_screener_bot as us_bot
    from src.us_screener import db, earnings_proximity, fundamentals
    from src.us_screener import formatter as fmt
    monkeypatch.setenv("US_SCREENER_CHART_BOT_TOKEN", "x")
    monkeypatch.setenv("US_SCREENER_CHART_CHANNEL_ID", "@uschartkim")
    monkeypatch.setenv("US_SCREENER_CHART_MAX", "10")   # 상한 걸려도 나머지는 Yahoo 폴백
    monkeypatch.setattr(db, "load_ohlcv", lambda t, days=260: _rows())
    monkeypatch.setattr(db, "meta_get", lambda k: None)
    monkeypatch.setattr(db, "meta_set", lambda k, v: None)
    monkeypatch.setattr(fundamentals, "ticker_fundamentals", lambda t: {})
    monkeypatch.setattr(earnings_proximity, "fetch_upcoming_earnings", lambda ts, days_fwd=7: {})
    from src.us_screener import chart
    monkeypatch.setattr(chart, "render_candle_volume", lambda t, rows, title="": b"png")
    import src.bot_helpers as bh

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(us_bot.asyncio, "sleep", _no_sleep)

    async def _send(bot, channel, png, cap, mode):
        return types.SimpleNamespace(message_id=1)
    monkeypatch.setattr(bh, "send_channel_photo", _send)
    monkeypatch.setattr(us_bot, "_permalink", lambda ch, mid: "https://t.me/uschartkim/1")
    results = {"high_52w": [{"ticker": t, "name": t, "chg_pct": 1.0, "market_cap": 2e9}
                            for t in ["AAPL", "BRKB", "MOG.A"] + [f"T{i}" for i in range(20)]]}
    links, extra = asyncio.run(us_bot._post_charts_and_meta(results, "2026-09-24"))
    disp = {it["ticker"] for it in fmt.displayed_items(results)}
    assert set(links) == disp
    assert sum(u.startswith("https://t.me/") for u in links.values()) == 10
    yahoo = [t for t, u in links.items() if "finance.yahoo.com" in u]
    assert len(yahoo) == len(disp) - 10
    for t in ("BRKB", "MOG.A"):
        if t in yahoo:
            assert "BRK-B" in links[t] or "MOG-A" in links[t]
    msg = fmt.format_results(results, datetime(2026, 9, 25, 7, 0), links=links, extra=extra)
    for t in disp:
        assert re.search(rf'<a href="[^"]+">{re.escape(t)}</a>', msg), t


def test_us_default_has_no_post_cap(monkeypatch):
    import src.us_screener_bot as us_bot
    from src.us_screener import db, earnings_proximity, fundamentals, chart
    import src.bot_helpers as bh
    monkeypatch.setenv("US_SCREENER_CHART_BOT_TOKEN", "x")
    monkeypatch.setenv("US_SCREENER_CHART_CHANNEL_ID", "@uschartkim")
    monkeypatch.delenv("US_SCREENER_CHART_MAX", raising=False)
    monkeypatch.setattr(db, "load_ohlcv", lambda t, days=260: _rows())
    monkeypatch.setattr(db, "meta_get", lambda k: None)
    monkeypatch.setattr(db, "meta_set", lambda k, v: None)
    monkeypatch.setattr(fundamentals, "ticker_fundamentals", lambda t: {})
    monkeypatch.setattr(earnings_proximity, "fetch_upcoming_earnings", lambda ts, days_fwd=7: {})
    monkeypatch.setattr(chart, "render_candle_volume", lambda t, rows, title="": b"png")

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(us_bot.asyncio, "sleep", _no_sleep)
    n = []

    async def _send(bot, channel, png, cap, mode):
        n.append(1)
        return types.SimpleNamespace(message_id=len(n))
    monkeypatch.setattr(bh, "send_channel_photo", _send)
    monkeypatch.setattr(us_bot, "_permalink", lambda ch, mid: f"https://t.me/uschartkim/{mid}")
    results = {cat: [{"ticker": f"{cat[:2]}{i}", "name": "x", "chg_pct": 1.0, "market_cap": 2e9}
                     for i in range(60)] for cat in ("high_52w", "near_breakout_52w")}
    links, _ = asyncio.run(us_bot._post_charts_and_meta(results))
    from src.us_screener import formatter as fmt
    disp = {it["ticker"] for it in fmt.displayed_items(results)}
    assert len(n) == len(disp) and all(u.startswith("https://t.me/") for u in links.values())
