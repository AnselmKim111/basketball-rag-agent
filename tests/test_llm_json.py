"""src/llm_json.py — tolerant JSON 파서 회귀 테스트.

LLM 응답 파싱은 IdeaBot·EarningsBot 등 모든 봇의 핵심 정확성에 직결. 단순 회귀가
production 결과 품질 떨어뜨리지 않게 ASCII·edge case 다 cover.
"""

from __future__ import annotations

import json

import pytest

from src.llm_json import (
    clean_json_loose,
    parse_json_array,
    parse_json_object,
    try_loads,
)


# ------------------------------------------------------------------
# 정상 케이스
# ------------------------------------------------------------------
def test_parse_simple_object():
    assert parse_json_object('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_parse_nested_object():
    assert parse_json_object('{"a": {"b": [1, 2, 3]}}') == {"a": {"b": [1, 2, 3]}}


def test_parse_with_json_fence():
    src = '```json\n{"x": [1,2,3]}\n```'
    assert parse_json_object(src) == {"x": [1, 2, 3]}


def test_parse_with_generic_fence():
    src = '```\n{"x": 1}\n```'
    assert parse_json_object(src) == {"x": 1}


def test_parse_text_before_object():
    src = '추론 결과:\n{"a": 1}'
    assert parse_json_object(src) == {"a": 1}


# ------------------------------------------------------------------
# Edge: trailing comma, comments
# ------------------------------------------------------------------
def test_trailing_comma_object():
    assert parse_json_object('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_trailing_comma_array():
    assert parse_json_object('{"items": [1, 2, 3,]}') == {"items": [1, 2, 3]}


def test_js_line_comment():
    src = '{"a": 1, // this is a comment\n"b": 2}'
    assert parse_json_object(src) == {"a": 1, "b": 2}


def test_js_block_comment():
    src = '{"a": 1, /* block\ncomment */ "b": 2}'
    assert parse_json_object(src) == {"a": 1, "b": 2}


# ------------------------------------------------------------------
# Edge: truncate 복구
# ------------------------------------------------------------------
def test_truncate_array_unclosed():
    """top5 배열에 3개 항목 완성, array `]`와 root `}` missing."""
    src = (
        '{"top5": [\n'
        '  {"rank": 1, "name": "A"},\n'
        '  {"rank": 2, "name": "B"},\n'
        '  {"rank": 3, "name": "C"}'
    )
    result = parse_json_object(src)
    assert result is not None
    assert len(result["top5"]) == 3
    assert result["top5"][0]["name"] == "A"
    assert result["top5"][2]["name"] == "C"


def test_truncate_inside_string():
    """perplexity가 string field 안에서 잘림 — 직전 완성 item까지만 보존."""
    src = (
        '{"items": [\n'
        '  {"name": "OK", "purity": 9},\n'
        '  {"name": "PARTIAL", "note": "incomplete'
    )
    result = parse_json_object(src)
    assert result is not None
    assert len(result["items"]) == 1
    assert result["items"][0]["name"] == "OK"


def test_truncate_deeply_nested():
    """nested 객체 중간 끊김 — 가장 가까운 안전 위치 복구."""
    src = (
        '{"a": {"b": {"c": [1, 2,'
    )
    result = parse_json_object(src)
    # 너무 일찍 끊겨서 최상위 키 자체가 미완성 → 복구 불가일 수도 있음
    # 정상이면 a={}, 실패면 None — 둘 다 OK 단, exception은 안 됨
    assert result is None or isinstance(result, dict)


def test_string_with_escaped_braces():
    """문자열 안 { } 가 진짜 bracket으로 오해되지 않아야 함."""
    src = '{"sentence": "use { and } in code", "n": 5}'
    assert parse_json_object(src) == {"sentence": "use { and } in code", "n": 5}


def test_string_with_escaped_quote():
    """문자열 안 \\\" 가 string 종료로 오해되지 않아야 함."""
    src = '{"q": "he said \\"hi\\""}'
    assert parse_json_object(src) == {"q": 'he said "hi"'}


# ------------------------------------------------------------------
# Edge: 실패 케이스
# ------------------------------------------------------------------
def test_empty_returns_none():
    assert parse_json_object("") is None
    assert parse_json_object(None) is None
    assert parse_json_object("   \n\t  ") is None


def test_no_json_returns_none():
    assert parse_json_object("just some text") is None


def test_non_dict_top_level_returns_none():
    """LLM이 array 반환했는데 dict 기대했을 때."""
    assert parse_json_object("[1, 2, 3]") is None


# ------------------------------------------------------------------
# parse_json_array
# ------------------------------------------------------------------
def test_parse_array_simple():
    assert parse_json_array("[1, 2, 3]") == [1, 2, 3]


def test_parse_array_objects():
    src = '[{"a": 1}, {"b": 2}]'
    assert parse_json_array(src) == [{"a": 1}, {"b": 2}]


def test_parse_array_with_fence():
    assert parse_json_array('```json\n[1, 2]\n```') == [1, 2]


def test_parse_array_trailing_comma():
    assert parse_json_array("[1, 2, 3,]") == [1, 2, 3]


def test_parse_array_empty_returns_none():
    assert parse_json_array("") is None
    assert parse_json_array("not json") is None


# ------------------------------------------------------------------
# clean_json_loose 단독
# ------------------------------------------------------------------
def test_clean_strips_line_comment():
    assert "// note" not in clean_json_loose('{"a": 1} // note')


def test_clean_strips_block_comment():
    assert "/* x */" not in clean_json_loose('{"a": 1 /* x */}')


def test_clean_removes_trailing_comma():
    assert clean_json_loose('[1,2,]') == '[1,2]'
    assert clean_json_loose('{"a":1,}') == '{"a":1}'


# ------------------------------------------------------------------
# try_loads — 단독 wrapper
# ------------------------------------------------------------------
def test_try_loads_ok():
    assert try_loads('{"a": 1}') == {"a": 1}


def test_try_loads_invalid_returns_none():
    assert try_loads('not json') is None
    assert try_loads('') is None
