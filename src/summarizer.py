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
from dataclasses import dataclass
from pathlib import Path

from openai import APIStatusError, OpenAI
from pypdf import PdfReader

log = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")

# pypdf로 추출한 텍스트의 최대 길이 (모델 컨텍스트 보호)
MAX_PDF_TEXT_CHARS = 80_000

# 요약 결과물 최대 글자수 (한국어 char 기준)
MAX_SUMMARY_CHARS = 5_000
MAX_SUMMARY_CHARS_SHORT = 1_000  # 카테고리(산업/시황/글로벌) 봇용 짧은 요약


class OpenRouterCreditExhausted(RuntimeError):
    """OpenRouter 토큰이 부족하거나 결제 거부된 경우."""

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


def get_client() -> OpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY 환경변수가 없습니다. .env에 추가하세요."
        )
    return OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": "https://github.com/anselmkim111/basketball-rag-agent",
            "X-Title": "wisereport-auto-downloader",
        },
    )


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
    """OpenRouter 결제·잔액 오류 판별."""
    if isinstance(exc, APIStatusError):
        if exc.status_code in (402, 429):
            msg = (str(exc) + " " + str(getattr(exc, "body", ""))).lower()
            if any(k in msg for k in ("credit", "balance", "payment", "insufficient", "quota")):
                return True
    return False


def _trim_to_chars(text: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """모델이 5000자 초과로 응답한 경우 안전 가드 (보통 발생 안 함)."""
    if len(text) <= max_chars:
        return text
    truncated = text[: max_chars - 60]
    return truncated.rstrip() + "\n\n... (5000자 제한으로 잘림)"


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

    try:
        # 5000자 ≈ 한국어 기준 ~3500 토큰. 여유 4000.
        resp = client.chat.completions.create(
            model=model,
            max_tokens=4000,
            messages=[
                {"role": "system", "content": INDIVIDUAL_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
    except APIStatusError as e:
        if _is_credit_error(e):
            raise OpenRouterCreditExhausted(str(e)) from e
        raise

    summary = _trim_to_chars(resp.choices[0].message.content or "")
    log.info(
        "요약 완료: %s (in=%s, out=%s, chars=%d)",
        pdf_path.name,
        getattr(resp.usage, "prompt_tokens", "?"),
        getattr(resp.usage, "completion_tokens", "?"),
        len(summary),
    )
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
    try:
        # 1000자 ≈ 800 토큰. max_tokens=1200 여유.
        resp = client.chat.completions.create(
            model=model,
            max_tokens=1200,
            messages=[
                {"role": "system", "content": SHORT_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
    except APIStatusError as e:
        if _is_credit_error(e):
            raise OpenRouterCreditExhausted(str(e)) from e
        raise
    summary = _trim_to_chars(resp.choices[0].message.content or "", MAX_SUMMARY_CHARS_SHORT)
    log.info(
        "짧은 요약 완료: %s (in=%s, out=%s, chars=%d)",
        pdf_path.name,
        getattr(resp.usage, "prompt_tokens", "?"),
        getattr(resp.usage, "completion_tokens", "?"),
        len(summary),
    )
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
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=4000,
            messages=[
                {"role": "system", "content": COMBINED_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
    except APIStatusError as e:
        if _is_credit_error(e):
            raise OpenRouterCreditExhausted(str(e)) from e
        raise
    return _trim_to_chars(resp.choices[0].message.content or "")


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
