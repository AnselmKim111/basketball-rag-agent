"""matplotlib 분기별 바그래프 PNG 생성.

격리: 무거운 matplotlib import는 build() 함수 본문에서만.
폴백:
  - 사업부별 매출 없음 → 회사 전체 매출 단일 시리즈
  - forward 없음 → forward 4분기 자리 비움
  - matplotlib 자체 실패 → 호출자가 None 처리 (예외 던지지 않음)
"""

from __future__ import annotations

import io
import logging
from typing import Optional

log = logging.getLogger(__name__)


def build(
    company_name: str,
    revenue_qoq: dict[str, int],          # 회사 전체 매출 분기별
    op_profit_qoq: dict[str, int],        # 회사 전체 영업이익
    net_profit_qoq: dict[str, int],       # 회사 전체 순이익
    segment_revenue: Optional[dict[str, dict[str, int]]] = None,
    # ↑ {"분기키": {"사업부": 매출}} - 없으면 회사 전체로 폴백
    forward: Optional[dict[str, dict[str, Optional[float]]]] = None,
    # ↑ {"분기키": {"revenue":..., "op_profit":..., "net_profit":...}}
) -> Optional[bytes]:
    """PNG 이미지 bytes 반환. 실패 시 None."""
    try:
        return _build_inner(
            company_name, revenue_qoq, op_profit_qoq, net_profit_qoq,
            segment_revenue, forward,
        )
    except Exception:
        log.exception("차트 생성 실패")
        return None


def _build_inner(
    company_name: str,
    revenue_qoq: dict[str, int],
    op_profit_qoq: dict[str, int],
    net_profit_qoq: dict[str, int],
    segment_revenue: Optional[dict[str, dict[str, int]]],
    forward: Optional[dict[str, dict[str, Optional[float]]]],
) -> Optional[bytes]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager, rcParams

    # 한국어 폰트
    for cand in ("Noto Sans CJK KR", "NanumGothic", "Noto Sans CJK JP", "DejaVu Sans"):
        try:
            font_manager.findfont(cand, fallback_to_default=False)
            rcParams["font.sans-serif"] = [cand]
            log.info("matplotlib 폰트: %s", cand)
            break
        except Exception:
            continue
    rcParams["axes.unicode_minus"] = False

    if not revenue_qoq:
        log.warning("revenue 데이터 없음 — 차트 스킵")
        return None

    # X축 분기 정렬 (실적 + forward 합치고 시간순)
    quarters_actual = sorted(revenue_qoq.keys())
    quarters_forward = sorted((forward or {}).keys())
    quarters = quarters_actual + [q for q in quarters_forward if q not in revenue_qoq]
    n_actual = len(quarters_actual)

    # 단위: 원 → 조원
    def to_trillion(v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        return v / 1_000_000_000_000  # 1조원

    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)

    # ---------- 위: 매출 (사업부별 stacked OR 회사 전체) ----------
    note_segment = ""
    if segment_revenue and any(segment_revenue.values()):
        # stacked bar
        # 모든 사업부 이름 추출
        seg_names: list[str] = []
        for q_data in segment_revenue.values():
            for s in q_data:
                if s not in seg_names:
                    seg_names.append(s)
        bottoms = [0.0] * len(quarters)
        for seg in seg_names:
            heights = [to_trillion(segment_revenue.get(q, {}).get(seg, 0)) or 0 for q in quarters]
            colors = ["#4C9EEB" if i < n_actual else "#A8CFE8" for i in range(len(quarters))]
            ax_top.bar(quarters, heights, bottom=bottoms, label=seg, edgecolor="white", linewidth=0.5)
            bottoms = [b + h for b, h in zip(bottoms, heights)]
        ax_top.legend(loc="upper left", fontsize=8, ncol=2)
    else:
        heights = [
            to_trillion(revenue_qoq.get(q, (forward or {}).get(q, {}).get("revenue") or 0))
            for q in quarters
        ]
        colors = ["#4C9EEB" if i < n_actual else "#A8CFE8" for i in range(len(quarters))]
        ax_top.bar(quarters, heights, color=colors, edgecolor="white", linewidth=0.5)
        note_segment = "  (사업부별 분해 미제공)"

    ax_top.set_title(f"{company_name} 분기별 매출 (조원){note_segment}", fontsize=12)
    ax_top.set_ylabel("매출 (조원)")
    ax_top.grid(True, axis="y", alpha=0.3)
    ax_top.tick_params(axis="x", rotation=45, labelsize=8)
    if n_actual < len(quarters):
        ax_top.axvline(x=n_actual - 0.5, color="gray", linestyle="--", alpha=0.5)

    # ---------- 아래: 영업이익 + 순이익 grouped ----------
    op_vals = [to_trillion(op_profit_qoq.get(q, (forward or {}).get(q, {}).get("op_profit"))) for q in quarters]
    net_vals = [to_trillion(net_profit_qoq.get(q, (forward or {}).get(q, {}).get("net_profit"))) for q in quarters]

    x = list(range(len(quarters)))
    width = 0.4
    op_plot = [v if v is not None else 0 for v in op_vals]
    net_plot = [v if v is not None else 0 for v in net_vals]
    ax_bot.bar([i - width / 2 for i in x], op_plot, width, label="영업이익", color="#1F77B4")
    ax_bot.bar([i + width / 2 for i in x], net_plot, width, label="당기순이익", color="#FF7F0E")

    ax_bot.set_title(f"{company_name} 분기별 영업이익·순이익 (조원, 회사 전체)", fontsize=12)
    ax_bot.set_ylabel("이익 (조원)")
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels(quarters, rotation=45, fontsize=8)
    ax_bot.legend(loc="upper left", fontsize=9)
    ax_bot.grid(True, axis="y", alpha=0.3)
    ax_bot.axhline(0, color="black", linewidth=0.5)
    if n_actual < len(quarters):
        ax_bot.axvline(x=n_actual - 0.5, color="gray", linestyle="--", alpha=0.5)

    if n_actual < len(quarters):
        fig.text(
            0.5, 0.005,
            "─ 좌: 실적 / 우: Forward (Consensus 추정)",
            ha="center", fontsize=8, style="italic",
        )
    elif forward is None or not forward:
        fig.text(
            0.5, 0.005,
            "(Forward Consensus 미수집)",
            ha="center", fontsize=8, style="italic", color="gray",
        )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
