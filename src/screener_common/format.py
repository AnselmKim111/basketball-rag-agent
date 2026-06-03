"""숫자 포맷터 — 퍼센트, 원, 달러."""
from __future__ import annotations


def fmt_pct(v) -> str:
    """퍼센트 (±X.X% 또는 N/A)."""
    return f"{v:+.1f}%" if isinstance(v, (int, float)) else "N/A"


def fmt_won(v) -> str:
    """원 → 조원/억원 표기."""
    if not isinstance(v, (int, float)) or v <= 0:
        return "N/A"
    if v >= 1e12:
        return f"{v / 1e12:.1f}조원"
    if v >= 1e8:
        return f"{v / 1e8:,.0f}억원"
    return f"{v:,.0f}원"


def fmt_usd(v) -> str:
    """USD → $XB / $XM / $X."""
    if not isinstance(v, (int, float)) or v <= 0:
        return "N/A"
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"
