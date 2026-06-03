"""환각 게이트 + 품질 가드 (Phase 7B).

세 함수가 핵심:
- verify_extract_verbatim(extract, raw_text) — extract의 verbatim 인용이 실제 raw transcript에
  존재하는지 사후 검증. 정규식·정규화(whitespace·case 무시) 매칭.
- verify_synthesis_citations(synthesis_text, payload) — synthesis 출력의 [TICKER] bracket과
  "..." 인용이 payload에 근거 있는지 검증.
- enforce_min_length(text, min_chars) — 합성 길이 보장.

결과는 ViolationReport 리스트. 임계치 초과 시 호출자가 재요청·항목 제거 등 결정.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 정규화
# ------------------------------------------------------------------
_WS = re.compile(r"\s+")
_SMART_QUOTES = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
}


def _normalize(s: str) -> str:
    """비교용 정규화 — lowercase + smart quote 보정 + 공백 단일화."""
    if not s:
        return ""
    s = s.lower()
    for k, v in _SMART_QUOTES.items():
        s = s.replace(k, v)
    return _WS.sub(" ", s).strip()


def _frag_in(fragment: str, haystack: str, head_chars: int = 60) -> bool:
    """fragment의 첫 head_chars를 haystack에서 찾기. 둘 다 정규화."""
    if not fragment or not haystack:
        return False
    f = _normalize(fragment)[:head_chars]
    if len(f) < 12:  # 너무 짧으면 우연 매치 위험 → fail
        return False
    return f in _normalize(haystack)


# ------------------------------------------------------------------
# Violation report
# ------------------------------------------------------------------
@dataclass
class ViolationReport:
    """검증 위반 1건."""
    layer: str           # "extract" | "synthesis"
    field: str           # "named_deals[2].verbatim" 같은 경로
    fragment: str        # 위반 fragment 첫 80자
    reason: str          # "not_found_in_source" | "ticker_not_in_payload" | "too_short" | ...


@dataclass
class GateResult:
    """검증 결과 묶음."""
    layer: str
    total_checked: int = 0
    violations: list[ViolationReport] = field(default_factory=list)
    violation_ratio: float = 0.0  # 0.0 ~ 1.0
    passed: bool = True

    def summary(self) -> str:
        if self.total_checked == 0:
            return f"{self.layer}: 검증 대상 0건"
        v = len(self.violations)
        return f"{self.layer}: 위반 {v}/{self.total_checked} ({self.violation_ratio*100:.1f}%) " + ("✅ pass" if self.passed else "❌ FAIL")


# ------------------------------------------------------------------
# 1) extract verbatim 검증
# ------------------------------------------------------------------
_EXTRACT_VERBATIM_FIELDS = [
    ("named_deals", "verbatim"),
    ("supply_chain_signals", "verbatim"),
    ("competitor_callouts", "verbatim"),
    ("macro_policy_mentions", "verbatim"),
]


def verify_extract_verbatim(
    extract: dict | None, raw_text: str | None, threshold: float = 0.30,
) -> GateResult:
    """extract의 verbatim 필드들이 raw transcript에 등장하는지 검증."""
    result = GateResult(layer="extract")
    if not extract or not raw_text:
        return result
    haystack = raw_text
    for arr_name, key in _EXTRACT_VERBATIM_FIELDS:
        items = extract.get(arr_name) or []
        for i, item in enumerate(items):
            v = (item or {}).get(key) if isinstance(item, dict) else None
            if not v or not isinstance(v, str):
                continue
            result.total_checked += 1
            if not _frag_in(v, haystack, head_chars=80):
                result.violations.append(ViolationReport(
                    layer="extract",
                    field=f"{arr_name}[{i}].{key}",
                    fragment=v[:80],
                    reason="not_found_in_transcript",
                ))
    # management_discussion의 quote도 검증 (가장 환각 위험 ↑)
    md = extract.get("management_discussion") or []
    for i, item in enumerate(md):
        if not isinstance(item, dict):
            continue
        q = item.get("quote")
        if not q or not isinstance(q, str):
            continue
        result.total_checked += 1
        if not _frag_in(q, haystack, head_chars=80):
            result.violations.append(ViolationReport(
                layer="extract",
                field=f"management_discussion[{i}].quote",
                fragment=q[:80],
                reason="not_found_in_transcript",
            ))
    nv = extract.get("notable_verbatim") or []
    for i, v in enumerate(nv):
        if not isinstance(v, str):
            continue
        result.total_checked += 1
        if not _frag_in(v, haystack, head_chars=60):
            result.violations.append(ViolationReport(
                layer="extract",
                field=f"notable_verbatim[{i}]",
                fragment=v[:80],
                reason="not_found_in_transcript",
            ))
    if result.total_checked > 0:
        result.violation_ratio = len(result.violations) / result.total_checked
    result.passed = result.violation_ratio < threshold
    return result


def strip_violating_items(extract: dict, violations: list[ViolationReport]) -> dict:
    """위반 항목을 extract에서 제거. 반환은 같은 dict (in-place 수정)."""
    if not violations:
        return extract
    # field 경로별로 위반 인덱스 모으기
    by_array: dict[str, set[int]] = {}
    notable_drop: set[int] = set()
    for v in violations:
        m = re.match(r"^(\w+)\[(\d+)\]\.(\w+)$", v.field)
        if m:
            arr_name = m.group(1)
            idx = int(m.group(2))
            by_array.setdefault(arr_name, set()).add(idx)
            continue
        m2 = re.match(r"^notable_verbatim\[(\d+)\]$", v.field)
        if m2:
            notable_drop.add(int(m2.group(1)))
    for arr_name, idxs in by_array.items():
        items = extract.get(arr_name) or []
        extract[arr_name] = [x for i, x in enumerate(items) if i not in idxs]
    if notable_drop:
        nv = extract.get("notable_verbatim") or []
        extract["notable_verbatim"] = [x for i, x in enumerate(nv) if i not in notable_drop]
    return extract


# ------------------------------------------------------------------
# 2) synthesis citation 검증
# ------------------------------------------------------------------
_TICKER_BRACKET_RE = re.compile(r"\[([A-Z][A-Z0-9.\-]{0,9})\]")
_QUOTE_RE = re.compile(r'"([^"]{15,200})"')


def verify_synthesis_citations(
    synthesis_text: str | None,
    payload_text: str | None,
    allowed_tickers: list[str] | None = None,
    threshold: float = 0.25,
) -> GateResult:
    """synthesis 출력의 [TICKER]·"..." 인용이 payload에 근거 있는지 검증."""
    result = GateResult(layer="synthesis")
    if not synthesis_text or not payload_text:
        return result
    allowed = {t.upper() for t in (allowed_tickers or [])}
    if not allowed:
        # payload에서 ticker 추출 시도 ("### NVDA ..." 라벨 등)
        for m in _TICKER_BRACKET_RE.finditer(payload_text):
            allowed.add(m.group(1))
        for m in re.finditer(r"#{1,3}\s+([A-Z][A-Z0-9.\-]{0,9})\b", payload_text):
            allowed.add(m.group(1))
    # 잘 알려진 라벨류 (bracket인데 ticker 아닌 것)는 화이트리스트
    known_labels = {
        "TICKER", "SEC", "CALL", "VERIFY", "CONSENSUS_DELTA", "DEAL", "SUPPLY",
        "COMPETITOR", "MACRO", "GEOMIX", "PRE", "ACTUAL",
    }
    # 1) [TICKER] bracket 검증
    seen_tickers: set[str] = set()
    for m in _TICKER_BRACKET_RE.finditer(synthesis_text):
        t = m.group(1)
        if t in known_labels:
            continue
        if t in seen_tickers:
            continue
        seen_tickers.add(t)
        result.total_checked += 1
        if t not in allowed:
            result.violations.append(ViolationReport(
                layer="synthesis", field=f"[TICKER]={t}",
                fragment=t, reason="ticker_not_in_payload",
            ))
    # 2) 큰따옴표 인용 검증 (longer than 15 chars to skip generic phrases)
    norm_payload = _normalize(payload_text)
    quote_checked = 0
    quote_violations: list[ViolationReport] = []
    for m in _QUOTE_RE.finditer(synthesis_text):
        q = m.group(1)
        # 한국어 prose 안의 영문 인용만 검증 (한국어 일반 표현은 환각 평가 어려움)
        if not re.search(r"[a-zA-Z]", q):
            continue
        quote_checked += 1
        if not _frag_in(q, payload_text, head_chars=50):
            quote_violations.append(ViolationReport(
                layer="synthesis", field=f'quote: "{q[:40]}..."',
                fragment=q[:80], reason="quote_not_in_payload",
            ))
    result.total_checked += quote_checked
    result.violations.extend(quote_violations)
    if result.total_checked > 0:
        result.violation_ratio = len(result.violations) / result.total_checked
    result.passed = result.violation_ratio < threshold
    return result


# ------------------------------------------------------------------
# 3) 길이 보장
# ------------------------------------------------------------------
def enforce_min_length(text: str | None, min_chars: int = 8000) -> tuple[bool, int]:
    """text 길이 보장. 반환: (passed, actual_chars)."""
    if not text:
        return False, 0
    n = len(text)
    return n >= min_chars, n


# ------------------------------------------------------------------
# 4) 미공개 셀 lint (Phase 8 Track Q)
# ------------------------------------------------------------------
_UNDISCLOSED_RE = re.compile(r"(수치|정량)\s*미공개|\(\s*미공개\s*\)")


def verify_no_undisclosed(synthesis_text: str | None) -> GateResult:
    """합성 본문에 '수치 미공개' / '정량 미공개' / '(미공개)' 가 등장하는지.

    표 셀에 비정량 산문을 채우는 게 가장 흔한 환각 회피 패턴 — 차단해야 PM이 표를 읽을 수 있다.
    """
    result = GateResult(layer="undisclosed_lint")
    if not synthesis_text:
        return result
    matches = list(_UNDISCLOSED_RE.finditer(synthesis_text))
    result.total_checked = 1
    if matches:
        for m in matches[:10]:
            ctx_start = max(0, m.start() - 40)
            ctx_end = min(len(synthesis_text), m.end() + 40)
            result.violations.append(ViolationReport(
                layer="undisclosed_lint",
                field=f"pos={m.start()}",
                fragment=synthesis_text[ctx_start:ctx_end].replace("\n", " "),
                reason="undisclosed_cell_phrase",
            ))
        result.violation_ratio = 1.0
        result.passed = False
    return result


# ------------------------------------------------------------------
# 5) Truncation 감지 (Phase 8 Track Q)
# ------------------------------------------------------------------
_SAFE_END_CHARS = set(".다요?)」』』。」?!*]")  # 자연스러운 종결
_SECTION_RE = re.compile(r"^##\s", re.MULTILINE)


def looks_truncated(text: str | None, expected_sections: int = 8) -> tuple[bool, str]:
    """합성 본문이 잘렸을 가능성. 반환: (잘림_의심, 사유)."""
    if not text or len(text) < 200:
        return True, f"len<200 ({len(text or '')})"
    tail = text.rstrip()
    last = tail[-1] if tail else ""
    if last not in _SAFE_END_CHARS:
        return True, f"tail_char={last!r}"
    sections = len(_SECTION_RE.findall(text))
    if sections < expected_sections:
        return True, f"sections={sections}<{expected_sections}"
    return False, ""


# ------------------------------------------------------------------
# 메시지 헬퍼
# ------------------------------------------------------------------
def fmt_quality_summary(
    extract_results: dict[str, GateResult] | None,
    synthesis_result: GateResult | None,
    synthesis_chars: int | None = None,
) -> str:
    """텔레그램용 헬로 — 환각 검증 결과 요약 한 블록."""
    lines = ["🛡️ 환각 검증 결과"]
    if extract_results:
        for t, r in extract_results.items():
            badge = "✅" if r.passed else "⚠️"
            lines.append(f"  {badge} {t} extract: 위반 {len(r.violations)}/{r.total_checked} ({r.violation_ratio*100:.0f}%)")
    if synthesis_result is not None:
        badge = "✅" if synthesis_result.passed else "⚠️"
        ratio = synthesis_result.violation_ratio * 100
        lines.append(f"  {badge} synthesis 인용: 위반 {len(synthesis_result.violations)}/{synthesis_result.total_checked} ({ratio:.0f}%)")
    if synthesis_chars is not None:
        lines.append(f"  📏 synthesis 길이: {synthesis_chars:,}자")
    return "\n".join(lines) if len(lines) > 1 else ""


def quality_payload(
    extract_results: dict[str, GateResult] | None,
    synthesis_result: GateResult | None,
    synthesis_chars: int | None,
) -> dict[str, Any]:
    """history.save_call의 _quality 필드용 직렬화."""
    out: dict[str, Any] = {}
    if extract_results:
        out["extract"] = {
            t: {
                "total": r.total_checked,
                "violations": len(r.violations),
                "ratio": r.violation_ratio,
                "passed": r.passed,
            }
            for t, r in extract_results.items()
        }
    if synthesis_result is not None:
        out["synthesis_citations"] = {
            "total": synthesis_result.total_checked,
            "violations": len(synthesis_result.violations),
            "ratio": synthesis_result.violation_ratio,
            "passed": synthesis_result.passed,
        }
    if synthesis_chars is not None:
        out["synthesis_chars"] = synthesis_chars
    return out
