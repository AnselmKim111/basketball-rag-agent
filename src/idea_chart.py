"""IdeaBot용 영업레버리지 4축 산점도 생성.

격리:
  - 무거운 matplotlib import는 build() 본문 안에서만
  - 한국어 폰트는 Dockerfile의 fonts-noto-cjk 사용
  - 차트 생성 실패 시 None 반환 — 호출자가 graceful 처리

축 매핑 (4D → 2D + 크기 + 색):
  - X축: growth_acceleration (1~10) — 아이디어 발현 시 매출 가속
  - Y축: fixed_cost_share (1~10) — 구조적 영업레버리지
  - 점 크기: margin_sensitivity (1~10) — 손익분기 근접도/마진 squeeze
  - 점 색: capacity_room (1~10) — 가동률 여유

오른쪽 위 + 큰 점 + 진한 색 = 영업레버리지가 가장 세게 걸리는 zone.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

log = logging.getLogger(__name__)


def build(idea_text: str, all30_scored: list[dict]) -> Optional[bytes]:
    """30종목 4축 산점도 PNG bytes. 실패 시 None.

    all30_scored: [
      {"ticker6": "...", "name": "...",
       "scores": {"fixed_cost": int, "capacity": int, "growth": int, "margin": int}},
      ...
    ]
    """
    if not all30_scored:
        return None
    try:
        return _build_inner(idea_text, all30_scored)
    except Exception:
        log.exception("산점도 생성 실패")
        return None


def _build_inner(idea_text: str, all30_scored: list[dict]) -> Optional[bytes]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 한국어 폰트 (Dockerfile의 fonts-noto-cjk)
    for fp in font_manager.findSystemFonts(fontpaths=None):
        low = fp.lower()
        if any(k in low for k in ("notosanscjk", "notosanskr", "nanumgothic")):
            try:
                font_manager.fontManager.addfont(fp)
                plt.rcParams["font.family"] = font_manager.FontProperties(fname=fp).get_name()
                break
            except Exception:
                continue
    plt.rcParams["axes.unicode_minus"] = False

    # 데이터 추출 (점수 누락 시 5로 기본)
    xs, ys, sizes, colors, names = [], [], [], [], []
    for item in all30_scored:
        if not isinstance(item, dict):
            continue
        s = item.get("scores") or {}
        try:
            x = float(s.get("growth", 5))
            y = float(s.get("fixed_cost", 5))
            sz = float(s.get("margin", 5))
            cl = float(s.get("capacity", 5))
        except (TypeError, ValueError):
            continue
        xs.append(_clamp(x, 1, 10))
        ys.append(_clamp(y, 1, 10))
        sizes.append(_clamp(sz, 1, 10))
        colors.append(_clamp(cl, 1, 10))
        names.append(item.get("name", "?"))

    if not xs:
        return None

    fig, ax = plt.subplots(figsize=(11.5, 8.5), dpi=120)

    # 점 크기는 100~900 범위로 매핑 (margin 1~10)
    point_sizes = [100 + (s - 1) * (800 / 9) for s in sizes]

    sc = ax.scatter(
        xs, ys,
        s=point_sizes,
        c=colors,
        cmap="viridis",
        vmin=1, vmax=10,
        alpha=0.55,  # 점 약간 투명 → 라벨 가독성 우선
        edgecolors="black",
        linewidths=0.7,
    )

    # 라벨: 회사명을 점 옆에 — 동일 (x,y) 좌표에 여러 종목 겹치면 위·아래로 fan-out
    from collections import defaultdict
    cluster: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, (x, y) in enumerate(zip(xs, ys)):
        cluster[(int(round(x)), int(round(y)))].append(idx)
    placed_labels: list[tuple[float, float]] = []
    for (cx, cy), idxs in cluster.items():
        for j, idx in enumerate(idxs):
            x = xs[idx]
            y = ys[idx]
            n = names[idx]
            # fan-out: 겹치면 위·아래로 흩어줌
            offset_y = 8 + j * 14 if j % 2 == 0 else -(8 + j * 14)
            offset_x = 8 if j % 2 == 0 else -8
            ha = "left" if j % 2 == 0 else "right"
            ax.annotate(
                n[:12], (x, y),
                xytext=(offset_x, offset_y), textcoords="offset points",
                fontsize=10, fontweight="bold", alpha=0.95,
                ha=ha, va="center",
                bbox=dict(
                    boxstyle="round,pad=0.25",
                    facecolor="white", edgecolor="gray",
                    alpha=0.85, linewidth=0.5,
                ),
                arrowprops=dict(
                    arrowstyle="-", color="gray", alpha=0.4, linewidth=0.5,
                ),
            )

    # 영업레버리지 강도 zone — 우상단을 강조
    ax.axhline(y=7, color="gray", linestyle=":", alpha=0.4, linewidth=0.8)
    ax.axvline(x=7, color="gray", linestyle=":", alpha=0.4, linewidth=0.8)
    ax.fill_between(
        [7, 10.5], 7, 10.5, color="red", alpha=0.06, zorder=0,
    )
    ax.text(
        9.6, 9.6, "★\n강한 OL\nzone",
        fontsize=9, ha="center", va="center",
        color="darkred", alpha=0.55, fontweight="bold",
    )

    # 축
    ax.set_xlim(0.5, 10.5)
    ax.set_ylim(0.5, 10.5)
    ax.set_xticks(range(1, 11))
    ax.set_yticks(range(1, 11))
    ax.grid(True, alpha=0.25, linestyle="--")
    ax.set_xlabel("매출 성장 가속도 (1~10) — 아이디어 발현 시 매출이 얼마나 가속되나", fontsize=10)
    ax.set_ylabel("고정비 비중 (1~10) — 구조적 영업레버리지", fontsize=10)

    # 제목 + 부제
    title = f"30 후보 종목 — 영업레버리지 4축 산점도"
    ax.set_title(title, fontsize=13, pad=15, fontweight="bold")
    fig.text(
        0.5, 0.94,
        f"아이디어: {idea_text[:80]}{'...' if len(idea_text) > 80 else ''}",
        ha="center", fontsize=9, color="gray", style="italic",
    )

    # 색 범례 (capacity)
    cbar = plt.colorbar(sc, ax=ax, fraction=0.04, pad=0.02)
    cbar.set_label("가동률 여유 (1~10) — 캐파 룸이 클수록 진한 색", fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    # 크기 범례 (margin) — 별도 텍스트
    fig.text(
        0.02, 0.02,
        "● 점 크기 = 마진 민감도 (큼=BEP 근접, 매출↑ → OP 폭발적 증폭)",
        fontsize=8, color="dimgray",
    )

    fig.tight_layout(rect=(0, 0.04, 1, 0.92))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
