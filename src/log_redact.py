"""로그·진단 출력에서 자격증명 값 마스킹 (2026-10-21 Railway env 유출 사고 후속).

사고: 부팅 진단(diag)이 OPENROUTER_MANAGEMENT_KEY 등을 원문 출력했고, 예외 로그에
`?apikey=...` URL·`api.telegram.org/bot<token>`·PTB "The token `...` was rejected"
메시지가 그대로 남았다. 로그는 호스트(Railway)에 평문 보관되므로 값 자체를 지운다.

공개 API (다른 모듈이 의존 — 시그니처 변경 금지):
  - is_secret_name(name)  — env 이름이 비밀값인지 (deny 쪽으로 넓게)
  - redact(text)          — 문자열에서 비밀값 제거. 절대 raise 안 함
  - RedactingFormatter    — format() 결과에 redact 적용 (exc_text/stack 포함)
  - install_redaction()   — root logger 모든 handler의 formatter를 감쌈 (멱등)
  - fingerprint(value)    — sha256 앞 8자리 (값 노출 없이 "어떤 값이 배포됐나" 비교용)

stdlib만 사용 — 어디서든(부팅 최초 시점 포함) import 가능해야 함.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import sys
import traceback
from urllib.parse import quote, quote_plus

# 이 길이 미만 env 값은 정확일치 치환 대상에서 제외 — 짧은 값("true", 5자리 코드)을
# 치환하면 무관한 로그 텍스트가 대량으로 깨진다.
MIN_SECRET_VALUE_LEN = 8

_SECRET_NAME_RE = re.compile(
    # AUTHOR(RAILWAY_GIT_AUTHOR 등)는 비밀 아님 → AUTH 뒤 OR 제외
    r"KEY|TOKEN|PW|PASS|SECRET|HASH|SESSION|_CODE|PHONE|COOKIE|CREDENTIAL|AUTH(?!OR)",
    re.IGNORECASE,
)
# 이름에 비밀 키워드가 섞여도 값이 공개 정보인 패턴 (모델명·chat/channel id)
_NON_SECRET_SUFFIX_RE = re.compile(
    r"(?:^|_)(?:MODELS?|CHAT_IDS?|CHANNEL_IDS?)$", re.IGNORECASE,
)
# 키워드 우연 일치 — 셸/파이썬 런타임 변수 (PWD의 PW, PYTHONHASHSEED의 HASH)
_NON_SECRET_NAMES = frozenset({"PWD", "OLDPWD", "PYTHONHASHSEED"})
# 키워드는 없지만 계정 식별자라 비밀 취급
_SECRET_ID_NAMES = frozenset({"WISEREPORT_ID", "KRX_ID"})


@functools.lru_cache(maxsize=4096)
def is_secret_name(name: str) -> bool:
    """env 이름이 비밀값(키·토큰·비번·세션·계정 id 등)을 담는지."""
    if not name:
        return False
    upper = name.upper()
    if upper in _SECRET_ID_NAMES:
        return True
    if upper in _NON_SECRET_NAMES or _NON_SECRET_SUFFIX_RE.search(upper):
        return False
    return bool(_SECRET_NAME_RE.search(upper))


# ------------------------------------------------------------------
# 패턴 기반 마스킹 — env에 없는 값(다른 서비스의 키, 로그에 찍힌 URL 등)도 잡는다
# ------------------------------------------------------------------
_MASK = "‹REDACTED›"

_PATTERNS: list[tuple[re.Pattern[str], object]] = [
    # 텔레그램 봇 토큰 — URL의 /bot<id>:<secret> 및 접두사 없는 형태.
    # bot id(콜론 앞)는 공개 정보라 남김 (어느 봇인지 디버깅용), 콜론 뒤만 제거.
    (
        re.compile(r"(?<!\d)(bot)?(\d{6,12}):[A-Za-z0-9_-]{30,}"),
        lambda m: f"{m.group(1) or ''}{m.group(2)}:{_MASK}",
    ),
    # 쿼리 파라미터 — 파라미터명 유지, 값만 마스킹. 이미 ‹NAME›으로 치환된 값은 건드리지 않음.
    (
        re.compile(
            r"(?i)\b(apikey|api_key|token|crtfc_key|access_token|key"
            r"|password|passwd|client_secret|secret)=(?!‹)[^&\s'\"]+"
        ),
        lambda m: f"{m.group(1)}={_MASK}",
    ),
    # OpenRouter 키 (일반 sk- 패턴보다 먼저)
    (re.compile(r"(?<![A-Za-z0-9])sk-or-v1-[0-9a-f]{16,}"), "‹OPENROUTER_KEY›"),
    # OpenAI 계열 sk-/sk-proj- 키 (task-xxx 같은 단어 내부 일치 방지 lookbehind)
    (re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}"), "‹API_KEY›"),
    # Authorization 헤더
    (
        re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{16,}"),
        lambda m: f"{m.group(1)} {_MASK}",
    ),
    # Telethon StringSession (urlsafe base64, 버전 문자 '1'로 시작, ~350자)
    (re.compile(r"(?<![A-Za-z0-9_-])1[A-Za-z0-9_-]{300,}={0,2}"), "‹TG_SESSION›"),
]


# ------------------------------------------------------------------
# env 정확일치 마스킹 — env 스냅샷이 바뀔 때만 regex 재컴파일
# ------------------------------------------------------------------
# (캐시 key, 컴파일된 패턴 or None, 치환 문자열 → env 이름)
_ENV_CACHE: tuple[tuple | None, re.Pattern[str] | None, dict[str, str]] = (None, None, {})


def _env_snapshot_key() -> tuple:
    """env 변경 감지용 캐시 key — 매 로그 라인마다 호출되므로 싸야 함.

    os.environ.items()는 값마다 decode라 160개 기준 ~100µs/라인 → 원시 dict(_data,
    CPython 내부) 튜플이면 ~3µs. 내부 속성이 없으면 공개 API로 폴백.
    """
    data = getattr(os.environ, "_data", None)
    if isinstance(data, dict):
        return tuple(data.items())
    return tuple(os.environ.items())


def _secret_env_items() -> tuple[tuple[str, str], ...]:
    """현재 os.environ에서 비밀 이름 + 충분히 긴 값만 (이름순)."""
    return tuple(sorted(
        (k, v) for k, v in os.environ.items()
        if len(v) >= MIN_SECRET_VALUE_LEN and is_secret_name(k)
    ))


def _env_pattern() -> tuple[re.Pattern[str] | None, dict[str, str]]:
    global _ENV_CACHE
    key = _env_snapshot_key()
    cached_key, cached_pat, cached_map = _ENV_CACHE
    if key == cached_key:
        return cached_pat, cached_map
    lookup: dict[str, str] = {}
    for name, value in _secret_env_items():
        # URL 인코딩된 형태(쿼리스트링·폼 바디)도 함께 — 특수문자 포함 비번 대비
        for variant in (value, quote(value, safe=""), quote_plus(value)):
            if len(variant) >= MIN_SECRET_VALUE_LEN:
                lookup.setdefault(variant, name)
    pat = None
    if lookup:
        # 긴 값 먼저 — 다른 값의 부분문자열인 값이 먼저 잘라먹지 않게
        alts = sorted(lookup, key=len, reverse=True)
        pat = re.compile("|".join(re.escape(a) for a in alts))
    _ENV_CACHE = (key, pat, lookup)
    return pat, lookup


def redact(text: str) -> str:
    """문자열에서 비밀값 제거. 실패 시 원문 반환 (로깅 경로라 절대 raise 금지)."""
    try:
        if not text or not isinstance(text, str):
            return text
        out = text
        pat, lookup = _env_pattern()
        if pat is not None:
            out = pat.sub(lambda m: f"‹{lookup.get(m.group(0), 'SECRET')}›", out)
        for rx, repl in _PATTERNS:
            out = rx.sub(repl, out)
        return out
    except Exception:
        return text


class RedactingFormatter(logging.Formatter):
    """format() 결과(메시지 + exc_text + stack_info 포함)에 redact 적용.

    `inner`를 주면 기존 formatter에 포맷을 위임 (install_redaction이 handler별
    format 문자열/datefmt/커스텀 서브클래스를 그대로 유지하려고 사용).
    """

    def __init__(self, *args, inner: logging.Formatter | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._inner = inner
        if inner is not None:
            # 외부에서 handler.formatter._fmt/datefmt를 들여다보는 코드 호환
            self._style = getattr(inner, "_style", self._style)
            self._fmt = getattr(inner, "_fmt", self._fmt)
            self.datefmt = getattr(inner, "datefmt", self.datefmt)

    def format(self, record: logging.LogRecord) -> str:
        if self._inner is not None:
            s = self._inner.format(record)
        else:
            s = super().format(record)
        return redact(s)


def _excepthook_installed() -> bool:
    return bool(getattr(sys.excepthook, "_log_redact", False))


def _install_excepthook() -> None:
    """미처리 예외 traceback(stderr 직출력, logging 미경유)도 마스킹.

    기본 hook일 때만 교체 — 다른 코드가 설치한 커스텀 hook은 건드리지 않음.
    """
    if _excepthook_installed() or sys.excepthook is not sys.__excepthook__:
        return
    original = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            sys.stderr.write(redact(text))
            sys.stderr.flush()
        except Exception:
            original(exc_type, exc, tb)

    _hook._log_redact = True  # type: ignore[attr-defined]
    sys.excepthook = _hook


def install_redaction() -> None:
    """root logger의 모든 handler formatter를 RedactingFormatter로 감싼다 (멱등).

    logging.basicConfig 직후 호출. 이후 추가되는 handler는 대상 아님 — 다시 호출하면 됨.
    """
    try:
        root = logging.getLogger()
        for handler in list(root.handlers):
            current = handler.formatter
            if isinstance(current, RedactingFormatter):
                continue
            # formatter 미설정 handler는 logging 기본("%(message)s")과 동일하게
            handler.setFormatter(RedactingFormatter(inner=current))
    except Exception:
        pass
    try:
        _install_excepthook()
    except Exception:
        pass


def fingerprint(value: str) -> str:
    """sha256 앞 8자리 — 배포된 값이 어떤 것인지 원문 없이 비교.

    저엔트로피 값(짧은 비번·인증코드·전화번호)엔 쓰지 말 것: 8자리 해시도 전수 대입으로
    원문이 복원된다. 랜덤 키/토큰처럼 충분히 긴 값 전용.
    """
    return hashlib.sha256((value or "").encode("utf-8", "replace")).hexdigest()[:8]
