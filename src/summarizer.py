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

from openai import OpenAI
from pypdf import PdfReader

log = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")

# pypdf로 추출한 텍스트의 최대 길이 (모델 컨텍스트 보호)
MAX_PDF_TEXT_CHARS = 80_000

INDIVIDUAL_SYSTEM_PROMPT = """당신은 한국 기업 분석 리포트를 정밀하게 요약하는 금융 애널리스트입니다.

증권사 또는 기관에서 발행한 PDF 리포트 1건의 추출 텍스트를 받아 한국어로 요약합니다.
다음 항목을 빠짐없이 작성하되, 리포트에 없는 항목은 "해당 없음"으로 표기하세요.

# 요약 형식

## 1. 기본 정보
- 발행일 / 발행 기관 / 작성자
- 분석 대상 기업 (종목코드 포함)
- 투자의견 / 목표주가 / 현재가 (있는 경우)

## 2. 핵심 결론 (3-5줄)
리포트가 전달하려는 가장 중요한 메시지를 압축해서 작성.

## 3. 실적 및 재무 하이라이트
- 최근 분기/연간 실적 (매출, 영업이익, 순이익 - 전년 동기 대비 증감 포함)
- 주요 재무 비율 변화
- 가이던스/컨센서스 대비 결과

## 4. 사업/시장 분석
- 주요 사업부문별 동향
- 시장 환경 / 경쟁 구도 / 산업 사이클

## 5. 투자 포인트 (강세 요인)
- 리포트가 강조하는 긍정 요인 3-5개

## 6. 리스크 요인
- 리포트가 지적하는 우려 사항 / 하방 요인

## 7. 밸류에이션 / 추정치
- PER, PBR, EV/EBITDA 등 적용 멀티플
- 향후 1-2년 추정 매출/영업이익

## 8. 기타 특이사항
- 업종 비교, 경쟁사 언급, 기술적 분석 등 위 항목에 안 들어간 중요 내용

원문(텍스트 추출이라 표/그림 일부 누락 가능)에 없는 정보를 추측해서 채우지 마세요.
숫자는 원문 그대로 인용하고 단위(억 원, 조 원, %)를 명확히 표기하세요."""

COMBINED_SYSTEM_PROMPT = """당신은 한국 기업 분석 리포트를 종합하는 시니어 애널리스트입니다.

같은 기업에 대한 여러 증권사/기관의 리포트 요약본을 받아 한국어로 종합 분석을 작성합니다.

# 종합 분석 형식

## 1. 컨센서스 요약
- 분석 대상 기업명 / 분석 리포트 수
- 분석 기간 (가장 오래된 리포트 ~ 가장 최근 리포트)
- 발행 기관 리스트
- 평균/중간 목표주가 (있는 경우 - 단순 산술평균과 범위)
- 투자의견 분포 (매수/중립/매도 카운트)

## 2. 합의된 시각 (Bullish Consensus)
여러 리포트가 공통적으로 강조한 긍정 포인트를 묶어서 정리.
각 항목 뒤에 [언급한 리포트 번호 #1, #3, #5] 식으로 출처 표기.

## 3. 공통된 우려 (Common Risks)
여러 리포트가 공통적으로 지적한 리스크.
각 항목 뒤에 출처 번호 표기.

## 4. 견해 차이가 있는 쟁점
리포트마다 평가가 다른 이슈를 정리하고, 어떤 리포트가 어떤 입장을 취하는지 명시.

## 5. 시간 경과에 따른 변화
가장 오래된 리포트와 가장 최근 리포트를 비교하여 시각이 어떻게 바뀌었는지 분석.
(목표주가 상향/하향, 투자의견 변경, 새롭게 부각된 이슈 등)

## 6. 핵심 모니터링 포인트
이 기업을 추적할 때 앞으로 주목해야 할 변수 3-5개.

## 7. 종합 결론
모든 리포트를 통합한 시각에서 이 기업의 현재 상황과 향후 전망을 5-7줄로 요약."""


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
        "시스템 프롬프트의 형식대로 요약해주세요.\n\n"
        f"<pdf_text>\n{text}\n</pdf_text>"
    )

    resp = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": INDIVIDUAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    summary = resp.choices[0].message.content or ""
    log.info(
        "요약 완료: %s (in=%s, out=%s)",
        pdf_path.name,
        getattr(resp.usage, "prompt_tokens", "?"),
        getattr(resp.usage, "completion_tokens", "?"),
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
        "시스템 프롬프트의 형식대로 종합 분석을 작성해주세요.\n\n"
        f"{bundled_text}"
    )
    resp = client.chat.completions.create(
        model=model,
        max_tokens=6000,
        messages=[
            {"role": "system", "content": COMBINED_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    return resp.choices[0].message.content or ""


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
