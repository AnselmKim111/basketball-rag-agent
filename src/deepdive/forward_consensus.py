"""wisereport 기업 리포트(최근 3개) → 분기별 forward consensus 추정.

흐름:
  1. wisereport_context.collect_for_ticker가 이미 다운받아둔 기업 리포트 텍스트들을 입력으로 받음.
  2. 각 리포트마다 LLM이 향후 4-8 분기 또는 향후 1-2년 매출/영업이익/순이익 전망을 JSON 추출.
     - 분기 키("2026Q2") 우선.
     - 분기 명시 없을 때 연간 키("2026FY") 도 허용 → 집계 단계에서 4 분기 균등 분할.
  3. 같은 분기 키에 N개 리포트 평균 → 컨센서스.

견고성:
  - LLM JSON 파싱 실패해도 빈 dict로 fallback.
  - 텍스트 비었거나 client 초기화 실패하면 빈 dict.
  - LLM raw 응답은 DEBUG 로그 (한국 분석가 리포트 패턴 검증용 — 평소 비활성).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

# 단일 리포트 텍스트를 LLM에 보낼 때 자르는 길이 (앞 60% + 뒤 40%).
# 한국 증권사 리포트 PDF는 forward 표가 후반부에 있는 경우가 많아 양 끝을 함께 봄.
FORWARD_TEXT_WINDOW = 40_000


_FORWARD_SYS_PROMPT = """당신은 한국 분석가 리포트에서 forward 전망 숫자를 추출하는 데이터 추출기입니다.
JSON으로만 출력하세요. 자연어 설명·markdown·코드펜스 절대 금지.

# 입력 텍스트의 표기 패턴 (참고)
한국 분석가 리포트의 forward 표기는 보통:
  - "2026E" / "2026F" / "FY26E" / "FY26F" / "2026P" / "26F" — 미래 연간 추정치
  - "1Q26F" / "2Q26E" — 미래 분기 추정치
  - 표 형식이 흔함: "매출/영업이익/순이익" 행 + "2024 / 2025 / 2026E / 2027E" 열
  - 텍스트형: "26F 매출 5,000억원, 영업이익 1,200억원 전망"

# 출력 JSON 키
- 분기 키: "2026Q1" 형식 (분석가가 분기 추정을 명시한 경우에만)
- 연간 키: "2026FY" 형식 (분기 없을 때)
- 둘 다 있으면 둘 다 출력 OK.
- value는 항상 {"revenue": ..., "op_profit": ..., "net_profit": ...} (하나만 있어도 됨).
- 단위는 **원** 정수. 조원이면 ×10^12, 억원이면 ×10^8.

# 추출 규칙
- 명시적인 미래 추정치만 (E/F/예상/전망/추정/Forecast 표시).
- 과거 실적, 분석가 목표주가, 밸류에이션 배수는 추출 대상 아님.
- 일부 항목만 발견되면 발견된 것만 (없는 키는 누락).
- 연간만 있고 분기 없으면 연간 키만.
- 추정치 전혀 없으면 빈 객체 {}.

# 출력 예 (분기 명시된 경우)
{"2026Q2": {"revenue": 22500000000000, "op_profit": 6800000000000}, "2026Q3": {"revenue": 24000000000000}}

# 출력 예 (연간만 있는 경우 — 한국 중소형주 리포트 흔한 케이스)
{"2026FY": {"revenue": 500000000000, "op_profit": 120000000000, "net_profit": 90000000000}, "2027FY": {"revenue": 600000000000, "op_profit": 150000000000, "net_profit": 110000000000}}

# 출력 예 (추정 못 찾음)
{}
"""


def from_wisereport(wctx) -> dict[str, dict[str, Optional[float]]]:
    """WisereportContext → forward consensus dict (분기별).

    빈 dict 반환 가능 (입력 없음, LLM 실패, 파싱 실패 모두).
    """
    if wctx is None or not getattr(wctx, "company_texts", None):
        log.info("forward consensus: wisereport company_texts 없음 → 빈 결과")
        return {}

    try:
        from src import summarizer
    except Exception:
        log.exception("summarizer import 실패")
        return {}

    try:
        client = summarizer.get_client()
    except Exception:
        log.exception("OpenRouter client 초기화 실패 → forward 빈 dict")
        return {}

    extractions: list[dict] = []
    for title, text in zip(wctx.company_titles, wctx.company_texts):
        if not text or not text.strip():
            continue
        try:
            ext = _extract_one(client, summarizer.DEFAULT_MODEL, title, text)
        except Exception:
            log.exception("forward 추출 실패: %s", title)
            continue
        if ext:
            log.info("forward 추출 성공 [%s]: 키=%s", title[:40], list(ext.keys()))
            extractions.append(ext)
        else:
            log.info("forward 추출 결과 빈 dict [%s]", title[:40])

    if not extractions:
        log.warning("모든 리포트에서 forward 전망 추출 실패")
        return {}

    return _aggregate(extractions)


def _extract_one(client, model: str, title: str, text: str) -> Optional[dict]:
    """단일 리포트 텍스트에서 forward JSON 추출. 어느 단계 실패해도 None."""
    from src import summarizer as _sz

    # forward 신호가 후반부에 있는 경우가 많아 텍스트 끝부분도 함께 보내기
    snippet = _smart_text_snippet(text, max_chars=FORWARD_TEXT_WINDOW)
    try:
        content = _sz.chat_with_retry(
            client,
            model=model,
            max_tokens=1200,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _FORWARD_SYS_PROMPT},
                {"role": "user", "content": (
                    f"리포트 제목: {title}\n\n"
                    f"<report_text>\n{snippet}\n</report_text>\n\n"
                    "위 텍스트에서 forward 전망 JSON만 출력하세요. "
                    "분기 추정 없으면 연간(2026FY) 키로라도 출력. "
                    "추정 자체가 전혀 없으면 빈 객체 {} 반환."
                )},
            ],
            context=f"forward {title[:40]}",
        )
    except _sz.OpenRouterCreditExhausted:
        log.warning("forward 추출 OpenRouter 토큰 부족 — 스킵")
        return None
    except Exception:
        log.exception("chat_with_retry 호출 실패 (forward, title=%s)", title[:40])
        return None

    if not content:
        return None
    log.debug("forward LLM raw [%s]: %s", title[:40], content[:600])
    return _parse_json_object(content)


def _smart_text_snippet(text: str, max_chars: int) -> str:
    """긴 텍스트를 max_chars로 자르되, forward 키워드 주변을 포함시키려고 시도.

    구현 단순화: 텍스트 길이 ≤ max_chars면 그대로. 길면 앞 60% + 뒤 40% 결합.
    """
    if len(text) <= max_chars:
        return text
    head = max_chars * 6 // 10
    tail = max_chars - head
    return text[:head] + "\n...[중략]...\n" + text[-tail:]


def _parse_json_object(content: str) -> Optional[dict]:
    """LLM 출력에서 첫 번째 valid JSON 객체 추출. src/llm_json.py 사용 (tolerant)."""
    from src.llm_json import parse_json_object
    return parse_json_object(content)


# ------------------------------------------------------------------
# 집계: 분기 키 + 연간 키 mix → 분기별 평균
# ------------------------------------------------------------------
_QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")
_ANNUAL_RE = re.compile(r"^(\d{4})(?:FY|F|E)?$")


def _aggregate(extractions: list[dict]) -> dict[str, dict[str, Optional[float]]]:
    """여러 리포트 추정치 → 분기별 평균.

    - 분기 키("2026Q2"): 그대로 슬롯에 누적.
    - 연간 키("2026FY", "2026F", "2026"): 4 분기 균등 분할 후 누적.
    - 분기 추정과 연간 추정이 같은 해에 같이 있으면, 분기 추정 우선
      (연간은 보조 — 분기에 안 잡힌 분기만 채움).
    """
    bucket: dict[str, dict[str, list[float]]] = {}

    def _add_quarter(q: str, vals: dict) -> None:
        slot = bucket.setdefault(q, {"revenue": [], "op_profit": [], "net_profit": []})
        for k in ("revenue", "op_profit", "net_profit"):
            v = vals.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                slot[k].append(float(v))

    for ext in extractions:
        # 1차: 명시적 분기 키
        explicit_quarters_by_year: dict[int, set[int]] = {}
        for q, vals in ext.items():
            if not isinstance(q, str) or not isinstance(vals, dict):
                continue
            qm = _QUARTER_RE.match(q)
            if qm:
                _add_quarter(q, vals)
                explicit_quarters_by_year.setdefault(int(qm.group(1)), set()).add(int(qm.group(2)))

        # 2차: 연간 키 → 분기별 균등 분할 (해당 분기에 명시 추정 없는 경우만)
        for q, vals in ext.items():
            if not isinstance(q, str) or not isinstance(vals, dict):
                continue
            am = _ANNUAL_RE.match(q)
            if not am or _QUARTER_RE.match(q):
                continue
            year = int(am.group(1))
            already = explicit_quarters_by_year.get(year, set())
            quarter_vals: dict[str, float] = {}
            for k in ("revenue", "op_profit", "net_profit"):
                v = vals.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    quarter_vals[k] = float(v) / 4.0
            if not quarter_vals:
                continue
            for q_idx in (1, 2, 3, 4):
                if q_idx in already:
                    continue
                _add_quarter(f"{year}Q{q_idx}", quarter_vals)

    result: dict[str, dict[str, Optional[float]]] = {}
    for q, slot in bucket.items():
        result[q] = {}
        for k, lst in slot.items():
            result[q][k] = sum(lst) / len(lst) if lst else None
    log.info("forward 컨센서스 집계: %d 분기, 키=%s", len(result), sorted(result.keys()))
    return result
