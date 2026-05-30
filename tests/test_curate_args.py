"""src/bot_worker._parse_curate_args 단위 테스트.

종목봇 `/curate` 명령의 인자 파싱 — 산업명과 N 둘 다 받기 (순서 무관).
"""

from __future__ import annotations

import pytest

from src.bot_worker import _parse_curate_args


@pytest.mark.parametrize("args,expected_industry,expected_n", [
    ([], None, 10),
    (["5"], None, 5),
    (["15"], None, 15),
    (["100"], None, 15),                       # 상한 15
    (["0"], None, 1),                          # 하한 1
    (["소부장"], "소부장", 10),
    (["소부장", "5"], "소부장", 5),
    (["5", "소부장"], "소부장", 5),               # 순서 무관
    (["2차전지", "소재"], "2차전지 소재", 10),     # 멀티 토큰 산업
    (["2차전지", "소재", "5"], "2차전지 소재", 5),
    (["7", "방산", "우주"], "방산 우주", 7),
    (["반도체", "소부장"], "반도체 소부장", 10),
])
def test_parse_curate_args(args, expected_industry, expected_n):
    industry, n = _parse_curate_args(args)
    assert industry == expected_industry
    assert n == expected_n


def test_parse_curate_args_truncates_long_industry():
    long_name = "가" * 80
    industry, n = _parse_curate_args([long_name])
    assert industry is not None and len(industry) <= 60
    assert n == 10
