"""산업 이름 정규화 — 사용자 자유 텍스트를 wisereport에 통할 정식 산업명 1개로 매핑.

산업봇 `/industry <줄임말>` + 종목봇 `/curate <섹터>` 가 공유.

설계
----
1. 정형 사전(`data/wisereport_gics_catalog.json`) 1차 매칭 — name/alias substring·exact.
   단일 매치면 confidence=1.0 즉시 반환.
2. 매치 0 또는 다중 매치(>1)면 sonnet(IDEA_SYNTHESIS_MODEL)로 폴백.
   카탈로그 전체를 컨텍스트로 던지고 "best 1개 + ranked 후보 3개, JSON" 강제.
3. best.confidence ≥ 0.85 → 단일 결정. 아니면 후보 리스트로 호출자에 책임 위임.

호출자(예: `category_bots.industry_on_demand`, `idea_bot._cmd_curate`)는
ResolveResult.candidates 가 비어있지 않으면 인라인 버튼으로 사용자 선택을 요청해야 함.

LLM 호출은 `summarizer.chat_with_retry` — 빈 응답 재시도 + haiku 폴백 안전.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "wisereport_gics_catalog.json"

# best 신뢰도 임계값 — 이 이상이면 단일 결정, 미만이면 후보 3개 제시.
SINGLE_DECISION_THRESHOLD = 0.85

_CATALOG_CACHE: list[dict] | None = None


@dataclass
class Candidate:
    name_ko: str
    wisereport_query: str
    confidence: float
    reason: str = ""


@dataclass
class ResolveResult:
    query: str
    best: Candidate | None
    candidates: list[Candidate] = field(default_factory=list)
    source: str = ""  # "dict" | "llm" | "none"

    @property
    def is_single(self) -> bool:
        return (
            self.best is not None
            and self.best.confidence >= SINGLE_DECISION_THRESHOLD
            and not self.candidates
        )

    @property
    def needs_user_pick(self) -> bool:
        return bool(self.candidates) and not self.is_single

    @property
    def failed(self) -> bool:
        return self.best is None and not self.candidates


def load_catalog(path: Path | None = None) -> list[dict]:
    """카탈로그 로드 (메모리 캐시). 각 항목: {name_ko, wisereport_query, aliases}."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None and path is None:
        return _CATALOG_CACHE
    target = path or CATALOG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        log.exception("산업 카탈로그 로드 실패: %s", target)
        return []
    if not isinstance(raw, list):
        log.warning("카탈로그 포맷 오류 (list 아님): %s", target)
        return []
    if path is None:
        _CATALOG_CACHE = raw
    return raw


def _norm(s: str) -> str:
    """공백·구분자(·/-) 제거, 소문자."""
    return re.sub(r"[\s·/\-_,.]+", "", (s or "").lower())


def _dict_match(query: str, catalog: list[dict]) -> list[Candidate]:
    """사전 1차 매칭 — exact name, exact alias, substring 순으로 점수.

    동일 점수 다중 매치는 모두 반환. 호출자가 단일/다중 판단.
    """
    nq = _norm(query)
    if not nq:
        return []
    exact: list[Candidate] = []
    alias_exact: list[Candidate] = []
    substr: list[Candidate] = []
    for ent in catalog:
        name = ent.get("name_ko", "")
        wsq = ent.get("wisereport_query", name)
        aliases = ent.get("aliases") or []
        nn = _norm(name)
        if nn == nq:
            exact.append(Candidate(name, wsq, 1.0, "정식 산업명 exact"))
            continue
        a_norms = [_norm(a) for a in aliases]
        if nq in a_norms:
            alias_exact.append(Candidate(name, wsq, 0.95, f"alias exact: {aliases[a_norms.index(nq)]}"))
            continue
        # substring: query가 name/alias의 일부거나 그 반대
        hit_kw = None
        for a, na in zip(aliases, a_norms):
            if na and (na in nq or nq in na):
                hit_kw = a
                break
        if hit_kw is None and (nq in nn or nn in nq):
            hit_kw = name
        if hit_kw is not None:
            substr.append(Candidate(name, wsq, 0.7, f"부분 매치: {hit_kw}"))
    if exact:
        return exact
    if alias_exact:
        return alias_exact
    return substr


def _build_llm_messages(query: str, catalog: list[dict]) -> list[dict]:
    """sonnet 폴백 프롬프트. prompts/industry_resolve.txt 시스템 + 카탈로그 inject."""
    try:
        from src import idea_prompts
        sys_prompt = idea_prompts.load("industry_resolve")
    except Exception:
        sys_prompt = (
            "당신은 한국 주식 산업 분류 전문가다. 사용자가 입력한 자유 텍스트를 "
            "주어진 산업 카탈로그의 정식 산업명 1개로 정확히 핀포인트 매핑하라.\n"
            "출력은 JSON: {\"best\": {\"name_ko\": ..., \"confidence\": 0.0~1.0, \"reason\": ...},"
            " \"candidates\": [{\"name_ko\": ..., \"confidence\": ..., \"reason\": ...}] (best 제외 2개)}\n"
            "한 산업을 명확히 가리키면 confidence 0.9 이상, 애매하면 0.5~0.8."
        )
    cat_payload = [
        {
            "name_ko": e.get("name_ko"),
            "aliases": e.get("aliases") or [],
        }
        for e in catalog
    ]
    user_msg = (
        f"사용자 입력:\n{query}\n\n"
        f"산업 카탈로그 (정식 산업명만 사용 가능):\n"
        f"{json.dumps(cat_payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "위 카탈로그 내 산업명 중 1개로 best 매핑, 나머지는 후보 2개로 ranked. JSON만 출력."
    )
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]


# 모델 티어는 src/llm_models.py 공유. IDEA_* env 이름 그대로.
from src.llm_models import synthesis_model as _make_synthesis, narrow_model as _make_narrow


def _synthesis_model() -> str:
    return _make_synthesis("IDEA_SYNTHESIS_MODEL")


def _narrow_model() -> str:
    return _make_narrow("IDEA_NARROW_MODEL")


def _llm_resolve(query: str, catalog: list[dict]) -> tuple[Candidate | None, list[Candidate]]:
    """sonnet 호출. 실패/빈 응답 시 (None, []) — 호출자가 fail 처리."""
    if not catalog:
        return None, []
    try:
        from src import summarizer
    except Exception:
        log.exception("산업 정규화 LLM import 실패")
        return None, []
    try:
        client = summarizer.get_client()
    except Exception:
        log.exception("OpenRouter client init 실패 (industry resolve)")
        return None, []
    messages = _build_llm_messages(query, catalog)
    try:
        content = summarizer.chat_with_retry(
            client,
            model=_synthesis_model(),
            fallback_model=_narrow_model(),
            max_tokens=800,
            temperature=0.0,
            messages=messages,
            context=f"industry_resolve[{query[:30]}]",
        )
    except summarizer.OpenRouterCreditExhausted:
        raise
    except Exception:
        log.exception("산업 정규화 LLM 호출 실패")
        return None, []
    if not content:
        return None, []
    # JSON 추출 — 코드펜스 제거 + 첫 { ~ 마지막 }
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    blob = fence.group(1) if fence else content
    s = blob.find("{")
    e = blob.rfind("}")
    if s < 0 or e <= s:
        log.warning("industry_resolve JSON 블록 없음: %r", content[:300])
        return None, []
    try:
        data = json.loads(blob[s : e + 1])
    except Exception:
        log.warning("industry_resolve JSON 파싱 실패: %r", blob[s : e + 1][:300])
        return None, []
    # 카탈로그 정식명 → wisereport_query 룩업
    name_to_wsq = {e.get("name_ko"): e.get("wisereport_query", e.get("name_ko")) for e in catalog}

    def _mk(d: dict | None) -> Candidate | None:
        if not isinstance(d, dict):
            return None
        nm = (d.get("name_ko") or "").strip()
        if nm not in name_to_wsq:
            return None
        try:
            conf = float(d.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        return Candidate(nm, name_to_wsq[nm], conf, str(d.get("reason", ""))[:200])

    best = _mk(data.get("best"))
    cands_raw = data.get("candidates") or []
    candidates: list[Candidate] = []
    for d in cands_raw:
        c = _mk(d)
        if c is None:
            continue
        if best and c.name_ko == best.name_ko:
            continue
        candidates.append(c)
    return best, candidates[:2]


def resolve_industry(query: str) -> ResolveResult:
    """하이브리드 매핑 — 사전 1차 → sonnet 폴백 → 신뢰도 기반 단일/후보 결정.

    실패해도 예외 없이 ResolveResult(failed=True) 반환 (호출자가 사용자에 안내).
    OpenRouter credit 부족만 예외 전파.
    """
    q = (query or "").strip()
    result = ResolveResult(query=q, best=None)
    if not q:
        return result
    catalog = load_catalog()
    if not catalog:
        return result

    # 1단계: 사전. exact/alias-exact 단일(confidence≥0.85)만 즉시 결정.
    # substring(0.7)만 잡힌 경우는 LLM 폴백으로 검증 (오매핑 방지).
    dict_hits = _dict_match(q, catalog)
    if (
        len(dict_hits) == 1
        and dict_hits[0].confidence >= SINGLE_DECISION_THRESHOLD
    ):
        result.best = dict_hits[0]
        result.source = "dict"
        log.info("industry_resolve dict 단일: %s → %s (conf=%.2f)",
                 q, result.best.name_ko, result.best.confidence)
        return result

    # 2단계: LLM 폴백 (다중·0건·저신뢰 단일 모두)
    if len(dict_hits) > 1:
        log.info("industry_resolve dict 다중(%d) → LLM 폴백: %s",
                 len(dict_hits), [c.name_ko for c in dict_hits])
    elif len(dict_hits) == 1:
        log.info("industry_resolve dict 저신뢰 단일(%.2f) → LLM 폴백: %s → %s",
                 dict_hits[0].confidence, q, dict_hits[0].name_ko)
    else:
        log.info("industry_resolve dict 무매치 → LLM 폴백: %s", q)
    best, cands = _llm_resolve(q, catalog)
    if best is None and not cands:
        # LLM도 빈손 — 사전 다중 hit이라도 있으면 그걸 후보로 노출
        if dict_hits:
            result.best = dict_hits[0]
            result.candidates = dict_hits[1:3]
            result.source = "dict-multi"
            log.info("industry_resolve LLM 실패, dict 다중 후보 노출: %s",
                     [c.name_ko for c in dict_hits[:3]])
        return result
    result.source = "llm"
    if best and best.confidence >= SINGLE_DECISION_THRESHOLD:
        # 단일 결정 — candidates 비움
        result.best = best
        result.candidates = []
        log.info("industry_resolve LLM 단일: %s → %s (conf=%.2f)",
                 q, best.name_ko, best.confidence)
        return result
    # 신뢰도 부족 → best + 2개 후보를 사용자에게 제시
    result.best = best
    if best:
        result.candidates = [best] + cands
    else:
        result.candidates = cands
    # 중복 제거
    seen: set[str] = set()
    deduped: list[Candidate] = []
    for c in result.candidates:
        if c.name_ko in seen:
            continue
        seen.add(c.name_ko)
        deduped.append(c)
    result.candidates = deduped[:3]
    log.info("industry_resolve LLM 후보 제시(%d): %s",
             len(result.candidates), [c.name_ko for c in result.candidates])
    return result


def encode_callback(prefix: str, idx: int, query: str) -> str:
    """텔레그램 callback_data — wisereport_query를 카탈로그 idx로 인코딩.

    callback_data는 64바이트 제한이라 짧게 유지: '<prefix>|<idx>'. 추가 컨텍스트는
    user_data 등 다른 곳에 저장.
    """
    return f"{prefix}|{idx}"


def lookup_by_name(name_ko: str) -> Candidate | None:
    """카탈로그에서 정식명으로 직접 룩업 (callback 처리 시 사용)."""
    for e in load_catalog():
        if e.get("name_ko") == name_ko:
            return Candidate(name_ko, e.get("wisereport_query", name_ko), 1.0, "user pick")
    return None
