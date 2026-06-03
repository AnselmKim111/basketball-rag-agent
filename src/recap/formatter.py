"""recap 메시지·차트 포맷 — 텔레그램에 발송할 텍스트와 PNG 생성.

매우 보수적: matplotlib import는 함수 안으로 지연 — 미사용 환경 부담 0.
"""

from __future__ import annotations

import io
import logging
from typing import Any

log = logging.getLogger(__name__)


_SIGNAL_LABEL = {
    "high_all": "🚀 역사적 신고가",
    "high_52w": "📈 52주 신고가",
    "volume_breakout": "🔥 거래량 돌파",
    "near_breakout_52w": "🎯 52주 돌파 직전",
    "vcp_breakout": "💎 VCP 돌파",
}


def format_recap_text(data: dict[str, Any], synthesis_md: str = "") -> str:
    """sonnet 합성(synthesis_md) 위에 4섹션 raw 요약을 얹어 텔레그램 메시지 생성.

    sonnet이 비었으면 raw 요약만으로 발송 (graceful)."""
    parts: list[str] = []
    today = data.get("today", "")
    lookback = data.get("lookback_days", 7)
    parts.append(f"📋 *주간 회고* — {today} 기준 최근 {lookback}일")

    # §1 시그널
    sig = data.get("signal") or {}
    summary = sig.get("summary") or {}
    stats = sig.get("stats") or []
    parts.append("\n🎯 *시그널 사후 추적*")
    if sig.get("empty_reason"):
        parts.append(f"  ⚠️ {sig['empty_reason']}")
    elif not stats:
        parts.append("  (히트 0건)")
    else:
        parts.append(
            f"  • 총 hit: {summary.get('total_hits', 0)}건 / "
            f"평균 {summary.get('overall_avg_pnl', 0):.2f}% / "
            f"win rate {summary.get('overall_win_rate', 0) * 100:.0f}%"
        )
        for s in stats:
            label = _SIGNAL_LABEL.get(s.signal, s.signal)
            parts.append(
                f"  {label}: {s.hit_count}건 | "
                f"평균 {s.avg_pnl:+.1f}% | win {s.win_rate * 100:.0f}%"
            )
            if s.top:
                top_str = ", ".join(
                    f"{h.name or h.ticker}({h.pnl_pct:+.1f}%)" for h in s.top[:3]
                )
                parts.append(f"     ↑ {top_str}")
            if s.bottom:
                bot_str = ", ".join(
                    f"{h.name or h.ticker}({h.pnl_pct:+.1f}%)" for h in s.bottom[:3]
                )
                parts.append(f"     ↓ {bot_str}")

    # §2 IdeaBot picks
    ideas = data.get("ideas") or {}
    parts.append(f"\n💡 *IdeaBot picks 후속* ({ideas.get('entry_count', 0)}개 idea)")
    for ent in (ideas.get("entries") or [])[:5]:
        idea_text = ent.get("idea_text", "")[:60]
        parts.append(f"  • _{idea_text}_")
        for p in (ent.get("picks") or [])[:3]:
            parts.append(f"     - {p['name']} ({p['ticker']})")

    # §3 공시 (placeholder)
    disc = data.get("disclosures") or {}
    if disc.get("empty_reason"):
        parts.append(f"\n📋 *공시 트리거*: {disc['empty_reason']}")

    # §4 테마
    themes = data.get("themes") or {}
    rising = themes.get("rising") or []
    fading = themes.get("fading") or []
    parts.append("\n📊 *테마 사이클* (지난 7일 vs 그 전 7일)")
    if themes.get("skipped"):
        parts.append("  (wisereport 미접근 — 다음 회고)")
    elif not rising:
        parts.append("  (산업 키워드 매칭 0)")
    else:
        if rising:
            top_rise = [
                f"{t['industry']}(+{t['delta']})"
                for t in rising[:5] if t["delta"] > 0
            ]
            if top_rise:
                parts.append("  ↑ 떠오르는: " + ", ".join(top_rise))
        if fading:
            top_fade = [
                f"{t['industry']}({t['delta']})"
                for t in fading[:5] if t["delta"] < 0
            ]
            if top_fade:
                parts.append("  ↓ 식는: " + ", ".join(top_fade))

    # §5 sonnet 합성 (있으면)
    if synthesis_md:
        parts.append("\n🧠 *학습 포인트* (sonnet)")
        parts.append(synthesis_md.strip())

    return "\n".join(parts)


def render_signal_bar_chart(stats: list, title: str = "신호별 평균 수익률") -> bytes | None:
    """SignalStats 리스트 → bar chart PNG bytes. 실패 시 None.

    matplotlib import는 함수 안 (호출 시점 부담)."""
    if not stats:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        log.exception("[recap] matplotlib import 실패")
        return None
    try:
        signals = [_SIGNAL_LABEL.get(s.signal, s.signal) for s in stats]
        avg_pnls = [s.avg_pnl for s in stats]
        colors = ["#2ecc71" if v > 0 else "#e74c3c" for v in avg_pnls]
        fig, ax = plt.subplots(figsize=(10, 5))
        bars = ax.bar(signals, avg_pnls, color=colors)
        for bar, val, s in zip(bars, avg_pnls, stats):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                val + (0.1 if val >= 0 else -0.3),
                f"{val:+.1f}%\n(n={s.hit_count}, win {s.win_rate * 100:.0f}%)",
                ha="center", va="bottom" if val >= 0 else "top",
                fontsize=9,
            )
        ax.axhline(0, color="black", linewidth=0.5)
        ax.set_ylabel("평균 PnL %")
        ax.set_title(title)
        plt.xticks(rotation=15, ha="right")
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120)
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception:
        log.exception("[recap] signal chart 생성 실패")
        return None


def render_theme_chart(themes: dict[str, Any], title: str = "테마 사이클") -> bytes | None:
    """rising/fading 테마 양옆 bar chart. 실패 시 None."""
    rising = themes.get("rising") or []
    if not rising:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        log.exception("[recap] matplotlib import 실패")
        return None
    try:
        # rising은 delta desc로 정렬됨. top 8을 그대로 사용 (음수도 일부 포함).
        items = rising[:8]
        labels = [t["industry"] for t in items]
        deltas = [t["delta"] for t in items]
        colors = ["#2ecc71" if d > 0 else "#e74c3c" if d < 0 else "#95a5a6" for d in deltas]
        fig, ax = plt.subplots(figsize=(10, max(5, len(items) * 0.5)))
        ax.barh(labels[::-1], deltas[::-1], color=colors[::-1])
        ax.axvline(0, color="black", linewidth=0.5)
        ax.set_xlabel("리포트 빈도 변화 (최근 7일 - 이전 7일)")
        ax.set_title(title)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120)
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception:
        log.exception("[recap] theme chart 생성 실패")
        return None
