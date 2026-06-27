"""KR validator — `screener_core.validator` 공통 구현 호출."""
from __future__ import annotations

from src.screener import data_source
from src.screener_core.validator import make_api

_api = make_api(data_source=data_source)

cross_validate = _api.cross_validate

__all__ = ["cross_validate"]
