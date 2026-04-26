"""IdeaBot용 편집 가능 프롬프트 로더.

src/deepdive/prompts.py와 같은 패턴 — 매 호출마다 prompts/ 디렉터리 read.
파일 없거나 읽기 실패 시 하드코딩된 DEFAULT 폴백 사용 → 절대 None 반환 안 함.

수정 절차: prompts/idea_*.txt 편집 → push → 다음 호출부터 반영.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


DEFAULT_PROMPTS = {
    "idea_research": (
        "당신은 한국 주식 시니어 애널리스트. 사용자 투자 아이디어를 받아 웹검색으로 현황을 조사하고 "
        "'논리의 기울기'를 검증한 뒤 수혜 산업 2-3개와 시총 상위 30 후보 종목을 JSON으로 출력하라."
    ),
    "idea_narrow": (
        "30개 후보 + 산업 리포트 텍스트를 받아 영업레버리지 4가지 축(고정비 비중·가동률 여유·"
        "매출 가속·마진 민감도)으로 평가해 top 10을 JSON으로 출력하라."
    ),
    "idea_synthesis": (
        "10 후보 + 종목 리포트 텍스트를 받아 영업레버리지가 가장 세게 걸릴 Top 5를 선정하고, "
        "각 종목별 1500자 thesis와 정량 수치, 참고 리포트 파일명을 JSON으로 출력하라."
    ),
}


def load(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        log.warning("프롬프트 파일 로드 실패: %s — DEFAULT 폴백 사용", path)
        return DEFAULT_PROMPTS.get(name, "Output JSON only.")
