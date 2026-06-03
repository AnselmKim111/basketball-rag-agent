"""글로벌 자산 위험선호 매트릭스 — 1페이지 시각 요약 (TradingView 룩).

상단: Risk-On/Off 게이지 바 + 점수
본체: 자산 클래스 5그룹(주식/채권/원자재/통화/암호) × 자산 셀
  - 셀 색: 1D % (TV 팔레트)
  - 셀 텍스트: 종목명·1D%·5D%
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.report.charts import chart_theme as theme

log = logging.getLogger(__name__)

_BG = "#131722"
_FG = "#d1d4dc"


def risk_matrix(rows: list[dict], gauge: dict, out_dir: Path,
                filename: str = "00_global_risk.png",
                date_iso: str | None = None) -> str | None:
    """rows: load_risk_map 반환. gauge: {score, label, signals}."""
    theme.setup()
    import matplotlib.pyplot as plt
    import numpy as np
    from src.report.charts.heatmap_chart import _tv_norm_cmap

    if not rows:
        log.warning("[risk] 자산 0개 — 스킵")
        return None

    norm, cmap = _tv_norm_cmap(vmin=-3, vmax=3)

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    group_order = ["주식", "채권", "원자재", "통화", "암호"]
    group_order = [g for g in group_order if g in groups]
    n_groups = len(group_order)

    fig = plt.figure(figsize=(15, 9.5))
    fig.patch.set_facecolor(_BG)

    score = int(gauge.get("score", 50))
    glabel = gauge.get("label", "")
    # 타이틀 (fig.text — clip 영향 없음)
    title = ("글로벌 위험선호 매트릭스 — 자산 클래스별 1D·5D 등락"
             + (f"  ·  기준 {date_iso}" if date_iso else ""))
    fig.text(0.5, 0.945, title, ha="center", va="top",
             fontsize=15.5, color="white", fontweight="bold")
    # 게이지 바 (전용 inset axes — 바만)
    gax = fig.add_axes([0.08, 0.875, 0.84, 0.025])
    gax.set_facecolor(_BG)
    grad = np.linspace(-3, 3, 256).reshape(1, -1)
    gax.imshow(grad, aspect="auto", cmap=cmap, alpha=0.95)
    gax.set_xticks([]); gax.set_yticks([])
    for sp in gax.spines.values():
        sp.set_visible(False)
    # 점수 마커 (게이지 바 위에 figure 좌표로 — 안전)
    marker_x = 0.08 + 0.84 * (score / 100.0)
    marker_y = 0.875 + 0.025 / 2
    fig.text(marker_x, marker_y, str(score), ha="center", va="center",
             fontsize=12, fontweight="bold", color="black",
             bbox=dict(boxstyle="circle,pad=0.32", facecolor="white", edgecolor="black", linewidth=2))
    # 양 끝 라벨
    fig.text(0.07, marker_y, "Risk-Off", fontsize=11, color="#f7525f",
             fontweight="bold", va="center", ha="right")
    fig.text(0.93, marker_y, "Risk-On", fontsize=11, color="#22ab94",
             fontweight="bold", va="center", ha="left")
    # 점수 + 레이블 (게이지 바 아래)
    fig.text(0.5, 0.852, f"{glabel}  ·  {score}/100",
             ha="center", va="top", fontsize=14, color="white", fontweight="bold")
    # 시그널 요약 (이모지 글리프 누락 대비 텍스트 마커)
    sigs = gauge.get("signals", {})
    on_sigs = [k for k, v in sigs.items() if v == 1]
    off_sigs = [k for k, v in sigs.items() if v == 0]
    pieces = []
    if on_sigs:
        pieces.append(f"[강세] {' · '.join(on_sigs)}")
    if off_sigs:
        pieces.append(f"[약세] {' · '.join(off_sigs)}")
    sig_txt = "      ".join(pieces)
    fig.text(0.5, 0.823, sig_txt, ha="center", va="top",
             fontsize=9, color=_FG)

    # 본체 — 그룹별 가로 행
    body = fig.add_axes([0.04, 0.05, 0.92, 0.70])
    body.set_facecolor(_BG); body.axis("off")
    body.set_xlim(0, 10); body.set_ylim(0, n_groups)

    for i, g in enumerate(group_order):
        lst = groups[g]
        y = n_groups - 1 - i
        # 그룹 라벨 (좌측)
        body.text(0.05, y + 0.5, g, fontsize=14, fontweight="bold",
                  color="#d6dae3", va="center", ha="left")
        # 자산 셀
        n_cells = len(lst)
        avail = 8.7  # 0.9~9.6 전후
        cell_w = avail / n_cells
        for j, r in enumerate(lst):
            x0 = 1.1 + j * cell_w
            d1 = r["d1"] if r["d1"] is not None else 0.0
            col = cmap(norm(max(-6, min(6, d1))))
            body.add_patch(plt.Rectangle((x0 + 0.04, y + 0.06),
                                         cell_w - 0.08, 0.88,
                                         facecolor=col, edgecolor=_BG, linewidth=2))
            cx = x0 + cell_w / 2
            cy = y + 0.5
            body.text(cx, cy + 0.27, r["label"], ha="center", va="center",
                      fontsize=10, color="white", fontweight="bold")
            d1s = f"{r['d1']:+.2f}%" if r['d1'] is not None else "—"
            body.text(cx, cy, d1s, ha="center", va="center",
                      fontsize=13, color="white", fontweight="bold")
            d5s = f"5D {r['d5']:+.1f}%" if r['d5'] is not None else ""
            body.text(cx, cy - 0.27, d5s, ha="center", va="center",
                      fontsize=8.5, color="white", alpha=0.88)

    # bbox_inches="tight"가 fig.text를 잘라내므로 직접 저장 (헤더 보존)
    import matplotlib.pyplot as plt
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        fig.savefig(str(out_dir / filename), dpi=110, facecolor=fig.get_facecolor())
        plt.close(fig)
        return filename
    except Exception:
        log.exception("[risk] save 실패")
        plt.close(fig)
        return None
