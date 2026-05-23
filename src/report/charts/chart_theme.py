"""차트 공통 테마 — 한글 폰트 + 흰 배경 + 16:9 기본 스타일.

모든 report 차트는 setup() 1회 호출 후 그림. matplotlib Agg(headless).
캡션은 차트 하단 또는 리포트 본문에서 관찰/해석/체크 3줄로 별도 작성.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SETUP_DONE = False


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
        "font.size": 11,
        "figure.dpi": 110,
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
