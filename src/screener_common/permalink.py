"""텔레그램 채널 게시물 영구 링크 생성."""
from __future__ import annotations


def permalink(channel: str, message_id: int) -> str | None:
    """채널 게시물 영구 링크. @username → 공개, -100… → 비공개 /c/ 형식."""
    if channel.startswith("@"):
        return f"https://t.me/{channel.lstrip('@')}/{message_id}"
    cid = channel.lstrip()
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None
