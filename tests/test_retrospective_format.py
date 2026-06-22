"""retrospective.format_retrospective_line — KR/US 양쪽 출력 계약.

HTML 모드 메시지에 박히는 라인이므로 동적 종목명 이스케이프가 핵심
(과거 legacy Markdown parse 실패로 며칠 미발송 사고의 재발 방지).
"""
from __future__ import annotations

import pytest

from src.screener import retrospective as kr
from src.us_screener import retrospective as us


RETRO = {
    "high_all": {
        "n": 3, "beats": 2, "avg_return_pct": 4.2,
        "top": [("005930", "삼성전자", 7.5)],
        "bottom": [],
    },
    "high_52w": {
        "n": 0, "beats": 0, "avg_return_pct": 0.0, "top": [], "bottom": [],
    },
}


@pytest.mark.parametrize("mod", [kr, us])
def test_empty_retro_returns_empty_string(mod):
    assert mod.format_retrospective_line({}) == ""


@pytest.mark.parametrize("mod", [kr, us])
def test_line_contains_category_stats(mod):
    line = mod.format_retrospective_line(RETRO)
    assert "🔁 <b>지난 회고</b>" in line
    assert "역사적 신고가 3종목 평균 +4.2% (2승/1패)" in line
    assert "최고 삼성전자 +7.5%" in line
    # n=0 카테고리는 표시 안 함
    assert "52주 신고가" not in line


@pytest.mark.parametrize("mod", [kr, us])
def test_top_name_html_escaped(mod):
    retro = {
        "high_all": {
            "n": 1, "beats": 1, "avg_return_pct": 21.5,
            "top": [("028050", "삼성E&A <테스트>", 21.5)],
            "bottom": [],
        },
    }
    line = mod.format_retrospective_line(retro)
    assert "삼성E&amp;A &lt;테스트&gt;" in line
    assert "<테스트>" not in line
