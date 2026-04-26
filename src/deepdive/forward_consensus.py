"""wisereport 기업 리포트(최근 3개) → 분기별 forward consensus 추정.

흐름:
  1. wisereport_context.collect_for_ticker가 이미 다운받아둔 기업 리포트 텍스트들을 입력으로 받음.
  2. 각 리포트마다 LLM이 "향후 4분기 매출/영업이익/순이익 전망"을 JSON 추출.
     - 분기별 명시 추정이 있으면 그대로.
     - 연간만 있으면 4 분기 균등 분할 (LLM 자체 판단).
  3. 같은 분기 키에 대해 N개 리포트 평균 → 컨센서스.

견고성:
  - LLM JSON 파싱 실패해도 빈 dict로 fallback.
  - 텍스트 비었거나 client 초기화 실패하면 빈 dict.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)


_FORWARD_SYS_PROMPT = """당신은 한국 분석가 리포트에서 forward 전망 숫자를 추출하는 데이터 추출기입니다.

입력으로 받은 기업 분석 리포트 텍스트에서 **향후 4-8 분기**의 매출/영업이익/순이익 전망 수치를
JSON 형식으로만 출력하세요. 자연어 설명 절대 금지, JSON만.

규칙:
- 분기 키는 "2026Q1", "2026Q2" 형식.
- 수치 단위는 **원**(KRW) 정수. (조원이면 ×1조, 억원이면 ×1억)
- 분기 전망이 명시 안 되어 있고 연간 전망만 있으면, 회사의 분기 분포 패턴
  (예: 반도체는 하반기 비중 큼)을 활용해 적절히 분할. 모호하면 균등 분할.
- 발견 못한 항목은 키에서 누락 (null로 채우지 말 것).
- 추정치가 없으면 빈 객체 {} 반환.
- 과거 실적은 추출 대상이 아님. 명시적으로 "E", "F", "예상", "전망", "추정" 표시된 미래 수치만.

출력 예:
{
  "2026Q2": {"revenue": 22500000000000, "op_profit": 6800000000000, "net_profit": 5200000000000},
  "2026Q3": {"revenue": 24000000000000, "op_profit": 7500000000000, "net_profit": 5800000000000},
  "2026Q4": {"revenue": 25000000000000, "op_profit": 8000000000000, "net_profit": 6200000000000},
  "2027Q1": {"revenue": 23000000000000, "op_profit": 7000000000000, "net_profit": 5400000000000}
}
"""


def from_wisereport(wctx) -> dict[str, dict[str, Optional[float]]]:
    """WisereportContext → forward consensus dict.

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
            log.info("forward 추출 성공 [%s]: %d 분기", title[:40], len(ext))
            extractions.append(ext)

    if not extractions:
        log.warning("모든 리포트에서 forward 전망 추출 실패")
        return {}

    return _aggregate(extractions)


def _extract_one(client, model: str, title: str, text: str) -> Optional[dict]:
    """단일 리포트 텍스트에서 forward JSON 추출. 어느 단계 실패해도 None."""
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=900,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _FORWARD_SYS_PROMPT},
                {"role": "user", "content": (
                    f"리포트 제목: {title}\n\n"
                    f"<report_text>\n{text[:30000]}\n</report_text>\n\n"
                    "위 텍스트에서 forward 전망 JSON만 출력하세요."
                )},
            ],
        )
    except Exception:
        log.exception("LLM 호출 실패 (forward, title=%s)", title[:40])
        return None
    try:
        content = (resp.choices[0].message.content or "").strip()
    except (IndexError, AttributeError):
        log.exception("LLM 응답 구조 비정상 (forward, title=%s)", title[:40])
        return None
    return _parse_json_object(content)


def _parse_json_object(content: str) -> Optional[dict]:
    """LLM 출력에서 첫 번째 valid JSON 객체 추출. 실패 시 None."""
    if not content:
        return None
    # 코드펜스 벗기기
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        content = fence.group(1)
    # 첫 { ~ 마지막 } 매칭 (간단한 휴리스틱; 보통 단일 객체 한 개라 OK)
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        log.exception("forward JSON 파싱 실패. raw=%s", content[:500])
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _aggregate(extractions: list[dict]) -> dict[str, dict[str, Optional[float]]]:
    """여러 리포트 추정치 → 분기별 평균.

    분기 키에 대한 매출/영업이익/순이익 각각 산술평균.
    한 리포트에만 있는 분기는 그 값을 그대로 사용.
    """
    bucket: dict[str, dict[str, list[float]]] = {}
    for ext in extractions:
        for q, vals in ext.items():
            if not isinstance(q, str) or not isinstance(vals, dict):
                continue
            if not re.match(r"^\d{4}Q[1-4]$", q):
                continue
            slot = bucket.setdefault(q, {"revenue": [], "op_profit": [], "net_profit": []})
            for k in ("revenue", "op_profit", "net_profit"):
                v = vals.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    slot[k].append(float(v))

    result: dict[str, dict[str, Optional[float]]] = {}
    for q, slot in bucket.items():
        result[q] = {}
        for k, lst in slot.items():
            result[q][k] = sum(lst) / len(lst) if lst else None
    log.info("forward 컨센서스 집계: %d 분기, 키=%s", len(result), sorted(result.keys()))
    return result
