"""부팅 1회 자격증명 점검 → 관리자 메시지 '🔐 자격증명 점검' 1건 (2026-10-21 Railway env 유출 후속).

사고 후 모든 키·토큰을 교체하고 재배포할 때 "새 값이 실제로 살아있나"를 사람 손 없이 확인.
orchestrator가 스케줄러 시작 직후 1회 실행 (`CRED_SELF_CHECK=0`이면 끔).

원칙 (절대 깨지 말 것):
  - 값은 절대 출력 안 함 — env 이름 + ✓/✗/–/⚠ + (✓일 때) fingerprint(sha256 앞 8자리)만
  - 토큰·크레딧 소모 없는 엔드포인트만 (OpenRouter /key, OpenAI /models, DART company.json 1건 …)
    Alpha Vantage는 일 25회 한도라 호출 안 함, Telethon 세션은 MTProto 2중 접속 방지로 존재만 표기
  - 프로브마다 10초 timeout, 전부 병렬, 어떤 실패도 raise 안 함
  - 키가 URL에 들어가는 프로브(텔레그램 getMe·DART·FMP)가 있으므로 URL·예외 메시지는
    로그·메시지에 남기지 않음 — 예외는 타입 이름만. 헤더 인증 가능한 곳은 헤더 사용

공개 API:
  - run_credential_check(bot_objects, failed_bots, *, bot_token_envs=None, fail_reasons=None) -> str
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

try:
    from src.log_redact import fingerprint as _fingerprint
    from src.log_redact import is_secret_name as _is_secret_name
    from src.log_redact import redact as _redact
except ImportError:  # 병행 작업 중 모듈 부재 — 점검 자체는 계속 동작해야 함
    import hashlib

    def _fingerprint(value: str) -> str:
        return hashlib.sha256((value or "").encode("utf-8", "replace")).hexdigest()[:8]

    def _is_secret_name(name: str) -> bool:
        return bool(re.search(r"KEY|TOKEN|SECRET|PASS|SESSION", name or "", re.IGNORECASE))

    def _redact(text: str) -> str:
        return text

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 10.0

OK, FAIL, SKIP, WARN = "✓", "✗", "–", "⚠"

HEADER = "🔐 자격증명 점검"

# 차트 채널 게시 전용 봇 — orchestrator가 polling하지 않으므로 getMe로 직접 확인
CHART_BOT_TOKEN_ENVS = ("SCREENER_CHART_BOT_TOKEN", "US_SCREENER_CHART_BOT_TOKEN")

# DART 프로브 고정 corp_code (삼성전자) — 응답 내용은 안 씀, status만 확인
DART_PROBE_CORP_CODE = "00126380"

RAILWAY_TOKEN_WARNING = "런타임에 Railway 토큰 존재 (유출 시 전체 env 장악 — 불필요하면 삭제 권장)"


@dataclass
class Line:
    mark: str
    name: str
    detail: str = ""

    def render(self) -> str:
        return f"{self.mark} {self.name}" + (f" — {self.detail}" if self.detail else "")


# ------------------------------------------------------------------
# HTTP — 테스트에서 이 함수만 monkeypatch
# ------------------------------------------------------------------
async def _http_get(url: str, *, headers: dict | None = None, params: dict | None = None,
                    timeout: float = PROBE_TIMEOUT_S) -> tuple[int, object]:
    """GET → (HTTP status, JSON body 또는 None). 네트워크 오류는 예외 그대로 전파
    (호출측이 타입 이름만 기록). 리다이렉트 미추종 — 키 붙은 요청이 다른 호스트로 새지 않게."""
    import httpx
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.get(url, headers=headers, params=params)
        try:
            body = resp.json()
        except Exception:
            body = None
        return resp.status_code, body


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _fp(value: str) -> str:
    try:
        return _fingerprint(value)
    except Exception:
        return "?"


def _safe_text(value: object, limit: int = 40) -> str:
    """외부 응답 문자열(라벨·username)을 메시지에 넣기 전 정리 — 마스킹 + 한 줄 + 길이 제한."""
    try:
        s = " ".join(str(value).split())
        return _redact(s)[:limit]
    except Exception:
        return ""


def _fmt_usd(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _http_fail(name: str, status: int) -> Line:
    if status in (401, 403):
        return Line(FAIL, name, f"키 거부 (HTTP {status})")
    if status == 429:
        return Line(SKIP, name, "요청 한도 초과 (HTTP 429) — 유효성 미확인")
    return Line(FAIL, name, f"HTTP {status}")


# ------------------------------------------------------------------
# 프로브 — 각자 Line 반환 (미설정이면 SKIP 라인), 예외는 _guard가 처리
# ------------------------------------------------------------------
async def _probe_telegram(name: str) -> Line:
    token = _env(name)
    # 텔레그램은 헤더 인증 불가 — URL에 토큰. URL은 어디에도 기록 안 함
    status, body = await _http_get(f"https://api.telegram.org/bot{token}/getMe")
    if status == 200 and isinstance(body, dict) and body.get("ok"):
        uname = _safe_text(((body.get("result") or {}).get("username")) or "")
        who = f"@{uname}, " if uname else ""
        return Line(OK, name, f"{who}fp={_fp(token)}")
    if status in (401, 404):
        return Line(FAIL, name, f"토큰 거부 (HTTP {status})")
    return _http_fail(name, status)


async def _probe_openrouter(name: str) -> Line:
    key = _env(name)
    # /key — 키 메타데이터 조회, 토큰 소모 없음
    status, body = await _http_get("https://openrouter.ai/api/v1/key",
                                   headers={"Authorization": f"Bearer {key}"})
    if status != 200:
        return _http_fail(name, status)
    data = body.get("data") if isinstance(body, dict) else None
    data = data if isinstance(data, dict) else {}
    parts = [f"fp={_fp(key)}"]
    label = _safe_text(data.get("label") or "")
    if label:
        parts.append(f"label={label}")
    limit = data.get("limit")
    if limit is None:
        parts.append("한도 없음 ⚠")
    else:
        lim = f"한도 {_fmt_usd(limit)}"
        remaining = data.get("limit_remaining")
        if remaining is not None:
            lim += f" (잔여 {_fmt_usd(remaining)})"
        parts.append(lim)
    return Line(OK, name, ", ".join(parts))


async def _probe_openai(name: str) -> Line:
    key = _env(name)
    status, _ = await _http_get("https://api.openai.com/v1/models",
                                headers={"Authorization": f"Bearer {key}"})
    if status == 200:
        return Line(OK, name, f"fp={_fp(key)}")
    return _http_fail(name, status)


async def _probe_dart(name: str) -> Line:
    key = _env(name)
    # OpenDART는 쿼리 파라미터 인증만 지원 — URL은 기록 안 함
    status, body = await _http_get(
        "https://opendart.fss.or.kr/api/company.json",
        params={"crtfc_key": key, "corp_code": DART_PROBE_CORP_CODE},
    )
    if status != 200:
        return _http_fail(name, status)
    code = str(body.get("status", "")) if isinstance(body, dict) else ""
    if code == "000":
        return Line(OK, name, f"fp={_fp(key)}")
    # 상태 메시지는 생략 — 코드만 (010 미등록 키, 011 사용불가 키, 020 한도 초과 …)
    return Line(FAIL, name, f"status {_safe_text(code, 8) or '?'}")


async def _probe_finnhub(name: str) -> Line:
    key = _env(name)
    status, _ = await _http_get("https://finnhub.io/api/v1/quote",
                                headers={"X-Finnhub-Token": key},
                                params={"symbol": "AAPL"})
    if status == 200:
        return Line(OK, name, f"fp={_fp(key)}")
    return _http_fail(name, status)


async def _probe_fmp(name: str) -> Line:
    key = _env(name)
    # 기존 코드(report/data/fetch_market_caps)와 같은 stable/quote — 무료 플랜 허용 엔드포인트
    status, body = await _http_get("https://financialmodelingprep.com/stable/quote",
                                   params={"symbol": "AAPL", "apikey": key})
    if status == 200 and not (isinstance(body, dict) and body.get("Error Message")):
        return Line(OK, name, f"fp={_fp(key)}")
    if status == 200:
        return Line(FAIL, name, "오류 응답 (HTTP 200)")
    return _http_fail(name, status)


# (env 이름, 프로브) — 순서 = 메시지 표시 순서
API_PROBES: tuple[tuple[str, Callable[[str], Awaitable[Line]]], ...] = (
    ("OPENROUTER_API_KEY", _probe_openrouter),
    ("OPENAI_API_KEY", _probe_openai),
    ("DART_API_KEY", _probe_dart),
    ("FINNHUB_API_KEY", _probe_finnhub),
    ("FMP_API_KEY", _probe_fmp),
)


async def _guard(name: str, probe: Callable[[str], Awaitable[Line]]) -> Line:
    """프로브 1개 실행 — timeout·예외를 ✗ 라인으로. 예외 메시지(URL 포함 가능)는 버림."""
    try:
        return await asyncio.wait_for(probe(name), timeout=PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return Line(FAIL, name, f"시간 초과 ({PROBE_TIMEOUT_S:g}s)")
    except Exception as e:
        return Line(FAIL, name, f"연결 실패 ({type(e).__name__})")


# ------------------------------------------------------------------
# 텔레그램 봇 — orchestrator 기동 결과 재사용 (getMe 중복 호출 안 함)
# ------------------------------------------------------------------
def _bot_username(bot) -> str:
    try:
        return _safe_text(getattr(bot, "username", None) or "")
    except Exception:  # PTB: initialize 전 username 접근은 RuntimeError
        return ""


def _bot_lines(bot_objects: dict, failed_bots: list[str],
               bot_token_envs: dict[str, str] | None,
               fail_reasons: dict[str, str] | None) -> list[Line]:
    envs = dict(bot_token_envs or {})
    reasons = fail_reasons or {}
    order = list(envs)
    for name in list(bot_objects) + list(failed_bots):
        if name not in order:
            order.append(name)
    out: list[Line] = []
    for name in order:
        env_name = envs.get(name) or name
        if name in failed_bots:
            out.append(Line(FAIL, env_name, f"{name}봇 시작 실패 — {reasons.get(name) or '로그 확인'}"))
        elif name in bot_objects:
            token = _env(env_name) if name in envs else ""
            if not token:
                token = str(getattr(bot_objects[name], "token", "") or "")
            uname = _bot_username(bot_objects[name])
            who = f" @{uname}" if uname else ""
            out.append(Line(OK, env_name, f"{name}봇{who}, fp={_fp(token)}"))
        elif name in envs and _env(env_name):
            # 토큰은 있는데 이 서비스에서 안 띄움 (ACTIVE_BOTS 필터·빌더 실패) — 노출면만 늘림
            out.append(Line(SKIP, env_name, f"{name}봇 이 서비스에서 미기동 (불필요하면 삭제 권장)"))
    return out


def _railway_token_names() -> list[str]:
    return sorted(k for k in os.environ
                  if "RAILWAY" in k.upper() and _is_secret_name(k) and _env(k))


async def _build(bot_objects: dict, failed_bots: list[str],
                 bot_token_envs: dict[str, str] | None,
                 fail_reasons: dict[str, str] | None) -> str:
    bot_lines = _bot_lines(bot_objects or {}, list(failed_bots or []), bot_token_envs, fail_reasons)

    # 네트워크 프로브 — 설정된 것만, 전부 병렬
    probes: list[tuple[str, Callable[[str], Awaitable[Line]]]] = []
    for name in CHART_BOT_TOKEN_ENVS:
        if _env(name):
            probes.append((name, _probe_telegram))
    api_unset: list[Line] = []
    for name, probe in API_PROBES:
        if _env(name):
            probes.append((name, probe))
        else:
            api_unset.append(Line(SKIP, name, "미설정"))
    results = await asyncio.gather(*(_guard(n, p) for n, p in probes), return_exceptions=True)
    probed: dict[str, Line] = {}
    for (name, _), res in zip(probes, results):
        probed[name] = res if isinstance(res, Line) else Line(FAIL, name, f"점검 오류 ({type(res).__name__})")

    lines: list[Line] = list(bot_lines)
    lines += [probed[n] for n in CHART_BOT_TOKEN_ENVS if n in probed]
    api_lines = [probed[n] for n, _ in API_PROBES if n in probed]

    # 존재만 표기 — 호출하면 안 되는 것들
    extra: list[Line] = []
    if _env("ALPHA_VANTAGE_KEY"):
        extra.append(Line(SKIP, "ALPHA_VANTAGE_KEY", "검증 생략 (일 25회 한도)"))
    if _env("TG_SESSION_STRING"):
        extra.append(Line(SKIP, "TG_SESSION_STRING", "설정됨 — 채널릴레이가 검증"))
    if _env("OPENROUTER_MANAGEMENT_KEY"):
        extra.append(Line(SKIP, "OPENROUTER_MANAGEMENT_KEY", "설정됨 (관리키 — 검증 생략)"))
    warns = [Line(WARN, n, RAILWAY_TOKEN_WARNING) for n in _railway_token_names()]

    parts = [HEADER]
    if lines:
        parts.append("[텔레그램 봇]")
        parts += [ln.render() for ln in lines]
    parts.append("[API 키]")
    parts += [ln.render() for ln in api_lines + api_unset]
    if extra or warns:
        parts.append("[기타]")
        parts += [ln.render() for ln in extra + warns]
    every = lines + api_lines + extra
    n_ok = sum(1 for ln in every if ln.mark == OK)
    n_fail = sum(1 for ln in every if ln.mark == FAIL)
    summary = f"요약: {OK} {n_ok} · {FAIL} {n_fail}"
    if warns:
        summary += f" · {WARN} Railway 토큰 {len(warns)}"
    parts.append(summary)
    return "\n".join(parts)


async def run_credential_check(bot_objects: dict, failed_bots: list[str], *,
                               bot_token_envs: dict[str, str] | None = None,
                               fail_reasons: dict[str, str] | None = None) -> str:
    """자격증명 1회 점검 → 관리자 메시지 텍스트 (plain text, parse_mode 없이 발송).

    bot_objects: 가동 중 봇 {name: telegram.Bot} / failed_bots: 기동 실패 봇 이름
    bot_token_envs: {봇 name: 토큰 env 이름} — 주면 env 이름으로 표시 + 미기동 토큰 경고
    fail_reasons: {봇 name: 짧은 실패 사유} (예: "토큰 거부")
    절대 raise 안 함. 반환 텍스트엔 env 이름·fingerprint·상태만 (값 없음).
    """
    try:
        text = await _build(bot_objects, failed_bots, bot_token_envs, fail_reasons)
    except Exception as e:
        log.warning("[cred_check] 점검 자체 실패 (%s)", type(e).__name__)
        return f"{HEADER}\n점검 실패 ({type(e).__name__}) — 로그 확인"
    # 방어선 — 혹시 값이 섞였어도 env 정확일치·패턴 마스킹
    try:
        return _redact(text)
    except Exception:
        return text
