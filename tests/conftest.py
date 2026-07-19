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


def _install_dummy_openai() -> None:
    """summarizer.py가 import time에 `from openai import APIStatusError, OpenAI`
    하므로 curator/deep_research/comparator를 import하는 단위 테스트에 필요."""
    if "openai" in sys.modules:
        return
    openai = types.ModuleType("openai")

    class _APIStatusError(Exception):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a)

    class _OpenAI:
        def __init__(self, *a, **kw) -> None:
            pass

    openai.APIStatusError = _APIStatusError
    openai.OpenAI = _OpenAI
    sys.modules["openai"] = openai


def _install_blanket_dummies(names: list[str]) -> None:
    """Railway 컨테이너에는 있지만 로컬 dev에 없는 외부 패키지들을 일괄 더미화.

    summarizer/wisereport/charts 등이 import time에 pypdf/playwright/httpx/bs4를
    가져옴. 단위 테스트는 실제 호출 없이 import만 검증하면 되므로 빈 모듈만 등록.
    """
    for n in names:
        if n in sys.modules:
            continue
        mod = types.ModuleType(n)

        class _Stub:
            def __init__(self, *a, **kw) -> None: pass
            def __call__(self, *a, **kw): return self
            def __getattr__(self, name): return _Stub()

        # 흔히 from X import Y 형태로 가져오는 심볼들을 무조건 stub로 노출
        mod.__getattr__ = lambda name, _s=_Stub: _s()  # type: ignore[attr-defined]
        sys.modules[n] = mod


_install_dummy_telegram()
_install_dummy_openai()
# bs4는 더미 제외 — channel_relay 파서 테스트가 실제 파싱을 검증 (dev에 설치됨).
_install_blanket_dummies([
    "pypdf", "playwright", "playwright.sync_api",
    "httpx", "matplotlib", "matplotlib.pyplot",
    "matplotlib.backends", "matplotlib.backends.backend_pdf",
    "PIL", "PIL.Image", "FinanceDataReader", "pykrx", "pykrx.stock",
])
