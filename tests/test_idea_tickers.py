"""src/idea/tickers.py — name 정규화 + fuzzy 매칭 회귀 테스트.

`validate_and_fix`는 DART corp_map에 의존 — 단위 테스트는 normalize_name·
names_match 같은 pure helper만. 통합 테스트는 self-test가 담당.
"""

from __future__ import annotations

import pytest

from src.idea.tickers import names_match, normalize_name


# ------------------------------------------------------------------
# normalize_name
# ------------------------------------------------------------------
def test_normalize_strips_spaces():
    assert normalize_name("HD 현대중공업") == "hd현대중공업"


def test_normalize_strips_parens():
    assert normalize_name("(주)카카오") == "카카오"


def test_normalize_strips_hyphen():
    assert normalize_name("S-Oil") == "soil"


def test_normalize_handles_korean_paren():
    assert normalize_name("㈜현대") == "현대"


def test_normalize_empty():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""


def test_normalize_lowercase():
    assert normalize_name("ABC") == "abc"


# ------------------------------------------------------------------
# names_match — 핵심: HD현대오일뱅크 vs KCC 같은 hallucination 차단
# ------------------------------------------------------------------
def test_exact_match():
    assert names_match("삼성전자", "삼성전자")


def test_match_with_paren():
    assert names_match("카카오", "(주)카카오")


def test_match_with_korean_paren():
    assert names_match("현대", "㈜현대")


def test_match_with_spaces():
    assert names_match("HD현대중공업", "HD 현대중공업")


def test_substring_match_company_suffix():
    """한국석유 (LLM 약식) ↔ 한국석유공업 (DART 등록명)."""
    assert names_match("한국석유", "한국석유공업")
    assert names_match("한국석유공업", "한국석유")


def test_substring_match_holdings():
    """SK ↔ SK홀딩스, 포스코 ↔ POSCO홀딩스 같은 케이스."""
    assert names_match("두산", "두산홀딩스")


def test_completely_different_companies_blocked():
    """HD현대오일뱅크 vs KCC — substring 매칭 안 됨, 차단."""
    assert not names_match("HD현대오일뱅크", "KCC")
    assert not names_match("KCC", "HD현대오일뱅크")


def test_unrelated_companies_blocked():
    assert not names_match("삼성전자", "LG전자")
    assert not names_match("현대차", "기아")


def test_empty_returns_false():
    assert not names_match("", "삼성전자")
    assert not names_match("삼성전자", "")
    assert not names_match("", "")


def test_case_insensitive():
    assert names_match("S-OIL", "s-oil")
    assert names_match("Hyundai", "HYUNDAI")
