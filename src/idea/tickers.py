"""Ticker 검증·교정 — IdeaBot용.

LLM(perplexity 등)이 비상장 회사에 ticker를 hallucinate하는 케이스를 잡아 잘못된
KCC 리포트 같은 엉뚱한 매핑을 차단. fuzzy name 매칭(한국석유 ↔ 한국석유공업)도 처리.

검증 흐름 (top10의 각 candidate에 대해):
  1. ticker6 있음 → DART corp_map에서 ticker→DART 등록 회사명 lookup
     - LLM name과 정규화·substring 매칭 → OK
     - 불일치 → LLM name으로 재lookup해서 새 ticker 발견 시 교체, 못 찾으면 ticker 비움
  2. ticker6 없음 → name으로 lookup (정상 경로)
  3. 그래도 못 찾음 → ticker 비워두면 _collect_company_reports 스킵
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def normalize_name(s: str) -> str:
    """회사명 정규화 — `(주)`·`㈜` 명시 제거 + 공백·괄호·하이픈 제거 + lowercase.

    예: `(주)카카오` → `카카오`, `㈜현대` → `현대`, `S-Oil` → `soil`.
    """
    s = s or ""
    # (주) 또는 ㈜ 접두/접미 제거 — 한자 `주`가 회사명에 포함된 경우는 보존
    s = re.sub(r"\(\s*주\s*\)|㈜", "", s)
    # 잔여 공백·괄호·하이픈 제거
    s = re.sub(r"[\s()（）\-]+", "", s)
    return s.lower()


def names_match(llm_name: str, dart_name: str) -> bool:
    """LLM 회사명과 DART 등록명이 사실상 같은 회사를 가리키는지 fuzzy 판단.

    - 정규화 후 정확 일치 → True
    - 정규화 후 한쪽이 다른 쪽의 substring → True (예: '한국석유' ↔ '한국석유공업')
    - 빈 문자열 → False

    참고: substring 허용은 회사 suffix(공업/산업/홀딩스/지주) 약식 표기 흡수용.
    KCC vs HD현대오일뱅크 같이 완전 다른 회사는 substring 안 되므로 차단됨.
    """
    nl = normalize_name(llm_name)
    nd = normalize_name(dart_name)
    if not nl or not nd:
        return False
    if nl == nd:
        return True
    if nl in nd or nd in nl:
        return True
    return False


async def validate_and_fix(top10: list[dict]) -> list[dict]:
    """후보의 (name, ticker6) 페어를 DART corp_map과 대조해 검증·교정.

    조용히 (예외 발생해도) — 외부(DART API/Playwright)에 의존하므로 graceful.

    동작:
      1. ticker6 있음 → DART에서 ticker → 등록 회사명 조회
         - names_match → OK
         - 불일치 → name으로 재lookup해서 새 ticker 교체. 못 찾으면 ticker 비움.
      2. ticker6 없음 → name으로 lookup.
      3. 못 찾음 → ticker 비워둠 (후속 단계에서 스킵).
    """
    loop = asyncio.get_running_loop()
    try:
        from src.deepdive import dart_client
    except Exception:
        log.exception("dart_client import 실패 — ticker 룩업 스킵")
        return top10

    # corp_map 빌드 (한 번)
    try:
        await loop.run_in_executor(None, dart_client._load_corp_map)
    except Exception:
        log.exception("DART corp_map 로딩 실패")

    out: list[dict] = []
    for c in top10:
        ticker = (c.get("ticker6") or "").strip()
        name = (c.get("name") or "").strip()

        if re.match(r"^\d{6}$", ticker):
            # ticker 있음 — 회사명 일치 검증
            dart_name = dart_client._CORP_NAME_CACHE.get(ticker, "")
            if names_match(name, dart_name):
                out.append(c)
                continue
            # 불일치 → name으로 재lookup
            log.warning(
                "ticker mismatch: name='%s' ticker=%s but DART에는 '%s' — name으로 재lookup",
                name, ticker, dart_name,
            )
            new_ticker = None
            if name:
                try:
                    new_ticker = await loop.run_in_executor(
                        None, dart_client.lookup_ticker_by_name, name,
                    )
                except Exception:
                    log.exception("재lookup 실패: %s", name)
            if new_ticker and new_ticker != ticker:
                log.info(
                    "ticker 교체: %s '%s' → %s '%s'",
                    ticker, name, new_ticker, dart_client._CORP_NAME_CACHE.get(new_ticker, ""),
                )
                c["ticker6"] = new_ticker
            else:
                log.warning(
                    "ticker 매칭 실패 (비상장 가능성) — '%s' candidate ticker6 비움",
                    name,
                )
                c["ticker6"] = ""
            out.append(c)
            continue

        # ticker 비어있음 — name으로 lookup
        if not name:
            out.append(c)
            continue
        try:
            t = await loop.run_in_executor(None, dart_client.lookup_ticker_by_name, name)
        except Exception:
            log.exception("ticker 룩업 실패: %s", name)
            t = None
        if t:
            c["ticker6"] = t
        out.append(c)
    return out
