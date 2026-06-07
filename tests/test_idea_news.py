"""src/idea/news.py 회귀 테스트."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from unittest.mock import patch


def _install_fake_summarizer(response: str | list[str], raise_credit: bool = False):
    """src.summarizer를 fake 모듈로 교체. response가 list면 호출마다 다음 값 반환."""
    import src
    fake = types.ModuleType("src.summarizer")

    responses = response if isinstance(response, list) else [response]
    idx = {"i": 0}

    class _FakeResp:
        def __init__(self, content: str):
            self.choices = [types.SimpleNamespace(
                message=types.SimpleNamespace(content=content)
            )]

    class _FakeChatCompletions:
        def create(self, **kw):
            if raise_credit:
                raise RuntimeError("credit exhausted")
            c = responses[idx["i"]] if idx["i"] < len(responses) else responses[-1]
            idx["i"] += 1
            return _FakeResp(c)

    class _FakeChat:
        completions = _FakeChatCompletions()

    class _FakeClient:
        chat = _FakeChat()

    fake.get_client = lambda: _FakeClient()
    fake._is_credit_error = lambda e: "credit" in str(e).lower()
    sys.modules["src.summarizer"] = fake
    setattr(src, "summarizer", fake)
    return fake


def _uninstall_fake_summarizer():
    import src
    sys.modules.pop("src.summarizer", None)
    if hasattr(src, "summarizer"):
        delattr(src, "summarizer")


def test_normalize_news_item_keeps_valid():
    from src.idea.news import _normalize_news_item
    raw = {
        "date": "2026-06-03",
        "headline": "SK하이닉스 capa 2배",
        "source": "한경",
        "impact_hint": "수혜",
    }
    out = _normalize_news_item(raw)
    assert out is not None
    assert out["headline"] == "SK하이닉스 capa 2배"
    assert out["date"] == "2026-06-03"


def test_normalize_news_item_missing_headline_returns_none():
    from src.idea.news import _normalize_news_item
    assert _normalize_news_item({"date": "2026-06-01"}) is None
    assert _normalize_news_item({}) is None
    assert _normalize_news_item(None) is None
    assert _normalize_news_item("not a dict") is None


def test_normalize_news_item_invalid_date_blanked():
    """잘못된 형식의 date는 빈 문자열로 교정 (headline은 유지)."""
    from src.idea.news import _normalize_news_item
    out = _normalize_news_item({"date": "어제", "headline": "발표"})
    assert out is not None
    assert out["date"] == ""


def test_filter_by_age_keeps_recent():
    from src.idea.news import _filter_by_age
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    today = datetime.now(KST).date()
    items = [
        {"date": (today - timedelta(days=5)).isoformat(), "headline": "최근"},
        {"date": (today - timedelta(days=60)).isoformat(), "headline": "옛날"},
        {"date": "", "headline": "날짜 없음 통과"},
    ]
    out = _filter_by_age(items, days=30)
    headlines = [it["headline"] for it in out]
    assert "최근" in headlines
    assert "날짜 없음 통과" in headlines
    assert "옛날" not in headlines


def test_fetch_one_sync_success():
    from src.idea import news
    fake_json = (
        '{"news": ['
        ' {"date": "2026-06-03", "headline": "SK하이닉스 capa 2배 발표",'
        '  "source": "한경", "impact_hint": "수혜"}'
        ']}'
    )
    _install_fake_summarizer(fake_json)
    try:
        out = news._fetch_one_sync("SK하이닉스", "000660", days=30)
    finally:
        _uninstall_fake_summarizer()
    assert len(out) == 1
    assert out[0]["headline"] == "SK하이닉스 capa 2배 발표"


def test_fetch_one_sync_empty_response_returns_empty():
    from src.idea import news
    _install_fake_summarizer("")
    try:
        out = news._fetch_one_sync("종목", "000001", days=30)
    finally:
        _uninstall_fake_summarizer()
    assert out == []


def test_fetch_one_sync_credit_error_returns_empty():
    from src.idea import news
    _install_fake_summarizer("dummy", raise_credit=True)
    try:
        out = news._fetch_one_sync("종목", "000001", days=30)
    finally:
        _uninstall_fake_summarizer()
    assert out == []


def test_fetch_one_sync_caps_at_5():
    from src.idea import news
    items_json = (
        '{"news": ['
        + ",".join(
            f'{{"date": "2026-06-0{i+1}", "headline": "뉴스{i}", "source": "S"}}'
            for i in range(8)
        )
        + ']}'
    )
    _install_fake_summarizer(items_json)
    try:
        out = news._fetch_one_sync("종목", "000001", days=30)
    finally:
        _uninstall_fake_summarizer()
    assert len(out) == 5


def test_fetch_recent_news_batch_parallel():
    from src.idea import news
    fake_json = (
        '{"news": [{"date": "2026-06-03", "headline": "공급계약", "source": "한경"}]}'
    )
    _install_fake_summarizer(fake_json)
    top = [
        {"name": "한미반도체", "ticker6": "042700"},
        {"name": "리노공업", "ticker6": "058470"},
        {"name": "비상장", "ticker6": ""},  # ticker 없음 → skip
        {"name": "", "ticker6": "095340"},  # name 없음 → skip
    ]
    try:
        out = asyncio.run(news.fetch_recent_news_batch(top, days=30))
    finally:
        _uninstall_fake_summarizer()
    assert "042700" in out and "058470" in out
    assert len(out["042700"]) == 1
    # ticker 없는 종목은 결과에 안 들어감 (또는 빈 리스트)
    assert out.get("") in (None, [])


def test_fetch_recent_news_batch_empty_top_returns_empty():
    from src.idea import news
    out = asyncio.run(news.fetch_recent_news_batch([], days=30))
    assert out == {}
