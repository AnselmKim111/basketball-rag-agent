"""LLM 응답에서 tolerant JSON 추출 — 봇 공유 모듈.

LLM(perplexity/sonar, claude, kimi 등)이 반환하는 JSON은 흔히 다음 결함을 가짐:
  - ```json fence로 감싸짐
  - trailing comma (`,]` `,}`)
  - JS-style 코멘트
  - max_tokens 한계로 array 중간 또는 string 안에서 truncate

이 모듈은 위 케이스를 단계적으로 복구해 최대한 데이터를 살린다:
  1. 코드 펜스 제거
  2. 첫 { ~ 마지막 } 추출 후 그대로 시도
  3. trailing comma + JS 코멘트 제거 후 시도
  4. depth/string-tracking으로 마지막 완전 closed top-level 위치까지 잘라냄
  5. force-close: 현재 stack의 가상 pop으로 가장 데이터 많이 보존하는 안전 위치
     찾아 잘라내고 stack 역순으로 ']' 또는 '}' 채워 닫음

주의: 이 모듈은 LLM-친화적이라 의도적으로 lenient. 진짜 잘못된 JSON은 None 반환.
무거운 의존성 없음 (json, re만 사용) — 어디서든 import 가능.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------
def parse_json_object(content: str) -> dict | None:
    """LLM 출력 텍스트에서 첫 `{...}` JSON 객체를 추출.

    실패 시 None. 4단계 복구 시도 (위 docstring 참조). 비-dict 결과(예: array)는
    None으로 처리 — 호출자가 dict 기대.
    """
    if not content:
        log.warning("parse_json_object: 빈 content")
        return None
    # 코드 펜스 제거 — fence 안에 객체가 있으면 그 안만 사용
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    content_inner = fence.group(1) if fence else content
    start = content_inner.find("{")
    end = content_inner.rfind("}")
    if start < 0 or end <= start:
        log.warning(
            "parse_json_object: JSON 블록 못 찾음 (head=%r tail=%r)",
            content_inner[:200], content_inner[-200:],
        )
        return None
    blob = content_inner[start:end + 1]

    # 1차: 그대로
    obj = try_loads(blob)
    if obj is not None:
        return obj if isinstance(obj, dict) else None

    # 2차: cleanup
    cleaned = clean_json_loose(blob)
    obj = try_loads(cleaned)
    if obj is not None:
        log.info("parse_json_object — cleanup 후 성공")
        return obj if isinstance(obj, dict) else None

    # 3차: depth/string-tracking truncate
    safe_blob = _truncate_to_balanced_json(content_inner, start)
    if safe_blob is not None:
        obj = try_loads(clean_json_loose(safe_blob))
        if obj is not None:
            log.info("parse_json_object — balanced truncate 성공 (len=%d)", len(safe_blob))
            return obj if isinstance(obj, dict) else None

    # 4차: force-close — 가상 pop으로 가장 데이터 많은 안전 위치 + 강제 닫음
    forced = _force_close_open_brackets(content_inner, start, end)
    if forced is not None:
        obj = try_loads(clean_json_loose(forced))
        if obj is not None:
            log.info("parse_json_object — force-close 성공 (len=%d)", len(forced))
            return obj if isinstance(obj, dict) else None

    log.warning(
        "parse_json_object 최종 실패 — head=%r tail=%r",
        blob[:200], blob[-200:],
    )
    return None


def parse_json_array(content: str) -> list | None:
    """LLM 출력에서 첫 `[...]` JSON array 추출. 실패 시 None."""
    if not content:
        return None
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", content, re.DOTALL)
    content_inner = fence.group(1) if fence else content
    start = content_inner.find("[")
    end = content_inner.rfind("]")
    if start < 0 or end <= start:
        return None
    blob = content_inner[start:end + 1]
    obj = try_loads(blob)
    if obj is not None and isinstance(obj, list):
        return obj
    obj = try_loads(clean_json_loose(blob))
    if obj is not None and isinstance(obj, list):
        return obj
    return None


def try_loads(s: str) -> Any:
    """json.loads 성공 시 객체, 실패 시 None. (테스트·외부 호출 가능)"""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None
    except Exception:
        return None


def clean_json_loose(s: str) -> str:
    """LLM JSON에서 흔한 비표준 표기 제거.

    - trailing comma: `,\\s*]` → `]`, `,\\s*}` → `}`
    - JS 라인 코멘트 `// ...`
    - JS 블록 코멘트 `/* ... */`
    """
    s = re.sub(r"//[^\n\r]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


# ------------------------------------------------------------------
# Internal recovery helpers
# ------------------------------------------------------------------
def _truncate_to_balanced_json(s: str, start: int) -> str | None:
    """문자열·이스케이프를 추적하며 depth가 0으로 돌아온 마지막 위치까지 잘라냄.

    LLM이 출력 중간(예: 문자열 안)에서 끊긴 경우, 가장 가까운 안전한 cutoff를 찾는다.
    반환: s[start:cutoff+1] 형태의 부분 문자열 (성공 시) 또는 None.
    """
    if start < 0 or start >= len(s) or s[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    last_complete = -1
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{" or c == "[":
            depth += 1
        elif c == "}" or c == "]":
            depth -= 1
            if depth == 0:
                last_complete = i
    if last_complete < 0:
        return None
    return s[start:last_complete + 1]


def _force_close_open_brackets(s: str, start: int, end: int) -> str | None:
    """array/object 중간 truncate 시 마지막 완전한 항목까지 보존하고 강제 닫음.

    동작:
      1. start 부터 한 글자씩 진행. 문자열·이스케이프 추적.
      2. push/pop마다 stack 변화 기록 + safe_positions[depth-after-pop] 갱신.
      3. 끝까지 가서 닫히지 않은 `]`·`}` 잔존 시:
         - stack을 가상으로 pop하면서 safe_positions를 탐색 (가장 데이터 많이 보존
           되는 깊이부터).
         - 그 위치까지 잘라낸 후 남은 stack을 역순으로 닫음.

    예: top5 배열에 5개 항목 중 5번째가 닫혔지만 array `]`와 root `}`가 missing →
        stack=['{', '['] 길이 2. safe_positions[2] = 5번째 `}` 위치.
        그 위치까지 잘라낸 후 `]}` append → 5개 항목 보존된 valid JSON.

    문자열 안에서 truncate된 경우(perplexity의 specialty_note 등):
        stack=['{', '[', '{'] 길이 3. safe_positions[3] = None.
        target_depth=2로 fallback → 직전 완성 candidate까지만 보존 + `]}` append.
    """
    if start < 0 or start >= len(s) or s[start] != "{":
        return None
    in_string = False
    escape = False
    stack: list[str] = []
    safe_positions: dict[int, int] = {}
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c in "{[":
            stack.append(c)
        elif c in "}]":
            if not stack:
                return None
            stack.pop()
            safe_positions[len(stack)] = i
    if not stack:
        return s[start:end + 1] if end >= start else None

    # stack 가상 pop으로 가장 데이터 많은 안전 위치 탐색
    for pop_count in range(0, len(stack) + 1):
        target_depth = len(stack) - pop_count
        safe = safe_positions.get(target_depth)
        if safe is None or safe <= start:
            continue
        truncated_blob = s[start:safe + 1]
        items_to_close = stack[:target_depth]
        closing = "".join("]" if ch == "[" else "}" for ch in reversed(items_to_close))
        return truncated_blob + closing
    return None
