"""src/industry_catalog.resolve_industry 사전 매핑 단위 테스트.

사전 단계만 검증 — LLM 폴백은 monkeypatch로 시뮬레이트. 실제 OpenRouter 호출 없음.

run:
    pytest tests/test_industry_resolve.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import industry_catalog as ic


@pytest.fixture(autouse=True)
def _clear_cache():
    """각 테스트 시작 전 카탈로그 캐시 리셋."""
    ic._CATALOG_CACHE = None
    yield
    ic._CATALOG_CACHE = None


def test_catalog_loads():
    catalog = ic.load_catalog()
    assert catalog, "카탈로그가 비어있다 — data/wisereport_gics_catalog.json 확인"
    # 필수 산업 — 시드 기준
    names = {e["name_ko"] for e in catalog}
    must_have = {
        "반도체 소재·부품·장비",
        "2차전지·배터리",
        "방산·우주·항공",
        "정유·석유화학",
    }
    missing = must_have - names
    assert not missing, f"필수 산업 누락: {missing}"


def test_so_bu_jang_single_hit(monkeypatch):
    """'소부장' → 반도체 소재·부품·장비, alias exact, LLM 호출 없이 단일 결정."""
    # LLM 폴백이 호출되면 실패로 간주 (사전이 단일 매치를 잡아야 함)
    monkeypatch.setattr(ic, "_llm_resolve", lambda q, c: pytest.fail(f"LLM called for {q}"))
    r = ic.resolve_industry("소부장")
    assert r.is_single, f"단일 결정 안 됨: best={r.best}, candidates={r.candidates}"
    assert r.best.name_ko == "반도체 소재·부품·장비"
    assert r.source == "dict"


@pytest.mark.parametrize("query,expected", [
    ("반도체 소부장", "반도체 소재·부품·장비"),
    ("반도체 장비", "반도체 소재·부품·장비"),
    ("이차전지", "2차전지·배터리"),
    ("배터리", "2차전지·배터리"),
    ("양극재", "2차전지 소재"),
    ("방산", "방산·우주·항공"),
    ("K-방산", "방산·우주·항공"),
    ("정유", "정유·석유화학"),
    ("CDMO", "바이오·바이오시밀러"),
    ("K뷰티", "화장품·뷰티"),
])
def test_alias_exact_single(monkeypatch, query, expected):
    """주요 alias가 단일 결정으로 잡혀야 함 (LLM 미호출)."""
    monkeypatch.setattr(ic, "_llm_resolve", lambda q, c: pytest.fail(f"LLM called for {q}"))
    r = ic.resolve_industry(query)
    assert r.best is not None, f"매핑 실패: {query}"
    assert r.best.name_ko == expected, f"{query} → 기대 {expected}, 실제 {r.best.name_ko}"
    assert r.is_single, f"{query}: 단일 결정 안 됨 (cand={[c.name_ko for c in r.candidates]})"


def test_ambiguous_query_uses_llm(monkeypatch):
    """사전에 정의되지 않은 신조어/광범위 입력 → LLM 폴백."""
    calls: list[str] = []

    def fake_llm(q, c):
        calls.append(q)
        # best: AI·소프트웨어, 낮은 신뢰도 + 후보 1개
        best = ic.Candidate("AI·소프트웨어", "AI", 0.7, "LLM 추정")
        cands = [ic.Candidate("데이터센터·서버", "데이터센터", 0.6, "보조")]
        return best, cands

    monkeypatch.setattr(ic, "_llm_resolve", fake_llm)
    r = ic.resolve_industry("ZZ_무명_신조어_QQQ")  # 카탈로그 무매치 → LLM 강제 진입
    assert calls, "LLM 폴백이 호출되어야 함"
    # 신뢰도 0.7 < 0.85 → 후보 제시
    assert not r.is_single
    assert r.needs_user_pick
    assert len(r.candidates) >= 2


def test_llm_high_confidence_single(monkeypatch):
    """LLM이 높은 신뢰도로 1개 결정 → single."""
    def fake_llm(q, c):
        best = ic.Candidate("AI·소프트웨어", "AI", 0.97, "확실")
        return best, []
    monkeypatch.setattr(ic, "_llm_resolve", fake_llm)
    r = ic.resolve_industry("생성형 AI 인프라 기업")  # alias로 정확히 안 잡힘 → LLM 진입 시나리오
    # 만약 사전에서 'AI' 또는 'AI 인프라' alias로 단일이면 그것도 OK
    assert r.best is not None
    # is_single 둘 중 하나: dict 단일이거나 LLM 단일.
    assert r.is_single, f"single 결정 안 됨: source={r.source} cand={r.candidates}"


def test_failed_when_empty_query():
    r = ic.resolve_industry("")
    assert r.failed


def test_failed_when_catalog_empty(monkeypatch, tmp_path):
    """카탈로그 비어있으면 failed."""
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(ic, "CATALOG_PATH", empty)
    ic._CATALOG_CACHE = None
    r = ic.resolve_industry("소부장")
    assert r.failed


def test_lookup_by_name():
    c = ic.lookup_by_name("반도체 소재·부품·장비")
    assert c is not None
    assert c.wisereport_query  # non-empty


def test_norm_handles_punctuation():
    assert ic._norm("반도체 소재·부품·장비") == ic._norm("반도체소재부품장비")
    assert ic._norm("K-방산") == ic._norm("K방산")
