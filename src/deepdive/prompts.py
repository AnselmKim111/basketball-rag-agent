"""편집 가능한 프롬프트 로더.

prompts/ 디렉터리의 텍스트 파일을 매 호출마다 read.
파일이 없거나 읽기 실패 시 하드코딩된 DEFAULT 폴백 → 절대 None 반환 안 함.

수정 절차: GitHub에서 파일 편집 → push → Railway 자동 재배포 → 다음 호출부터 반영.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# 프로젝트 루트 (Dockerfile WORKDIR /app, COPY prompts ./prompts)
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


DEFAULT_PROMPTS = {
    "deepdive_business": (
        "한국 기업의 사업 본질을 1000자 이내로 정리하라. "
        "사업부 구성·매출 비중·핵심 경쟁력·산업 환경·리스크를 항목별로."
    ),
    "deepdive_ir": (
        "회사 IR자료에서 핵심 메시지·가이던스·투자 포인트를 1000자 이내로 정리하라. "
        "정량 수치와 회사가 직접 강조한 내용 위주로."
    ),
    "deepdive_ir_fallback": (
        "회사 IR PDF가 없어 외부 분석가·산업 리포트(wisereport)만으로 핵심 투자 포인트를 "
        "1000자 이내로 정리하라. 컨센서스 수치·성장 동인·리스크·산업 흐름 위주."
    ),
}


def load(name: str) -> str:
    """프롬프트 이름(파일명에서 .txt 제외) → 시스템 프롬프트 문자열."""
    path = PROMPTS_DIR / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        log.warning("프롬프트 파일 로드 실패: %s — DEFAULT 폴백 사용", path)
        return DEFAULT_PROMPTS.get(name, "Summarize concisely.")
