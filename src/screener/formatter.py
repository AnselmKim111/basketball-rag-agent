"""KR 신호 결과 → 텔레그램 메시지 (미미 스타일).

미미 스타일(https://t.me/mimi_ATH 참고)의 섹터 그룹·% 포맷·헤더는
`src/screener_core/formatter.py`에 공통. 본 모듈은 KR 헤더·시장 라벨만 담당.
최종 텍스트는 send_text_chunked가 4000자 청크로 분할.
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


def _label_kr(item: dict) -> str:
    """미미 스타일 라벨: '삼성전자(+5.2%)' — KR은 한글 종목명을 표시."""
    name = item.get("name") or item.get("ticker", "")
    return f"{name}({fmt_pct(item.get('chg_pct', 0.0))})"


_CFG = FormatConfig(
    fallback_sector="기타",
    priority_market="KOSPI",
    per_category_env="SCREENER_PER_CATEGORY_TOP",
    label_fn=_label_kr,
)


def format_results(
    results: dict[str, list[dict]],
    as_of: datetime,
    base_date: str | None = None,
    stats: dict | None = None,
) -> str:
    """미미 스타일 메시지 포맷.

    base_date(YYYY-MM-DD): 신호 계산에 쓰인 OHLCV 종가 날짜.
    stats: signals.compute_all이 반환한 dict — 검증된 종목 수 / 누락 종목 수 표시.
    """
    parts: list[str] = []
    parts.append(f"📈 한국 주식 기술적 신호 — {fmt_kst_header(as_of)}")
    if base_date:
        as_of_iso = as_of.strftime("%Y-%m-%d")
        if base_date != as_of_iso:
            parts.append(f"📊 기준일: {base_date} (장마감 종가)")
        else:
            parts.append(f"📊 기준일: {base_date} 당일 종가")
    if stats:
        proc = stats.get("processed", 0)
        skipped_no_base = stats.get("skipped_no_base", 0)
        validated = stats.get("validated", -1)
        rejected = stats.get("rejected", -1)
        verify_lines = [f"✓ {proc}종목 신호 계산 (시총 3000억+)"]
        if skipped_no_base > 0:
            verify_lines[-1] += f" · {skipped_no_base}종목 base_date 데이터 누락"
        if validated >= 0:
            v_line = f"✓ 이중확인: Naver 재 fetch로 {validated}종목 정합성 통과"
            if rejected > 0:
                v_line += f" · {rejected}종목 불일치/누락 제외"
            verify_lines.append(v_line)
        parts.append("\n".join(verify_lines))
    parts.append("(KOSPI/KOSDAQ · 섹터별 분류 · 시총·상승률 복합 정렬)\n")

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
