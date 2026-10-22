"""src/log_redact + diag_env_keys + bot_worker 자식 env 회귀 테스트.

2026-10-21 Railway env 유출 사고 후속: 부팅 diag·예외 로그·subprocess 실패 tail에
자격증명 원문이 남지 않아야 한다. 모든 값은 가짜.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import logging
import re
import sys
from pathlib import Path

import pytest

from src import log_redact
from src.log_redact import (
    RedactingFormatter,
    fingerprint,
    install_redaction,
    is_secret_name,
    redact,
)

REPO = Path(__file__).resolve().parent.parent

FAKE_BOT_TOKEN = "7123456789:AAFakeTokenValue_abcdefghijklmnopqrstu"
FAKE_SECRETS = {
    "OPENROUTER_MANAGEMENT_KEY": "sk-or-v1-" + "a1" * 32,
    "OPENROUTER_API_KEY": "sk-or-v1-" + "b2" * 32,
    "TG_SESSION_STRING": "1" + "AbCd-_09xYz" * 32 + "=",
    "TG_LOGIN_PASSWORD": "fake-pass-Zq9x",
    "TG_API_HASH": "0123456789abcdef0123456789abcdef",
    "DART_API_KEY": "fakedart1234567890abcdef1234567890ab",
    "FMP_API_KEY": "FAKEfmpKEY0123456789abcdefABCDEF",
    "ALPHA_VANTAGE_KEY": "FAKEAV0123456789",
    "FINNHUB_API_KEY": "fakefinnhub_cr0123456789",
    "WISEREPORT_PW": "Wise#Pass&2026!",
    "WISEREPORT_ID": "fakewiseuser01",
    "SCREENER_BOT_TOKEN": FAKE_BOT_TOKEN,
}
PLAIN_VALUES = {
    "OPENROUTER_MODEL": "deepseek/deepseek-v4-flash",
    "IDEA_SYNTHESIS_MODEL": "deepseek/deepseek-v4-pro",
    "SCREENER_CHAT_ID": "-1001234567890",
    "ALLOWED_CHAT_IDS": "1813560888,2222222222",
}


@pytest.fixture()
def fake_env(monkeypatch):
    for k, v in {**FAKE_SECRETS, **PLAIN_VALUES}.items():
        monkeypatch.setenv(k, v)
    return FAKE_SECRETS


@pytest.fixture()
def root_capture(monkeypatch):
    """root logger handler를 테스트 전용 StringIO 하나로 교체 (pytest 핸들러 오염 방지)."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(
        logging.Formatter("[%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M")
    )
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [handler])
    monkeypatch.setattr(root, "level", logging.INFO)
    # install_redaction이 교체하는 excepthook도 테스트 후 원복
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    return buf, handler


def _assert_no_secret(text: str, secrets: dict[str, str]) -> None:
    for name, value in secrets.items():
        assert value not in text, f"{name} 원문 노출"
        # 앞부분 조각도 남으면 안 됨 (truncate 출력 회귀 방지)
        assert value[:12] not in text, f"{name} 앞 12자 노출"


# ------------------------------------------------------------------
# is_secret_name
# ------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "OPENROUTER_MANAGEMENT_KEY", "OPENROUTER_API_KEY", "TG_SESSION_STRING",
    "TG_LOGIN_PASSWORD", "TG_API_HASH", "TG_LOGIN_CODE", "TG_LOGIN_PHONE",
    "DART_API_KEY", "ALPHA_VANTAGE_KEY", "WISEREPORT_PW", "WISEREPORT_ID",
    "KRX_ID", "SCREENER_BOT_TOKEN", "RAILWAY_PROJECT_ACCESS_TOKEN",
    "SOME_COOKIE", "GCP_CREDENTIALS", "X_AUTH", "client_secret",
])
def test_is_secret_name_true(name):
    assert is_secret_name(name) is True


@pytest.mark.parametrize("name", [
    "OPENROUTER_MODEL", "IDEA_SYNTHESIS_MODEL", "OPENROUTER_FALLBACK_MODEL",
    "ROUTER_CANDIDATE_MODELS", "SCREENER_CHAT_ID", "REPORT_CHAT_IDS",
    "ALLOWED_CHAT_IDS", "EARNINGS_ALLOWED_CHAT_IDS", "SCREENER_CHART_CHANNEL_ID",
    "RAILWAY_GIT_AUTHOR", "PWD", "PYTHONHASHSEED", "PATH", "TZ", "",
])
def test_is_secret_name_false(name):
    assert is_secret_name(name) is False


# ------------------------------------------------------------------
# redact — env 정확일치 + 패턴
# ------------------------------------------------------------------
def test_redact_env_values(fake_env):
    text = " | ".join(f"{k}={v}" for k, v in fake_env.items())
    out = redact(text)
    _assert_no_secret(out, fake_env)
    assert "‹OPENROUTER_MANAGEMENT_KEY›" in out
    assert "‹WISEREPORT_ID›" in out


def test_redact_url_encoded_env_value(fake_env):
    from urllib.parse import quote, quote_plus
    pw = fake_env["WISEREPORT_PW"]
    out = redact(f"POST body: pw={quote_plus(pw)} / q={quote(pw, safe='')}")
    assert quote_plus(pw) not in out and quote(pw, safe="") not in out
    assert "‹WISEREPORT_PW›" in out


def test_redact_does_not_touch_models_and_chat_ids(fake_env):
    text = " ".join(PLAIN_VALUES.values())
    assert redact(text) == text


def test_redact_patterns_without_env(monkeypatch):
    """env에 없는 값도 패턴으로 마스킹 (다른 서비스 키·로그에 찍힌 URL)."""
    for k in list(FAKE_SECRETS):
        monkeypatch.delenv(k, raising=False)
    other_tok = "6000000001:BBotherTokenValue_ZYXWVUTSRQPONMLKJIH"
    session = "1" + "Qw_-9" * 70 + "=="
    cases = {
        f"https://api.telegram.org/bot{other_tok}/getMe": other_tok.split(":")[1],
        f"The token `{other_tok}` was rejected by the server.": other_tok.split(":")[1],
        "GET https://x.io/q?symbol=AAPL&apikey=ZZsecretZZ123&x=1": "ZZsecretZZ123",
        "https://opendart.fss.or.kr/api/list.json?crtfc_key=DARTKEYabc999": "DARTKEYabc999",
        "url?access_token=tokval12345": "tokval12345",
        "url?token=tokval67890&b=2": "tokval67890",
        "url?key=keyval54321": "keyval54321",
        "auth sk-or-v1-" + "c3" * 32: "c3" * 32,
        "openai sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012": "ABCDEFGHIJKLMNOPQRSTUVWXYZ012",
        "Authorization: Bearer abcdefghijklmnop.qrstuvwx-yz": "abcdefghijklmnop",
        f"session='{session}'": session[:40],
    }
    for text, secret in cases.items():
        out = redact(text)
        assert secret not in out, (text, out)
    # 파라미터명·봇 id(공개)는 남김 — 디버깅용
    assert "apikey=‹REDACTED›" in redact("q?apikey=ZZsecretZZ123")
    assert "bot6000000001:‹REDACTED›/getMe" in redact(
        f"https://api.telegram.org/bot{other_tok}/getMe")
    assert "‹TG_SESSION›" in redact(f"s={session}")


def test_redact_no_false_positive_on_words():
    text = "task-abcdefghijklmnopqrstuvwxyz 완료, max_token=4000, monkey=1, 20261022 12:30:00"
    assert redact(text) == text


def test_redact_never_raises(monkeypatch):
    assert redact("") == ""
    assert redact(None) is None  # type: ignore[arg-type]

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(log_redact, "_ENV_FAILURE_REPORTED", False)
    monkeypatch.setattr(log_redact, "_env_pattern", boom)
    assert redact("plain text") == "plain text"


# ------------------------------------------------------------------
# fail-open 회귀 (SEC-2): env 단계 실패가 패턴 단계를 끄면 안 됨 / 패턴 실패는 fail closed
# ------------------------------------------------------------------
_LEAKY_LINE = (
    f"The token `{FAKE_BOT_TOKEN}` was rejected by the server. "
    "GET https://x.io/q?apikey=FAKEKEY123456&s=1"
)
_ENV_FAIL_NOTICE = "[log_redact] env 마스킹 빌드 실패"


def _assert_line_masked(out: str) -> None:
    assert FAKE_BOT_TOKEN.split(":")[1] not in out, out
    assert "FAKEKEY123456" not in out, out
    assert "apikey=‹REDACTED›" in out


def test_env_stage_failure_keeps_pattern_masking(monkeypatch, capsys):
    """_env_pattern이 터져도 토큰·apikey 패턴 마스킹은 그대로, stderr 통지는 1회·값 없음."""
    monkeypatch.setattr(log_redact, "_ENV_FAILURE_REPORTED", False)

    def boom():
        raise RuntimeError("SHOULD_NOT_PRINT_detail")

    monkeypatch.setattr(log_redact, "_env_pattern", boom)
    for _ in range(3):
        out = redact(_LEAKY_LINE)
        _assert_line_masked(out)
        assert "7123456789:‹REDACTED›" in out
    err = capsys.readouterr().err
    assert err.count(_ENV_FAIL_NOTICE) == 1
    assert "RuntimeError" in err
    assert "SHOULD_NOT_PRINT_detail" not in err


def test_surrogate_env_value_does_not_disable_masking(fake_env, monkeypatch, capsys):
    """비-UTF-8 바이트 env 값(surrogateescape 디코드) 1개가 전체 env 마스킹을 끄면 안 됨."""
    from urllib.parse import quote
    monkeypatch.setattr(log_redact, "_ENV_FAILURE_REPORTED", False)
    bad = "abc\udcffdefghij"  # os.environb b"abc\xffdefghij" 와 동일
    monkeypatch.setenv("SOME_LEGACY_SECRET", bad)
    with pytest.raises(UnicodeEncodeError):
        quote(bad, safe="")  # 수정 전 경로 — strict 인코딩은 터진다
    out = redact(_LEAKY_LINE)
    _assert_line_masked(out)
    assert "‹SCREENER_BOT_TOKEN›" in out  # env 단계도 살아 있음
    # 값 자체 + 원래 바이트 기준 URL 인코딩 형태 둘 다 마스킹
    assert redact(f"v={bad}") == "v=‹SOME_LEGACY_SECRET›"
    assert redact("q=abc%FFdefghij") == "q=‹SOME_LEGACY_SECRET›"
    dart = fake_env["DART_API_KEY"]
    assert redact(f"k {dart}") == "k ‹DART_API_KEY›"
    assert _ENV_FAIL_NOTICE not in capsys.readouterr().err


def test_surrogate_env_value_logging_end_to_end(fake_env, root_capture, monkeypatch):
    """원 재현: 깨진 env 값이 있어도 install_redaction 경로 로그에 토큰·apikey 원문 없음."""
    buf, _ = root_capture
    monkeypatch.setenv("SOME_LEGACY_SECRET", "abc\udcffdefghij")
    install_redaction()
    logging.getLogger("t.sec2").error("%s", _LEAKY_LINE)
    out = buf.getvalue()
    _assert_line_masked(out)
    _assert_no_secret(out, fake_env)


def test_env_build_failure_cached_and_reported_once(fake_env, monkeypatch, capsys):
    """빌드 실패는 같은 env 스냅샷에서 1회만 시도 (매 로그 라인 재빌드 금지), 통지는 1회."""
    monkeypatch.setattr(log_redact, "_ENV_FAILURE_REPORTED", False)
    monkeypatch.setattr(log_redact, "_ENV_CACHE", (None, None, {}))
    calls = []

    def broken_items():
        calls.append(1)
        raise ValueError("SHOULD_NOT_PRINT_value")

    monkeypatch.setattr(log_redact, "_secret_env_items", broken_items)
    for _ in range(5):
        _assert_line_masked(redact(_LEAKY_LINE))
    assert len(calls) == 1
    assert log_redact._env_pattern() == (None, {})
    assert len(calls) == 1
    # env가 바뀌면 재시도 (복구 기회), 통지는 여전히 프로세스당 1회
    monkeypatch.setenv("SEC2_CHANGE_MARKER", "1")
    _assert_line_masked(redact(_LEAKY_LINE))
    assert len(calls) == 2
    err = capsys.readouterr().err
    assert err.count(_ENV_FAIL_NOTICE) == 1
    assert "ValueError" in err and "SHOULD_NOT_PRINT_value" not in err


def test_value_variant_failure_keeps_raw_value(fake_env, monkeypatch, capsys):
    """URL 인코딩 변형 생성이 실패한 값도 원문 일치 마스킹은 유지, 다른 값도 영향 없음."""
    monkeypatch.setattr(log_redact, "_ENV_FAILURE_REPORTED", False)
    monkeypatch.setattr(log_redact, "_ENV_CACHE", (None, None, {}))

    def bad_quote(*a, **kw):
        raise TypeError("quote broken")

    monkeypatch.setattr(log_redact, "quote", bad_quote)
    assert log_redact._value_variants("abcdefghijk") == ("abcdefghijk",)
    pw = fake_env["WISEREPORT_PW"]
    out = redact(f"pw={pw} dart={fake_env['DART_API_KEY']}")
    assert out == "pw=‹WISEREPORT_PW› dart=‹DART_API_KEY›"
    assert _ENV_FAIL_NOTICE not in capsys.readouterr().err


def test_pattern_stage_failure_fails_closed(fake_env, monkeypatch):
    """패턴 단계 예외 시 원문(비밀값 포함 가능) 대신 placeholder."""
    def boom(m):
        raise RuntimeError("pattern bug")

    monkeypatch.setattr(log_redact, "_PATTERNS", [
        *log_redact._PATTERNS[:1], (re.compile(r"apikey"), boom),
    ])
    line = f"{_LEAKY_LINE} pw={fake_env['WISEREPORT_PW']}"
    assert redact(line) == "‹redaction-error›"
    # 패턴에 안 걸리는 줄은 정상 처리 (예외 경로 아님)
    assert redact("plain text") == "plain text"
    # formatter 경로도 동일 — 원문이 handler로 새지 않음
    rec = logging.LogRecord("n", logging.ERROR, __file__, 1, "%s", (line,), None)
    assert RedactingFormatter("%(message)s").format(rec) == "‹redaction-error›"


def test_redact_guard_failure_fails_closed():
    """str 서브클래스의 길이 판정이 터져도 raise·원문 반환 없이 placeholder."""
    class Weird(str):
        def __len__(self):
            raise RuntimeError("len broken")

    assert redact(Weird(FAKE_BOT_TOKEN)) == "‹redaction-error›"


def test_env_cache_refreshes_on_change(monkeypatch):
    monkeypatch.delenv("LATE_API_KEY", raising=False)
    assert redact("v=lateValue123456") == "v=lateValue123456"
    monkeypatch.setenv("LATE_API_KEY", "lateValue123456")
    assert redact("v=lateValue123456") == "v=‹LATE_API_KEY›"
    # 같은 env면 패턴 객체 재사용 (재컴파일 안 함)
    pat1 = log_redact._env_pattern()[0]
    pat2 = log_redact._env_pattern()[0]
    assert pat1 is pat2


def test_short_values_not_replaced(monkeypatch):
    monkeypatch.setenv("TG_LOGIN_CODE", "12345")
    assert redact("step 12345 done") == "step 12345 done"


# ------------------------------------------------------------------
# logging 통합 — install_redaction
# ------------------------------------------------------------------
def test_logged_exception_messages_redacted(fake_env, root_capture):
    buf, handler = root_capture
    install_redaction()
    log = logging.getLogger("test.redact")
    fmp = fake_env["FMP_API_KEY"]
    av = fake_env["ALPHA_VANTAGE_KEY"]
    msgs = [
        f"HTTP 401 for url https://financialmodelingprep.com/api/v3/x?apikey={fmp}",
        f"https://www.alphavantage.co/query?function=EARNINGS_CALL_TRANSCRIPT&apikey={av}",
        f"Client error for https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getMe",
        f"The token `{FAKE_BOT_TOKEN}` was rejected by the server.",
        f"login failed id={fake_env['WISEREPORT_ID']} pw={fake_env['WISEREPORT_PW']}",
        f"session {fake_env['TG_SESSION_STRING']} hash {fake_env['TG_API_HASH']}",
        f"openrouter 401 key={fake_env['OPENROUTER_API_KEY']} "
        f"mgmt={fake_env['OPENROUTER_MANAGEMENT_KEY']}",
    ]
    for m in msgs:
        try:
            raise RuntimeError(m)
        except RuntimeError:
            log.exception("요청 실패: %s", m)
    out = buf.getvalue()
    _assert_no_secret(out, fake_env)
    # 기존 format 문자열 유지 + 마스킹 마커 존재
    assert "[ERROR] test.redact: 요청 실패:" in out
    assert "Traceback" in out
    assert "apikey=‹FMP_API_KEY›" in out
    assert "‹SCREENER_BOT_TOKEN›" in out


def test_model_and_chat_id_values_survive_logging(fake_env, root_capture):
    buf, _ = root_capture
    install_redaction()
    logging.getLogger("t").info(
        "model=%s chat=%s allowed=%s",
        PLAIN_VALUES["OPENROUTER_MODEL"], PLAIN_VALUES["SCREENER_CHAT_ID"],
        PLAIN_VALUES["ALLOWED_CHAT_IDS"],
    )
    out = buf.getvalue()
    for v in PLAIN_VALUES.values():
        if v != PLAIN_VALUES["IDEA_SYNTHESIS_MODEL"]:
            assert v in out


def test_install_redaction_idempotent_and_keeps_format(root_capture):
    _, handler = root_capture
    original = handler.formatter
    install_redaction()
    install_redaction()
    fmt = handler.formatter
    assert isinstance(fmt, RedactingFormatter)
    assert fmt._inner is original  # 이중 래핑 없음
    assert fmt._fmt == "[%(levelname)s] %(name)s: %(message)s"
    assert fmt.datefmt == "%H:%M"


def test_install_redaction_handler_without_formatter(monkeypatch):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)  # formatter None → logging 기본 "%(message)s"
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [handler])
    monkeypatch.setattr(root, "level", logging.INFO)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    install_redaction()
    logging.getLogger("x").info("q?apikey=abcdef123456")
    assert buf.getvalue() == "q?apikey=‹REDACTED›\n"


def test_redacting_formatter_standalone(fake_env):
    f = RedactingFormatter("%(message)s")
    rec = logging.LogRecord("n", logging.INFO, __file__, 1,
                            "k=%s", (fake_env["DART_API_KEY"],), None)
    assert f.format(rec) == "k=‹DART_API_KEY›"


def test_excepthook_redacts_uncaught_traceback(fake_env, root_capture, capsys):
    install_redaction()
    assert getattr(sys.excepthook, "_log_redact", False)
    try:
        raise RuntimeError(f"The token `{FAKE_BOT_TOKEN}` was rejected by the server.")
    except RuntimeError as e:
        sys.excepthook(type(e), e, e.__traceback__)
    err = capsys.readouterr().err
    assert "Traceback" in err and "‹SCREENER_BOT_TOKEN›" in err
    assert FAKE_BOT_TOKEN not in err


def test_excepthook_custom_hook_untouched(root_capture, monkeypatch):
    def custom(*a):
        pass

    monkeypatch.setattr(sys, "excepthook", custom)
    install_redaction()
    assert sys.excepthook is custom


def test_fingerprint():
    v = "some-long-random-api-key-value"
    assert fingerprint(v) == hashlib.sha256(v.encode()).hexdigest()[:8]
    assert len(fingerprint(v)) == 8
    assert fingerprint(v) != fingerprint(v + "x")


# ------------------------------------------------------------------
# diag_env_keys — deny-by-default
# ------------------------------------------------------------------
_DIAG_PREFIXES = ("OPEN", "TG_", "DART", "FMP", "ALPHA", "FINN", "WISE",
                  "SCREENER", "IDEA", "CHAT", "ALLOWED")


def test_diag_env_keys_masks_everything_but_allowlist(fake_env):
    from src.bot_helpers import diag_env_keys
    diag = diag_env_keys(_DIAG_PREFIXES)
    text = repr(diag)
    _assert_no_secret(text, fake_env)
    # 관리키(사고 원인)는 orchestrator가 쓰는 'OPEN' prefix로도 마스킹
    only_open = diag_env_keys(("OPEN",))
    mgmt = fake_env["OPENROUTER_MANAGEMENT_KEY"]
    assert only_open["OPENROUTER_MANAGEMENT_KEY"] == (
        f"<set len={len(mgmt)} fp={fingerprint(mgmt)}>"
    )
    # 비밀번호류는 길이·fp 모두 생략 (사전 대입 확인 방지)
    assert diag["WISEREPORT_PW"] == "<set>"
    # allowlist는 원문
    for k, v in PLAIN_VALUES.items():
        assert diag[k] == v


def test_diag_env_keys_allowlist_names(monkeypatch):
    from src.bot_helpers import diag_env_keys
    plain = {
        "ACTIVE_BOTS": "screener,us_screener",
        "RAILWAY_PROJECT_ID": "proj-0000-1111",
        "RAILWAY_ENVIRONMENT_ID": "env-0000-1111",
        "RAILWAY_SERVICE_ID": "svc-0000-1111",
        "RAILWAY_SERVICE_IDS": "svc-a,svc-b",
        "SCREENER_CHART_CHANNEL_ID": "-1009999999999",
    }
    masked = {
        "RAILWAY_PROJECT_ACCESS_TOKEN": "fake-railway-token-0000000000",
        "RAILWAY_TOKEN": "fake-railway-token-1111111111",
        "SOME_UNKNOWN_SETTING": "not-on-allowlist-value",
    }
    for k, v in {**plain, **masked}.items():
        monkeypatch.setenv(k, v)
    diag = diag_env_keys(("ACTIVE", "RAILWAY", "SCREENER", "SOME_"))
    for k, v in plain.items():
        assert diag[k] == v
    for k, v in masked.items():
        assert v not in repr(diag)
        assert diag[k].startswith("<set len=")


def test_diag_env_keys_accepts_generator(fake_env):
    from src.bot_helpers import diag_env_keys
    diag = diag_env_keys(p for p in ("OPEN", "WISE"))
    assert "WISEREPORT_ID" in diag and "OPENROUTER_MODEL" in diag


def test_bot_worker_main_diag_has_no_values(fake_env, root_capture, monkeypatch, capsys):
    import src.bot_worker as bw
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1005555555555")
    with pytest.raises(SystemExit):
        bw.main()
    out = capsys.readouterr().out
    assert "DIAG: TELEGRAM_BOT_TOKEN 못 찾음" in out
    _assert_no_secret(out, fake_env)
    assert "'OPENROUTER_MANAGEMENT_KEY': '<set len=" in out
    assert "-1005555555555" in out  # chat id는 원문


# ------------------------------------------------------------------
# bot_worker._run_pipeline 자식 env allowlist
# ------------------------------------------------------------------
def _src_main_import_closure() -> list[Path]:
    """src.main에서 시작하는 src.* 전이 import 모듈 파일 목록 (함수 내 lazy import 포함)."""
    seen: dict[str, Path] = {}

    def resolve(mod: str) -> Path | None:
        p = REPO / (mod.replace(".", "/") + ".py")
        if p.exists():
            return p
        p = REPO / mod.replace(".", "/") / "__init__.py"
        return p if p.exists() else None

    def walk(mod: str) -> None:
        if mod in seen:
            return
        path = resolve(mod)
        if path is None:
            return
        seen[mod] = path
        pkg = mod if path.name == "__init__.py" else mod.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = pkg.split(".")
                    parts = parts[: len(parts) - (node.level - 1)]
                    base = ".".join(parts + ([base] if base else []))
                if base.startswith("src"):
                    walk(base)
                    for a in node.names:
                        walk(f"{base}.{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("src"):
                        walk(a.name)

    walk("src.main")
    return list(seen.values())


def _literal_env_reads(path: Path) -> set[str]:
    """os.getenv("X") / os.environ.get("X") / os.environ["X"] 의 리터럴 이름."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and node.args:
            fn = node.func
            arg = node.args[0]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                continue
            if isinstance(fn, ast.Attribute) and (
                (fn.attr == "getenv")
                or (fn.attr == "get" and isinstance(fn.value, ast.Attribute)
                    and fn.value.attr == "environ")
            ):
                names.add(arg.value)
        elif isinstance(node, ast.Subscript):
            v = node.value
            if isinstance(v, ast.Attribute) and v.attr == "environ":
                s = node.slice
                if isinstance(s, ast.Constant) and isinstance(s.value, str):
                    names.add(s.value)
    return names


def test_src_main_env_reads_all_forwarded():
    from src.bot_worker import _pipeline_child_env
    files = _src_main_import_closure()
    assert any(p.name == "summarizer.py" for p in files)
    assert any(p.name == "telegram_sender.py" for p in files)
    reads: set[str] = set()
    for p in files:
        reads |= _literal_env_reads(p)
    # 최소 보장 — AST 수집이 비정상적으로 비면 테스트 무의미
    must = {"WISEREPORT_ID", "WISEREPORT_PW", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
            "OPENROUTER_FALLBACK_MODEL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "TELEGRAM_USERNAME", "DOWNLOAD_DIR", "SUMMARY_DIR", "HEADLESS",
            "STORAGE_STATE", "TOP_N", "SORT_BY", "DISABLE_TELEGRAM"}
    assert must <= reads, must - reads
    # summarizer → model_router.health가 동적으로 읽는 티어 모델 env
    from src.model_router.candidates import TIER_ENV
    for envs in TIER_ENV.values():
        reads |= set(envs)
    fake = {n: "x" for n in reads}
    child = _pipeline_child_env(fake)
    missing = sorted(reads - set(child))
    assert not missing, f"src.main이 읽는데 자식 env에서 빠짐: {missing}"


def test_child_env_excludes_unrelated_secrets(fake_env, monkeypatch):
    from src.bot_worker import _pipeline_child_env
    excluded = {
        "TG_SESSION_STRING": "s", "TG_API_ID": "1", "TG_API_HASH": "h",
        "TG_LOGIN_PHONE": "p", "TG_LOGIN_CODE": "c", "TG_LOGIN_PASSWORD": "w",
        "RAILWAY_PROJECT_ACCESS_TOKEN": "r1", "RAILWAY_ACCESS_TOKEN": "r2",
        "RAILWAY_TOKEN": "r3", "RAILWAY_API_TOKEN": "r4",
        "OPENROUTER_MANAGEMENT_KEY": "m",
        "SCREENER_BOT_TOKEN": "b1", "US_SCREENER_BOT_TOKEN": "b2",
        "SCREENER_CHART_BOT_TOKEN": "b3", "IDEA_BOT_TOKEN": "b4",
        "EARNINGS_BOT_TOKEN": "b5", "RECAP_BOT_TOKEN": "b6", "REPORT_BOT_TOKEN": "b7",
        "DART_API_KEY": "d", "FMP_API_KEY": "f", "ALPHA_VANTAGE_KEY": "a",
        "FINNHUB_API_KEY": "fh",
    }
    kept = {
        "PATH": "/usr/bin", "HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1", "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
        "TZ": "Asia/Seoul", "STATE_DIR": "/data", "RAILWAY_VOLUME_MOUNT_PATH": "/data",
        "IDEA_NARROW_MODEL": "m1", "OPENROUTER_API_KEY": "k",
        "OPENROUTER_AUTH_COOLDOWN_S": "60", "TELEGRAM_BOT_TOKEN": "t",
        "WISEREPORT_ID": "i", "WISEREPORT_PW": "p",
    }
    child = _pipeline_child_env({**excluded, **kept})
    assert set(child) == set(kept)
    # 기본 인자 = 현재 os.environ
    monkeypatch.setenv("TG_SESSION_STRING", "zzz")
    assert "TG_SESSION_STRING" not in _pipeline_child_env()
    assert "OPENROUTER_MANAGEMENT_KEY" not in _pipeline_child_env()
    assert "SCREENER_BOT_TOKEN" not in _pipeline_child_env()


class _Msg:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

    async def reply_text(self, text, **kw):
        self.sink.append(text)
        return self


class _Update:
    def __init__(self, sink: list[str]) -> None:
        self.message = _Msg(sink)


class _Proc:
    returncode = 1

    def __init__(self, out: bytes) -> None:
        self._out = out

    async def communicate(self):
        return self._out, None


def test_run_pipeline_child_env_and_redacted_error_tail(fake_env, monkeypatch):
    import src.bot_worker as bw
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "5000000001:CCcompanyBotToken_abcdefghijklmnopq")
    captured: dict = {}
    tail = (
        "Traceback (most recent call last):\n"
        f"httpx.HTTPStatusError: 401 for https://api.telegram.org/bot{FAKE_BOT_TOKEN}/sendDocument\n"
        f"openai.AuthenticationError: key {fake_env['OPENROUTER_API_KEY']}\n"
        f"wisereport login {fake_env['WISEREPORT_ID']} / {fake_env['WISEREPORT_PW']}\n"
    )

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw["env"]
        return _Proc(tail.encode())

    monkeypatch.setattr(bw.asyncio, "create_subprocess_exec", fake_exec)
    sink: list[str] = []
    asyncio.run(bw._run_pipeline(_Update(sink), "삼성전자", "005930", 3))

    env = captured["env"]
    for k in ("TG_SESSION_STRING", "TG_API_HASH", "TG_LOGIN_PASSWORD",
              "OPENROUTER_MANAGEMENT_KEY", "SCREENER_BOT_TOKEN", "DART_API_KEY"):
        assert k not in env
    for k in ("WISEREPORT_ID", "WISEREPORT_PW", "OPENROUTER_API_KEY",
              "OPENROUTER_MODEL", "TELEGRAM_BOT_TOKEN"):
        assert k in env
    fail_msg = sink[-1]
    assert "실패 (exit 1)" in fail_msg
    _assert_no_secret(fail_msg, fake_env)
    assert "‹OPENROUTER_API_KEY›" in fail_msg
