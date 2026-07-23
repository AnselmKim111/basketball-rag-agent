"""src/channel_relay.py 파서·저자 필터 단위 테스트 (네트워크 없음)."""

from __future__ import annotations

from datetime import datetime, timezone

from src.channel_relay import ChannelPost, is_author_post, parse_preview_html

# t.me/s/<channel> 실제 구조 축약 fixture
FIXTURE_HTML = """
<html><body>
<div class="tgme_widget_message" data-post="DSInvResearch/101">
  <div class="tgme_widget_message_text">DS투자전략 양형모입니다.<br/>오늘 시황 요약:<br/>코스피 반등.</div>
  <time datetime="2026-07-19T08:00:00+00:00">08:00</time>
</div>
<div class="tgme_widget_message" data-post="DSInvResearch/102">
  <div class="tgme_widget_message_text">DS투자증권 리서치 다른 저자 글입니다.</div>
  <time datetime="2026-07-19T09:00:00+00:00">09:00</time>
</div>
<div class="tgme_widget_message" data-post="DSInvResearch/103">
  <div class="tgme_widget_message_photo_wrap"></div>
  <div class="tgme_widget_message_text">[DS투자전략 양형모] 차트 코멘트</div>
  <time datetime="2026-07-19T10:30:00+00:00">10:30</time>
</div>
<div class="tgme_widget_message" data-post="DSInvResearch/104">
  <div class="tgme_widget_message_photo_wrap"></div>
</div>
</body></html>
"""


def test_parse_preview_html():
    posts = parse_preview_html(FIXTURE_HTML)
    assert [p.post_id for p in posts] == [
        "DSInvResearch/101", "DSInvResearch/102",
        "DSInvResearch/103", "DSInvResearch/104",
    ]
    p101 = posts[0]
    assert "양형모" in p101.text
    assert "\n" in p101.text  # <br/> → 개행
    assert p101.posted_at == datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
    assert p101.url == "https://t.me/DSInvResearch/101"
    assert posts[2].has_media is True
    assert posts[3].text == ""  # 이미지-only — 텍스트 없음, id는 보존


def test_author_filter():
    posts = parse_preview_html(FIXTURE_HTML)
    hits = [p for p in posts if is_author_post(p, "양형모")]
    assert [p.post_id for p in hits] == ["DSInvResearch/101", "DSInvResearch/103"]


def test_author_filter_head_chars_only():
    """저자 마커가 글 뒤쪽에만 있으면 (인용 등) 미매칭 — 앞부분만 본다."""
    post = ChannelPost(post_id="X/1", text=("배경 설명. " * 60) + "양형모", posted_at=None)
    assert not is_author_post(post, "양형모", head_chars=300)
    assert is_author_post(post, "양형모", head_chars=10_000)


def test_parse_garbage_html_graceful():
    assert parse_preview_html("<html><body>nothing here</body></html>") == []
    assert parse_preview_html("") == []
