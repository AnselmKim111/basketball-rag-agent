"""다운로드된 PDF 리포트를 OpenRouter (Claude 등) 로 요약.

사용자가 OpenRouter API 키를 제공하므로 OpenAI-compatible 게이트웨이를 사용.
PDF는 pypdf로 텍스트 추출 후 LLM에 전달 (OpenAI-compat API는 native PDF
입력을 모델에 따라 다르게 지원하므로 텍스트가 가장 안정적).

- 각 PDF별 개별 요약
- 10개 요약을 묶은 종합 요약
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from openai import APIStatusError, OpenAI
from pypdf import PdfReader

log = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Summary tier 기본 — CLAUDE.md §2: 요약·추출을 sonnet으로 올리지 말 것.
# (이전 기본 sonnet-4.5는 규칙 위반 — env 미설정 환경에서 6-30x 과금 위험)
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")

# pypdf로 추출한 텍스트의 최대 길이 (모델 컨텍스트 보호)
MAX_PDF_TEXT_CHARS = 80_000

# 요약 결과물 최대 글자수 (한국어 char 기준)
MAX_SUMMARY_CHARS = 5_000
MAX_SUMMARY_CHARS_SHORT = 1_000  # 카테고리(산업/시황/글로벌) 봇용 짧은 요약


class OpenRouterCreditExhausted(RuntimeError):
    """OpenRouter 토큰이 부족하거나 결제 거부된 경우."""


class OpenRouterAuthError(OpenRouterCreditExhausted):
    """OpenRouter 401 — API 키 비활성/폐기/미존재 ('User not found.').

    OpenRouterCreditExhausted 하위 클래스: 기존 핸들러(~25곳)가 그대로 단락한다
    (재시도·폴백 없이 사용자 안내). 2026-10-21 Railway env 유출 → 키 disable 사고 후속.
    """

INDIVIDUAL_SYSTEM_PROMPT = """당신은 한국 기업 분석 리포트를 정밀하게 요약하는 시니어 애널리스트입니다.

증권사/기관 PDF 리포트 1건의 추출 텍스트를 받아 한국어로 요약합니다.

# 출력 규칙 (엄수)
- **전체 5000자 이내** (한국어 글자수 기준, 공백 포함). 절대 초과 금지.
- 핵심만 정확하게. 부연 설명 금지. 표/번호 매김 활용으로 압축.
- 원문에 없는 정보 추측 금지. 숫자는 원문 그대로 인용 + 단위 명시.

# 형식
## 1. 기본 정보
- 발행일 / 기관 / 작성자 / 종목코드
- 투자의견 / 목표주가 / 현재가 (있는 경우)

## 2. 핵심 결론 (2-3줄)
가장 중요한 메시지만 압축.

## 3. 실적 / 재무 하이라이트
최근 실적, 주요 재무 변화, 컨센서스 대비 결과 — 숫자 위주.

## 4. 투자 포인트
긍정 요인 핵심 3개.

## 5. 리스크
부정 요인 핵심 2-3개.

## 6. 밸류에이션
PER/PBR/EV-EBITDA 등 멀티플과 추정치 (있는 경우).

리포트에 없는 항목은 "해당 없음" 한 줄로 처리하고 추측하지 말 것."""

SHORT_SYSTEM_PROMPT = """당신은 한국 증권사·기관 리서치 리포트를 한국어 한 단락으로 요약하는 시니어 애널리스트입니다.

# 출력 규칙 (엄수)
- **전체 1000자 이내** (한국어 글자수, 공백 포함). 절대 초과 금지.
- 최대 5-7문장. bullet 1-2개 정도면 OK.
- 핵심 메시지 + 가장 중요한 숫자/근거 + 핵심 결론.
- 원문에 없는 정보 추측 금지. 숫자는 단위 명시 (조원/억원/%).

# 형식 예시
[발행: 기관 / 작성자 / 날짜]
**핵심**: 한 줄 요약.
- 주요 근거 1 (숫자 포함)
- 주요 근거 2 (숫자 포함)
**결론**: 투자 시사점 1-2줄."""


COMBINED_SYSTEM_PROMPT = """당신은 한국 기업 분석 리포트를 종합하는 시니어 애널리스트입니다.

같은 기업에 대한 여러 증권사/기관 리포트 요약본을 받아 한국어로 종합 분석을 작성합니다.

# 출력 규칙 (엄수)
- **전체 5000자 이내** (한국어 글자수 기준, 공백 포함). 절대 초과 금지.
- 핵심만. 압축적 표현 권장.
- 출처 표기 필수: 각 주장 뒤에 [#1, #3] 형태로.

# 형식
## 1. 컨센서스 요약 (1-2줄)
기업명, 리포트 수, 기간, 평균 목표주가(범위), 투자의견 분포.

## 2. 합의된 강세 시각
공통 긍정 포인트 핵심 3-4개 (각 1-2줄 + 출처).

## 3. 공통 우려
공통 리스크 핵심 2-3개 (각 1-2줄 + 출처).

## 4. 견해 차이
리포트마다 평가 갈리는 이슈 1-2개와 양측 입장.

## 5. 시간 변화
초기 리포트 vs 최근 리포트 비교 — 목표가/의견/관점 변화.

## 6. 모니터링 포인트
앞으로 주목할 핵심 변수 3개.

## 7. 종합 결론 (3-4줄)
모든 리포트 통합 시각으로 현재 상황과 전망."""


@dataclass
class IndividualSummary:
    pdf_path: Path
    summary_text: str


# get_client 모듈 캐시 — OpenAI 클라이언트는 thread-safe이고 내부 httpx pool을
# 재사용하므로 호출마다 새로 만들면 TLS handshake·pool 비용만 낭비.
_CLIENT_CACHE: OpenAI | None = None


def get_client() -> OpenAI:
    global _CLIENT_CACHE
    if _CLIENT_CACHE is not None:
        return _CLIENT_CACHE
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY 환경변수가 없습니다. .env에 추가하세요."
        )
    # OpenRouter 쪽 일부 모델(xiaomi/mimo 등)이 stream idle timeout으로 partial response를
    # 자주 흘림. 명시적 timeout(180s) + 자동 재시도(2회)로 transient 실패 흡수.
    _CLIENT_CACHE = OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        timeout=180.0,
        max_retries=2,
        default_headers={
            "HTTP-Referer": "https://github.com/anselmkim111/basketball-rag-agent",
            "X-Title": "wisereport-auto-downloader",
        },
    )
    return _CLIENT_CACHE


def _extract_pdf_text(pdf_path: Path, max_chars: int = MAX_PDF_TEXT_CHARS) -> str:
    """PDF 텍스트 추출 (페이지 경계 표시 포함)."""
    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    total = 0
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        chunk = f"[Page {i}]\n{text}"
        parts.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            parts.append(f"\n... (이후 {len(reader.pages) - i}페이지 생략)")
            break
    return "\n\n".join(parts)


def _is_credit_error(exc: Exception) -> bool:
    """OpenRouter 결제·잔액·키 한도 오류 판별.

    - 402: Payment Required — 메시지 무관 항상 (잔액 0 등)
    - 429: payment / quota 관련 메시지 (단순 rate limit은 재시도 대상이라 제외)
    - 403: 'Key limit exceeded' (사용자 키의 spending limit 초과)
    401(키 무효)은 `_is_auth_error` — 별도 breaker로 처리.
    """
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        msg = (str(exc) + " " + str(getattr(exc, "body", ""))).lower()
        if status == 402:
            return True
        if status == 429:
            if any(k in msg for k in ("credit", "balance", "payment", "insufficient", "quota")):
                return True
        if status == 403:
            if any(k in msg for k in ("key limit exceeded", "spending limit", "limit exceeded")):
                return True
    return False


def _is_auth_error(exc: Exception) -> bool:
    """OpenRouter 401 — 키 비활성/폐기 ('User not found.'). 403(한도)은 해당 없음."""
    return isinstance(exc, APIStatusError) and getattr(exc, "status_code", None) == 401


# ------------------------------------------------------------------
# 401 인증 breaker — 키가 죽으면 모든 LLM 호출을 cooldown 동안 네트워크 없이 차단
# ------------------------------------------------------------------
# 2026-10-21 사고: 키 disable(401)인데 chat_with_retry가 3회(2s/4s backoff, 3차는 같은
# 키로 fallback 모델) 재시도 → 401마다 모델 health 실패로 집계 → Layer D가 멀쩡한 모델을
# 롤백 시도. 401은 모델이 아니라 키 문제라 재시도·폴백·health 기록 모두 무의미.
# 상태는 프로세스 단위 (봇 전부 한 프로세스 — orchestrator). cooldown 만료 후 다음 호출이
# 네트워크로 재확인(half-open) — 또 401이면 재차단, 성공하면 즉시 해제.
AUTH_COOLDOWN_DEFAULT_S = 900
# 같은 장애가 이어지는 동안 관리자 알림 재발송 간격 (cooldown마다 재차단돼도 스팸 방지)
AUTH_REALERT_S = 6 * 3600

_AUTH_LOCK = threading.Lock()
_auth_open_until: float = 0.0          # _clock() 기준. 0 = 닫힘
_auth_last_trip: float | None = None   # 마지막 차단 시각 (Layer D 가드용)
_auth_alerted_at: float | None = None  # 마지막 관리자 알림 시각 (성공 호출 시 None)
_auth_alert_hook: Callable[[str], None] | None = None


def _clock() -> float:
    """breaker 시계 (monotonic). 테스트에서 monkeypatch로 시간 이동."""
    return time.monotonic()


def _auth_cooldown_s() -> float:
    try:
        return max(1.0, float(os.getenv("OPENROUTER_AUTH_COOLDOWN_S", str(AUTH_COOLDOWN_DEFAULT_S))))
    except ValueError:
        return float(AUTH_COOLDOWN_DEFAULT_S)


def set_auth_alert_hook(fn: Callable[[str], None] | None) -> None:
    """401 차단 시 관리자 알림 hook 등록 (None이면 해제).

    차단 1회당 1번만 호출 (호출마다 아님). 같은 장애가 이어지면 AUTH_REALERT_S마다 재알림.
    LLM 호출 스레드(executor)에서 동기 호출되므로 fn은 빨리 반환해야 함 — 텔레그램 발송은
    asyncio.run_coroutine_threadsafe 등으로 넘길 것. fn 예외는 삼킴.
    """
    global _auth_alert_hook
    with _AUTH_LOCK:
        _auth_alert_hook = fn


def trip_auth_breaker(exc: Exception | None = None) -> None:
    """401 감지 → breaker 열기 (cooldown). 관리자 hook은 장애당 1회."""
    global _auth_open_until, _auth_last_trip, _auth_alerted_at
    cooldown = _auth_cooldown_s()
    hook = None
    with _AUTH_LOCK:
        now = _clock()
        was_open = _auth_open_until > now
        _auth_open_until = now + cooldown
        _auth_last_trip = now
        if _auth_alert_hook is not None and (
                _auth_alerted_at is None or now - _auth_alerted_at >= AUTH_REALERT_S):
            _auth_alerted_at = now
            hook = _auth_alert_hook
    if not was_open:
        # 서버 메시지는 로그에만 (사용자·텔레그램 메시지엔 고정 문구) — 키 값은 포함 안 됨
        detail = str(exc)[:160] if exc is not None else ""
        log.error("[auth_breaker] OpenRouter 401 — LLM 호출 %d초 차단 (%s)", int(cooldown), detail)
    if hook is not None:
        mins = max(1, round(cooldown / 60))
        msg = (
            "🔑 OpenRouter 인증 실패(401) — API 키 비활성/폐기 의심. "
            f"LLM 호출을 {mins}분간 중단합니다. "
            "새 키를 OPENROUTER_API_KEY에 넣고 재배포하세요."
        )
        try:
            hook(msg)
        except Exception:
            log.exception("[auth_breaker] 관리자 알림 hook 실패 (무시)")


def auth_breaker_open() -> bool:
    """breaker 열림(cooldown 중) 여부. 만료되면 False → 다음 호출이 네트워크 재확인."""
    with _AUTH_LOCK:
        return _auth_open_until > _clock()


def auth_breaker_tripped_within(seconds: float) -> bool:
    """최근 seconds 안에 401 차단이 있었는지 (Layer D 롤백 가드용)."""
    with _AUTH_LOCK:
        return _auth_last_trip is not None and _clock() - _auth_last_trip <= seconds


def reset_auth_breaker() -> None:
    """breaker 상태 초기화 (테스트용). hook 등록은 유지."""
    global _auth_open_until, _auth_last_trip, _auth_alerted_at
    with _AUTH_LOCK:
        _auth_open_until = 0.0
        _auth_last_trip = None
        _auth_alerted_at = None


def _auth_breaker_on_success() -> None:
    """정상 응답 수신 → 키 살아있음: breaker 즉시 닫고 알림 상태 해제 (다음 장애는 새로 알림)."""
    global _auth_open_until, _auth_alerted_at
    if not _auth_open_until and _auth_alerted_at is None:
        return  # fast path — 평상시 lock 생략
    with _AUTH_LOCK:
        _auth_open_until = 0.0
        _auth_alerted_at = None


def _auth_error_message() -> str:
    return ("OpenRouter 인증 실패(401) — API 키 비활성/폐기 의심. "
            "새 키를 OPENROUTER_API_KEY에 넣고 재배포하세요.")


def raise_if_auth_breaker_open(context: str = "") -> None:
    """breaker 열려 있으면 네트워크 없이 OpenRouterAuthError. 직접 client를 쓰는 호출부용."""
    if auth_breaker_open():
        log.info("[auth_breaker] 차단 중 — LLM 호출 skip [%s]", context)
        raise OpenRouterAuthError(_auth_error_message() + " (차단 중)")


def raise_for_openrouter_error(exc: Exception) -> None:
    """직접 client.chat.completions.create를 쓰는 호출부의 except 블록용.

    401 → breaker 열고 OpenRouterAuthError, 결제/한도 → OpenRouterCreditExhausted,
    그 외 no-op (호출부 기존 처리 계속).
    """
    if _is_auth_error(exc):
        trip_auth_breaker(exc)
        raise OpenRouterAuthError(_auth_error_message()) from exc
    if _is_credit_error(exc):
        raise OpenRouterCreditExhausted(str(exc)) from exc


def _trim_to_chars(text: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """모델이 5000자 초과로 응답한 경우 안전 가드 (보통 발생 안 함)."""
    if len(text) <= max_chars:
        return text
    truncated = text[: max_chars - 60]
    return truncated.rstrip() + "\n\n... (5000자 제한으로 잘림)"


def _safe_content(resp, *, context: str) -> str:
    """LLM 응답에서 content 추출. 빈 응답이면 placeholder 반환.

    OpenRouter stream idle timeout 등으로 빈 content가 올 수 있는데,
    그대로 호출자에 빈 string을 돌려주면 헤더만 발송되는 버그가 난다.
    """
    try:
        raw = (resp.choices[0].message.content or "").strip()
    except (IndexError, AttributeError):
        log.warning("LLM 응답 구조 비정상 (%s)", context)
        return "(LLM 응답 구조 비정상 — 재시도 필요)"
    if not raw:
        log.warning("LLM이 빈 content 반환 (%s) — stream idle timeout 가능성", context)
        return "(LLM이 빈 응답 반환 — stream idle timeout 가능성. 잠시 후 재시도 권장)"
    return raw


# ------------------------------------------------------------------
# LLM 호출 재시도 + 폴백 모델
# ------------------------------------------------------------------
# 빈 응답 / 일시적 transient 에러 발생 시 application-level retry.
# OpenAI SDK의 max_retries는 HTTP 오류만 재시도하고 빈 content는 잡지 못함.
RETRY_BACKOFF_SECONDS = (2, 4)  # attempt 1 → 2, attempt 2 → 4초


def chat_with_retry(
    client: OpenAI,
    *,
    messages: list[dict],
    max_tokens: int,
    model: str | None = None,
    temperature: float | None = None,
    max_attempts: int = 3,
    fallback_model: str | None = None,
    context: str = "",
    force_json: bool = False,
    validate_schema=None,
) -> str:
    """LLM chat 호출 + 빈 content 자동 재시도 + 폴백 모델.

    동작:
      - 1차/2차 시도: model로 호출. 빈 content 혹은 transient 에러면 backoff 후 재시도.
      - 3차 시도(마지막): fallback_model이 있으면 그걸로, 없으면 동일 model 재호출.
      - 결제 부족(`OpenRouterCreditExhausted`)은 즉시 raise (재시도 의미 없음).
      - 401(키 무효)은 breaker 열고 `OpenRouterAuthError` 즉시 raise — 재시도·폴백·health
        기록 없음. breaker 열려 있으면 네트워크 호출 없이 바로 raise.

    반환:
      - 성공 시 strip()된 content.
      - 모든 시도 실패 시 빈 string "" — 호출자가 placeholder 처리.

    인자:
      - fallback_model 미지정 시 env `OPENROUTER_FALLBACK_MODEL` 사용.
      - context: 로그 식별용 문자열 (예: "deepdive 업의 본질").
      - force_json: True면 response_format={"type":"json_object"} 추가 (Layer C).
        미지원 모델 시 400 에러 → 자동으로 flag off 재시도.
      - validate_schema: pydantic BaseModel 클래스 (Layer B). 응답을 schema 검증해
        실패 시 다음 시도 prompt에 schema 재주입. validate_schema 있으면 force_json
        자동 default True.
    """
    raise_if_auth_breaker_open(context)
    primary = model or DEFAULT_MODEL
    fallback = fallback_model or os.getenv("OPENROUTER_FALLBACK_MODEL") or None
    last_reason = "unknown"
    # Layer B+C: schema 있으면 force_json 자동 True
    if validate_schema is not None and not force_json:
        force_json = True
    use_force_json = force_json
    cur_messages = list(messages)  # schema 재주입 시 copy 수정

    for attempt in range(1, max_attempts + 1):
        # 마지막 시도 + fallback 있으면 모델 교체
        cur_model = fallback if (attempt == max_attempts and fallback) else primary
        kwargs: dict = {"model": cur_model, "max_tokens": max_tokens, "messages": cur_messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if use_force_json:
            kwargs["response_format"] = {"type": "json_object"}

        # Layer D: health 측정 시작
        _t0 = time.time()

        try:
            resp = client.chat.completions.create(**kwargs)
        except APIStatusError as e:
            if _is_auth_error(e):
                # 키 문제 — 재시도·fallback 모델(같은 키)·health 기록(모델 탓 아님) 모두 생략
                trip_auth_breaker(e)
                raise OpenRouterAuthError(_auth_error_message()) from e
            if _is_credit_error(e):
                raise OpenRouterCreditExhausted(str(e)) from e
            status_code = getattr(e, "status_code", None) or 0
            # Layer C: 400 + force_json → 미지원 모델 가능성, flag off 재시도
            if use_force_json and status_code == 400:
                log.info("[chat_with_retry] %s response_format 미지원 — force_json off 재시도", cur_model)
                use_force_json = False
                continue
            last_reason = f"APIStatus {status_code}"
            log.warning(
                "LLM call failed [%s] attempt %d/%d (model=%s): %s",
                context, attempt, max_attempts, cur_model, last_reason,
            )
            _record_health(cur_model, False, time.time() - _t0)
        except Exception as e:
            last_reason = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning(
                "LLM call failed [%s] attempt %d/%d (model=%s): %s",
                context, attempt, max_attempts, cur_model, last_reason,
            )
            _record_health(cur_model, False, time.time() - _t0)
        else:
            try:
                content = (resp.choices[0].message.content or "").strip()
            except (IndexError, AttributeError):
                content = ""
                last_reason = "malformed response"
            if content:
                # Layer B: schema validation (있을 때만)
                if validate_schema is not None:
                    schema_ok, schema_err = _validate_schema_content(content, validate_schema)
                    if not schema_ok:
                        last_reason = f"schema: {schema_err[:120]}"
                        log.warning(
                            "LLM schema fail [%s] attempt %d/%d (model=%s): %s",
                            context, attempt, max_attempts, cur_model, last_reason,
                        )
                        _record_health(cur_model, False, time.time() - _t0)
                        # 다음 시도에 schema 재주입
                        if attempt < max_attempts:
                            _inject_schema(cur_messages, validate_schema, schema_err)
                            sleep_s = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                            time.sleep(sleep_s)
                            continue
                        return ""  # 최종 실패
                if attempt > 1:
                    log.info(
                        "LLM call recovered [%s] attempt %d (model=%s, chars=%d)",
                        context, attempt, cur_model, len(content),
                    )
                else:
                    log.info(
                        "LLM call ok [%s] (model=%s, chars=%d)",
                        context, cur_model, len(content),
                    )
                _record_health(cur_model, True, time.time() - _t0)
                _auth_breaker_on_success()
                return content
            last_reason = "empty content (stream idle timeout 가능성)"
            log.warning(
                "LLM empty content [%s] attempt %d/%d (model=%s)",
                context, attempt, max_attempts, cur_model,
            )
            _record_health(cur_model, False, time.time() - _t0)

        # backoff (마지막 attempt 후엔 안 함)
        if attempt < max_attempts:
            sleep_s = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            time.sleep(sleep_s)

    log.error(
        "LLM call all attempts failed [%s] (max=%d, last=%s)",
        context, max_attempts, last_reason,
    )
    return ""


def _record_health(model: str, ok: bool, elapsed_s: float) -> None:
    """Layer D — health.record_call 호출 wrapper. 모듈 미설치 시 silent skip."""
    try:
        from src.model_router import health
        health.record_call(model, ok, int(elapsed_s * 1000))
    except Exception:
        pass  # health 모듈 미사용 환경 (테스트 등)


def _validate_schema_content(content: str, schema_cls) -> tuple[bool, str]:
    """Layer B — content를 schema로 validate. (ok, err_message) 반환."""
    try:
        from src.llm_json import parse_json_object
        obj = parse_json_object(content)
        if not isinstance(obj, dict):
            return False, "parsed is not dict"
        schema_cls.model_validate(obj)
        return True, ""
    except Exception as e:
        return False, str(e)


def _inject_schema(messages: list[dict], schema_cls, prev_err: str) -> None:
    """Layer B — 다음 시도 user message에 schema 재주입."""
    try:
        schema_json = schema_cls.model_json_schema()
        hint = (f"\n\n[이전 응답이 스키마 실패: {prev_err[:80]}]\n"
                f"반드시 다음 JSON 스키마 따를 것:\n{schema_json}")
        # 마지막 user message에 append
        for msg in reversed(messages):
            if msg.get("role") == "user":
                msg["content"] = (msg.get("content", "") or "") + hint
                return
    except Exception:
        pass


def chat_with_chain(
    client: OpenAI,
    *,
    models: list[str],
    messages: list[dict],
    max_tokens: int,
    temperature: float | None = None,
    context: str = "",
    max_attempts_per_model: int = 2,
) -> tuple[str, str]:
    """모델 chain 따라 순차 호출 — 첫 비어있지 않은 응답 반환.

    사용자가 env로 모델 변경 시 그 모델이 빈 응답을 보내면 자동으로 다음 모델로
    fallback. OpenRouterCreditExhausted는 즉시 raise (credit 부족은 다음 모델도
    동일). OpenRouterAuthError(401, 하위 클래스)도 동일 — breaker 열려 있으면
    네트워크 없이 바로 raise.

    Args:
        models: 시도할 모델 list. 첫 모델부터 순차. 각 모델당
            max_attempts_per_model 번 재시도 (chat_with_retry 내부 로직).
        max_attempts_per_model: 모델당 retry 횟수 (기본 2 — chain에선 빠른 패스).

    Returns:
        (content, used_model). 모두 빈 응답이면 ("", "").
    """
    raise_if_auth_breaker_open(context)
    if not models:
        log.error("[chain] 빈 모델 list (%s)", context)
        return "", ""
    for i, model in enumerate(models):
        chain_ctx = f"{context} chain {i+1}/{len(models)} {model}"
        try:
            content = chat_with_retry(
                client, model=model,
                messages=messages, max_tokens=max_tokens, temperature=temperature,
                max_attempts=max_attempts_per_model,
                fallback_model=None,  # chain이 fallback 역할
                context=chain_ctx,
            )
        except OpenRouterCreditExhausted:
            raise
        except Exception:
            log.exception("[chain] %s 예외 — 다음 모델 시도", model)
            continue
        if content and content.strip():
            if i > 0:
                log.info("[chain] fallback 성공: %s (모델 %d/%d 통과, %d chars)",
                         context, i + 1, len(models), len(content))
            return content, model
        log.warning("[chain] %s 빈 응답 — 다음 모델 시도", model)
    log.error("[chain] 모든 모델 fail (%s): %s", context, ", ".join(models))
    return "", ""


def summarize_pdf(
    client: OpenAI,
    pdf_path: Path,
    model: str = DEFAULT_MODEL,
) -> IndividualSummary:
    log.info("요약 시작: %s", pdf_path.name)
    text = _extract_pdf_text(pdf_path)
    if not text.strip():
        log.warning("PDF 텍스트 추출 실패: %s", pdf_path.name)
        return IndividualSummary(pdf_path=pdf_path, summary_text="(텍스트 추출 실패)")

    user_msg = (
        f"다음은 PDF 리포트({pdf_path.name})에서 추출한 텍스트입니다. "
        "시스템 프롬프트의 형식대로 5000자 이내로 요약해주세요.\n\n"
        f"<pdf_text>\n{text}\n</pdf_text>"
    )

    # 5000자 ≈ 한국어 기준 ~3500 토큰. 여유 4000.
    content = chat_with_retry(
        client,
        model=model,
        max_tokens=4000,
        messages=[
            {"role": "system", "content": INDIVIDUAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        context=f"summarize_pdf {pdf_path.name}",
    )
    if not content:
        return IndividualSummary(
            pdf_path=pdf_path,
            summary_text="(LLM 빈 응답이 3회 연속 — 모델/네트워크 일시 장애. 잠시 후 재시도)",
        )
    summary = _trim_to_chars(content)
    log.info("요약 완료: %s (chars=%d)", pdf_path.name, len(summary))
    return IndividualSummary(pdf_path=pdf_path, summary_text=summary)


def summarize_pdf_short(
    client: OpenAI,
    pdf_path: Path,
    model: str = DEFAULT_MODEL,
) -> IndividualSummary:
    """1000자 이내 짧은 요약. 카테고리(산업/시황/글로벌) 봇용."""
    log.info("짧은 요약 시작: %s", pdf_path.name)
    text = _extract_pdf_text(pdf_path, max_chars=60_000)
    if not text.strip():
        return IndividualSummary(pdf_path=pdf_path, summary_text="(텍스트 추출 실패)")

    user_msg = (
        f"PDF: {pdf_path.name}\n"
        "1000자 이내로 시스템 프롬프트 형식대로 요약해주세요.\n\n"
        f"<pdf_text>\n{text}\n</pdf_text>"
    )
    # 1000자 ≈ 800 토큰. max_tokens=1200 여유.
    content = chat_with_retry(
        client,
        model=model,
        max_tokens=1200,
        messages=[
            {"role": "system", "content": SHORT_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        context=f"summarize_pdf_short {pdf_path.name}",
    )
    if not content:
        return IndividualSummary(
            pdf_path=pdf_path,
            summary_text="(LLM 빈 응답이 3회 연속 — 모델/네트워크 일시 장애. 잠시 후 재시도)",
        )
    summary = _trim_to_chars(content, MAX_SUMMARY_CHARS_SHORT)
    log.info("짧은 요약 완료: %s (chars=%d)", pdf_path.name, len(summary))
    return IndividualSummary(pdf_path=pdf_path, summary_text=summary)


def summarize_combined(
    client: OpenAI,
    company_name: str,
    summaries: list[IndividualSummary],
    model: str = DEFAULT_MODEL,
) -> str:
    log.info("종합 요약 (%d건)", len(summaries))
    bundled_text = "\n\n".join(
        f"=== 리포트 #{i+1}: {s.pdf_path.name} ===\n{s.summary_text}"
        for i, s in enumerate(summaries)
    )
    user_msg = (
        f"분석 대상 기업: {company_name}\n"
        f"아래는 {len(summaries)}건의 리포트 개별 요약본입니다. "
        "시스템 프롬프트의 형식대로 5000자 이내 종합 분석을 작성해주세요.\n\n"
        f"{bundled_text}"
    )
    content = chat_with_retry(
        client,
        model=model,
        max_tokens=4000,
        messages=[
            {"role": "system", "content": COMBINED_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        context=f"summarize_combined {company_name}",
    )
    if not content:
        return "(종합 요약 LLM 빈 응답이 3회 연속 — 모델/네트워크 일시 장애. 잠시 후 재시도)"
    return _trim_to_chars(content)


def write_report(
    output_dir: Path,
    company_name: str,
    individual: list[IndividualSummary],
    combined: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{company_name}_종합리포트.txt"

    parts: list[str] = []
    parts.append("=" * 80)
    parts.append(f"  {company_name} - wisereport 리포트 자동 분석 결과")
    parts.append(f"  분석 리포트: {len(individual)}건")
    parts.append("=" * 80)
    parts.append("")
    parts.append("# [PART 1] 종합 분석")
    parts.append("")
    parts.append(combined)
    parts.append("")
    parts.append("=" * 80)
    parts.append("# [PART 2] 개별 리포트 요약")
    parts.append("=" * 80)

    for i, s in enumerate(individual, start=1):
        parts.append("")
        parts.append("-" * 80)
        parts.append(f"## 리포트 #{i}: {s.pdf_path.name}")
        parts.append("-" * 80)
        parts.append("")
        parts.append(s.summary_text)
        parts.append("")

    out_path.write_text("\n".join(parts), encoding="utf-8")
    log.info("종합 리포트 저장: %s", out_path)
    return out_path
