"""KR subscribers — `screener_core.subscribers` 공통 구현 호출."""
from __future__ import annotations

from src.screener import db as _db
from src.screener_core.subscribers import KST, make_api

_api = make_api(db=_db)

subscribe = _api.subscribe
unsubscribe = _api.unsubscribe
block = _api.block
unblock = _api.unblock
list_active_chat_ids = _api.list_active_chat_ids
list_all = _api.list_all
is_subscribed = _api.is_subscribed

__all__ = [
    "subscribe",
    "unsubscribe",
    "block",
    "unblock",
    "list_active_chat_ids",
    "list_all",
    "is_subscribed",
    "KST",
]
