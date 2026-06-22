"""차트 공통 테마 — 한글 폰트 + 흰 배경 + 16:9 기본 스타일.

모든 report 차트는 setup() 1회 호출 후 그림. matplotlib Agg(headless).
캡션은 차트 하단 또는 리포트 본문에서 관찰/해석/체크 3줄로 별도 작성.

모바일 우선 가독성 — figsize 기본값 1.4배 적용 (DEFAULT_FIGSIZE).
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SETUP_DONE = False

# 모바일 가독성 우선 — 모든 차트의 base figsize. 호출자가 명시하면 override.
DEFAULT_FIGSIZE = (16.0, 9.0)  # 기존 ~12x7 → 1.4배 확대
PORTRAIT_FIGSIZE = (12.0, 14.0)  # 세로형 (히트맵·종목 grid 등)
SQUARE_FIGSIZE = (13.0, 13.0)


def setup() -> None:
    """matplotlib Agg + 한글 폰트 + 흰 배경 1회 설정 (idempotent)."""
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 한글 폰트 (Dockerfile fonts-noto-cjk). 미발견이어도 영문은 그려짐.
    for fp in font_manager.findSystemFonts(fontpaths=None):
        low = fp.lower()
        if any(k in low for k in ("notosanscjk", "notosanskr", "nanumgothic")):
            try:
                font_manager.fontManager.addfont(fp)
                plt.rcParams["font.family"] = font_manager.FontProperties(fname=fp).get_name()
                break
            except Exception:
                continue
    plt.rcParams.update({
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "axes.grid": True,
        "grid.alpha": 0.25,
        "axes.unicode_minus": False,
        "font.size": 13,           # 모바일 가독성 (11 → 13)
        "axes.titlesize": 16,
        "axes.labelsize": 13,
        "xtick.labelsize": 11,
        "ytick.labelsize": 11,
        "legend.fontsize": 11,
        "figure.dpi": 130,         # 110 → 130 (선명도)
        "text.parse_math": False,  # '$' LaTeX 파싱 방지 (가격 표기)
    })
    _SETUP_DONE = True


def save_fig(fig, out_dir: Path, filename: str) -> str | None:
    """fig를 out_dir/filename(.png)으로 저장 → 상대경로 반환. 실패 시 None."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        fig.savefig(str(path), bbox_inches="tight", dpi=110)
        import matplotlib.pyplot as plt
        plt.close(fig)
        return filename
    except Exception:
        log.exception("[chart] save 실패: %s", filename)
        try:
            import matplotlib.pyplot as plt
            plt.close(fig)
        except Exception:
            pass
        return None


# 색상 팔레트
COLOR_UP = "#d62728"      # 상승(빨강 — 한국 관습)
COLOR_DOWN = "#1f77b4"    # 하락(파랑)
COLOR_MA = ["#888888", "#ff7f0e", "#2ca02c", "#9467bd"]  # 10/20/50/200
COLOR_NEUTRAL = "#333333"


def stamp(ax, date_iso: str | None) -> None:
    """차트 우하단에 데이터 기준일 워터마크."""
    if not date_iso:
        return
    try:
        ax.annotate(f"기준 {date_iso}", xy=(1.0, -0.02), xycoords="axes fraction",
                    ha="right", va="top", fontsize=7, color="#999999")
    except Exception:
        pass


def candlestick(ax, df, width: float = 0.6, n: int | None = None) -> None:
    """OHLC 캔들 그리기 (날짜 정수 인덱스 기반). 상승=빨강/하락=파랑.

    n 지정 시 최근 n봉만. x축은 0..len-1 정수 (라벨은 호출측에서 set).
    """
    import numpy as np
    sub = df.iloc[-n:] if n else df
    o = sub["Open"].values if "Open" in sub else sub["Close"].values
    h = sub["High"].values if "High" in sub else sub["Close"].values
    low = sub["Low"].values if "Low" in sub else sub["Close"].values
    c = sub["Close"].values
    x = np.arange(len(sub))
    for i in range(len(sub)):
        up = c[i] >= o[i]
        col = COLOR_UP if up else COLOR_DOWN
        ax.vlines(x[i], low[i], h[i], color=col, linewidth=0.7, alpha=0.9)
        lo_b, hi_b = (o[i], c[i]) if up else (c[i], o[i])
        ax.add_patch(__import__("matplotlib").patches.Rectangle(
            (x[i] - width / 2, lo_b), width, max(hi_b - lo_b, (hi_b + lo_b) * 1e-5 or 1e-6),
            facecolor=col, edgecolor=col, linewidth=0.4, alpha=0.9))
    ax.set_xlim(-1, len(sub))


def date_xticks(ax, index, n: int | None = None, count: int = 6) -> None:
    """정수 x축에 날짜 라벨 count개 분산 배치."""
    import numpy as np
    idx = index[-n:] if n else index
    if len(idx) == 0:
        return
    pos = np.linspace(0, len(idx) - 1, min(count, len(idx))).astype(int)
    ax.set_xticks(pos)
    try:
        labels = [idx[p].strftime("%y/%m") for p in pos]
    except Exception:
        labels = [str(idx[p]) for p in pos]
    ax.set_xticklabels(labels, fontsize=7)
