"""숫자 교차검증 — 어닝콜 추출 숫자 ↔ SEC EDGAR 대조.

전문 추출(LLM)의 headline 숫자가 실제 SEC 보고치와 맞는지 규칙 기반으로 확인.
불일치는 환각/정의차이 신호 → 보고서에 flag로 표시 (스크리너 cross-validation 철학).

best-effort: 분기 매칭 실패 시 graceful skip. 절대 raise 안 함.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 허용 오차 — 매출은 타이트, capex는 정의 차이(리스 포함 여부 등)로 느슨하게
REVENUE_TOLERANCE = 0.05   # 5%
CAPEX_TOLERANCE = 0.15     # 15%


@dataclass
class VerifyResult:
    ticker: str
    metric: str            # "revenue" | "capex"
    status: str            # "match" | "mismatch" | "unavailable"
    message: str


def _parse_money(s: str | None) -> float | None:
    """'$65.6B', '124.3 billion', '$1,234M' → float USD. 실패 시 None."""
    if not s:
        return None
    txt = str(s).strip().lower().replace(",", "").replace("$", "")
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(b|bn|billion|m|mn|million|k|thousand|t|trillion)?", txt)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2) or ""
    mult = {
        "b": 1e9, "bn": 1e9, "billion": 1e9,
        "m": 1e6, "mn": 1e6, "million": 1e6,
        "k": 1e3, "thousand": 1e3,
        "t": 1e12, "trillion": 1e12,
    }.get(unit, 1.0)
    # 단위 없는 큰 숫자는 그대로 (이미 절대값일 수 있음)
    return val * mult


def _compare(ticker: str, metric: str, call_val: float | None, sec_val: float | None, tol: float) -> VerifyResult:
    from src.earnings.sec_edgar import fmt_usd
    if call_val is None or sec_val is None or sec_val == 0:
        return VerifyResult(ticker, metric, "unavailable",
                            f"{ticker} {metric}: 대조 불가 (콜 또는 SEC 값 없음)")
    diff = (call_val - sec_val) / abs(sec_val)
    if abs(diff) <= tol:
        return VerifyResult(ticker, metric, "match",
                            f"✅ {ticker} {metric} 일치: 콜 {fmt_usd(call_val)} vs SEC {fmt_usd(sec_val)} ({diff*100:+.1f}%)")
    return VerifyResult(ticker, metric, "mismatch",
                        f"⚠️ {ticker} {metric} 불일치: 콜 {fmt_usd(call_val)} vs SEC {fmt_usd(sec_val)} "
                        f"({diff*100:+.1f}%) — 정의 차이/환각 가능")


def cross_check(extract: dict, financials, year: int | None, quarter: int | None) -> list[VerifyResult]:
    """단일 종목 검증. financials: CompanyFinancials. year/quarter 매칭 분기로 대조.

    분기 정보 없거나 SEC 분기 데이터 못 찾으면 unavailable 결과만 반환.
    """
    ticker = extract.get("ticker", "?")
    results: list[VerifyResult] = []
    if not extract or extract.get("extract_failed"):
        return results
    if financials is None or not year or not quarter:
        return results

    hn = extract.get("headline_numbers") or {}

    # 매출
    sec_rev = financials.find_quarter(financials.q_revenue, year, quarter)
    results.append(_compare(
        ticker, "revenue",
        _parse_money(hn.get("revenue_actual")),
        sec_rev.val if sec_rev else None,
        REVENUE_TOLERANCE,
    ))

    # capex
    sec_cap = financials.find_quarter(financials.q_capex, year, quarter)
    results.append(_compare(
        ticker, "capex",
        _parse_money(hn.get("capex_quarter")),
        sec_cap.val if sec_cap else None,
        CAPEX_TOLERANCE,
    ))

    return results


def format_results(all_results: list[VerifyResult]) -> str:
    """검증 결과 → 사람이 읽는 텍스트 (텔레그램/페이로드용)."""
    if not all_results:
        return ""
    mism = [r for r in all_results if r.status == "mismatch"]
    match = [r for r in all_results if r.status == "match"]
    lines = ["🔍 숫자 교차검증 (콜 ↔ SEC)"]
    for r in mism:
        lines.append(f"  {r.message}")
    for r in match:
        lines.append(f"  {r.message}")
    if not mism:
        lines.append("  (불일치 없음)")
    return "\n".join(lines)
