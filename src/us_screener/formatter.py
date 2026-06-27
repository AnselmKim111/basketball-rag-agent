"""US 신호 결과 → 텔레그램 메시지 (미미 스타일, 영문 종목).

KR `src/screener/formatter.py` 미러. 공통 헬퍼는 `src/screener_core/formatter.py`.
차이:
  - 헤더: "미국 주식 기술적 신호"
  - 시장 라벨: KOSPI/KOSDAQ → S&P500/NASDAQ100
  - 라벨: KR=name → US=ticker (심볼이 곧 식별자)
  - 섹터 fallback: "기타" → "Other"
"""
from __future__ import annotations

from datetime import datetime

from src.screener_core.formatter import (
    FormatConfig,
    fmt_kst_header,
    fmt_pct,
    format_section,
    sector_summary,
)


def _label_us(item: dict) -> str:
    """단순 라벨: 'AAPL(+5.2%)' — 미국은 심볼이 곧 식별자."""
    sym = item.get("ticker", "")
    return f"{sym}({fmt_pct(item.get('chg_pct', 0.0))})"


_CFG = FormatConfig(
    fallback_sector="Other",
    priority_market="S&P500",
    per_category_env="US_SCREENER_PER_CATEGORY_TOP",
    label_fn=_label_us,
)


def format_results(
    results: dict[str, list[dict]],
    as_of: datetime,
    base_date: str | None = None,
    stats: dict | None = None,
) -> str:
    parts: list[str] = []
    parts.append(f"🇺🇸 미국 주식 기술적 신호 — {fmt_kst_header(as_of)}")
    if base_date:
        as_of_iso = as_of.strftime("%Y-%m-%d")
        if base_date != as_of_iso:
            parts.append(f"📊 기준일: {base_date} (미국 장마감 종가)")
        else:
            parts.append(f"📊 기준일: {base_date} 당일 종가")
    if stats:
        proc = stats.get("processed", 0)
        skipped_no_base = stats.get("skipped_no_base", 0)
        validated = stats.get("validated", -1)
        rejected = stats.get("rejected", -1)
        verify_lines = [f"✓ {proc}종목 신호 계산 (시총 $2B+)"]
        if skipped_no_base > 0:
            verify_lines[-1] += f" · {skipped_no_base}종목 base_date 데이터 누락"
        if validated >= 0:
            v_line = f"✓ 이중확인: 재 fetch로 {validated}종목 정합성 통과"
            if rejected > 0:
                v_line += f" · {rejected}종목 불일치/누락 제외"
            verify_lines.append(v_line)
        parts.append("\n".join(verify_lines))
    parts.append("(NYSE+NASDAQ 보통주 · 섹터별 분류 · 시총·상승률 복합 정렬)\n")

    all_signals: list[dict] = []
    for v in results.values():
        all_signals.extend(v)
    sec_summary = sector_summary(all_signals, _CFG)
    if sec_summary:
        parts.append(f"🏷️ 주요 섹터: {sec_summary}\n")

    parts.append(format_section(results.get("high_all", []), "🚀", "역사적 신고가", _CFG))
    parts.append(format_section(results.get("high_52w", []), "📈", "52주 신고가", _CFG))
    parts.append(format_section(results.get("vcp_breakout", []), "💎", "VCP 돌파 (최근 2주 이내)", _CFG))
    parts.append(format_section(results.get("near_breakout_52w", []), "🎯", "52주 돌파 직전 95-99%", _CFG))

    total = sum(len(v) for v in results.values())
    if total == 0:
        parts.append("\n오늘은 신호 발생 종목이 없습니다.")

    return "\n".join(parts)
