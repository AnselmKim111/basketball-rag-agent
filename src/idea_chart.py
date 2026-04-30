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

    fig, ax = plt.subplots(figsize=(13.5, 9.5), dpi=120)

    # ── Jitter: 같은 (round(x), round(y)) 그룹의 점들을 원형 패턴으로 분산 ──
    # 같은 점수의 종목이 한 점에 뭉쳐 라벨이 깨지는 문제를 시각적으로 해소.
    # 점수 의미는 보존 (그룹 중심은 원래 좌표).
    import math as _math
    from collections import defaultdict as _dd
    cluster: dict[tuple[int, int], list[int]] = _dd(list)
    for idx, (x, y) in enumerate(zip(xs, ys)):
        cluster[(int(round(x)), int(round(y)))].append(idx)
    jx = list(xs)
    jy = list(ys)
    for (_, _), idxs in cluster.items():
        n = len(idxs)
        if n <= 1:
            continue
        # 종목 수에 따라 반지름 조정. n=2~3은 0.20, 5+는 0.32 정도
        radius = min(0.18 + 0.04 * (n - 1), 0.42)
        for j, idx in enumerate(idxs):
            angle = 2 * _math.pi * j / n
            jx[idx] = xs[idx] + radius * _math.cos(angle)
            jy[idx] = ys[idx] + radius * _math.sin(angle)

    # 점 크기는 80~700 범위로 매핑 (margin 1~10) — 라벨 공간 확보 위해 살짝 작게
    point_sizes = [80 + (s - 1) * (620 / 9) for s in sizes]

    sc = ax.scatter(
        jx, jy,
        s=point_sizes,
        c=colors,
        cmap="viridis",
        vmin=1, vmax=10,
        alpha=0.6,
        edgecolors="black",
        linewidths=0.7,
    )

    # 라벨: 점에서 fan-out. 같은 클러스터 내 종목은 시계방향 12/3/6/9시 등.
    for (_, _), idxs in cluster.items():
        n = len(idxs)
        for j, idx in enumerate(idxs):
            x = jx[idx]
            y = jy[idx]
            name_ = names[idx]
            # 클러스터 중심으로부터의 방향각으로 offset 결정 (점이 중심에서 밀려난 방향으로 라벨)
            if n > 1:
                angle = 2 * _math.pi * j / n
                ox = 14 * _math.cos(angle)
                oy = 14 * _math.sin(angle)
                ha = "left" if ox >= 0 else "right"
                va = "bottom" if oy >= 0 else "top"
            else:
                ox, oy = 10, 6
                ha, va = "left", "bottom"
            ax.annotate(
                name_[:14], (x, y),
                xytext=(ox, oy), textcoords="offset points",
                fontsize=11, fontweight="bold", color="black",
                ha=ha, va=va,
                bbox=dict(
                    boxstyle="round,pad=0.3",
                    facecolor="white", edgecolor="dimgray",
                    alpha=0.92, linewidth=0.6,
                ),
                arrowprops=dict(
                    arrowstyle="-", color="gray", alpha=0.5, linewidth=0.6,
                ),
                zorder=5,
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
