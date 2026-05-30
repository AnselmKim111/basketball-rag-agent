"""테스트 환경 셋업.

로컬 dev 환경엔 `python-telegram-bot`이 없는데 (Railway에만 설치) bot_worker 등
모듈은 import time에 telegram을 import한다. 단위 테스트는 telegram을 실제로
호출하지 않으므로 더미 모듈을 등록해 import만 성공시킨다.
"""

from __future__ import annotations

import sys
import types


def _install_dummy_telegram() -> None:
    if "telegram" in sys.modules:
        return

    telegram = types.ModuleType("telegram")

    class _Stub:
        def __init__(self, *a, **kw) -> None:
            pass

        def __call__(self, *a, **kw):
            return self

        def __getattr__(self, name):
            return _Stub()

    for name in ("Bot", "Update", "InlineKeyboardButton", "InlineKeyboardMarkup"):
        setattr(telegram, name, _Stub)
    sys.modules["telegram"] = telegram

    constants = types.ModuleType("telegram.constants")
    constants.ParseMode = _Stub()
    sys.modules["telegram.constants"] = constants

    ext = types.ModuleType("telegram.ext")
    for name in (
        "Application", "CommandHandler", "ContextTypes",
        "MessageHandler", "filters", "CallbackQueryHandler",
    ):
        setattr(ext, name, _Stub)
    sys.modules["telegram.ext"] = ext


_install_dummy_telegram()
