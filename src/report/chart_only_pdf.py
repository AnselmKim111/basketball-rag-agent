"""Chart-only PDF — narrative 없이 차트 이미지만 PDF로 묶음.

각 차트 1장 = PDF 1페이지 (모바일 가독성). 표지 1장 (제목 + 날짜 + 데이터 신선도 라벨).
matplotlib PdfPages — 외부 의존 0 (Playwright·markdown 미사용).
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def build_pdf(chart_paths: list[tuple[str, str]], img_dir: Path, out_pdf: Path,
              headline: str, freshness_notes: list[str] | None = None) -> bool:
    """chart_paths: [(filename, caption)] — img_dir 안의 파일.

    각 차트가 PDF 1페이지 가운데 정렬. 첫 페이지는 표지 (제목 + 날짜 + 신선도).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from PIL import Image

    if not chart_paths:
        log.warning("[chart_only_pdf] chart_paths 비어있음 — PDF 생성 skip")
        return False

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    try:
        with PdfPages(str(out_pdf)) as pdf:
            # 1. 표지 페이지
            _render_cover(pdf, headline, freshness_notes or [])

            # 2. 각 차트 1페이지
            n_ok = 0
            for filename, caption in chart_paths:
                p = img_dir / filename
                if not p.exists():
                    log.warning("[chart_only_pdf] 이미지 없음: %s", filename)
                    continue
                try:
                    _render_image_page(pdf, p, caption)
                    n_ok += 1
                except Exception:
                    log.exception("[chart_only_pdf] 페이지 렌더 실패: %s", filename)

        if n_ok == 0:
            log.error("[chart_only_pdf] 렌더된 페이지 0 — PDF 빈 채로 생성됨")
            return False
        size_mb = out_pdf.stat().st_size / 1_048_576
        log.info("[chart_only_pdf] 생성 완료: %s (%.2f MB, %d 차트)", out_pdf, size_mb, n_ok)
        return True
    except Exception:
        log.exception("[chart_only_pdf] PDF 생성 실패")
        return False


def _render_cover(pdf, headline: str, freshness_notes: list[str]) -> None:
    """표지 페이지 — 제목 + 날짜 + 데이터 신선도."""
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(12, 14))
    fig.patch.set_facecolor("white")
    ax = fig.add_subplot(111)
    ax.axis("off")

    now = datetime.now()
    ax.text(0.5, 0.78, headline, ha="center", va="center",
            fontsize=26, fontweight="bold", color="#0f172a", transform=ax.transAxes)
    ax.text(0.5, 0.70, now.strftime("%Y-%m-%d  %H:%M KST"),
            ha="center", va="center", fontsize=14, color="#475569",
            transform=ax.transAxes)
    ax.text(0.5, 0.62, "차트 전용 · 모바일 최적",
            ha="center", va="center", fontsize=12, color="#94a3b8",
            transform=ax.transAxes)

    # 데이터 신선도 (있을 때만)
    if freshness_notes:
        ax.text(0.5, 0.52, "📡 데이터 신선도",
                ha="center", va="center", fontsize=14, fontweight="bold",
                color="#1e293b", transform=ax.transAxes)
        y = 0.48
        for note in freshness_notes[:8]:
            ax.text(0.5, y, note, ha="center", va="center",
                    fontsize=11, color="#334155", transform=ax.transAxes)
            y -= 0.035

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_image_page(pdf, image_path: Path, caption: str) -> None:
    """이미지 1장 = PDF 1페이지. 이미지 비율 유지 + 가운데 정렬 + 하단 캡션."""
    import matplotlib.pyplot as plt
    from PIL import Image

    with Image.open(image_path) as im:
        w, h = im.size
    aspect = h / w
    # 페이지: A4 portrait 비율 (가로 ~12 inch, 세로 가변)
    fig_w = 12.0
    fig_h = min(16.0, fig_w * aspect + 1.0)  # 캡션 공간 +1
    fig = plt.figure(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor("white")

    # 이미지 axes (90% 영역)
    ax_img = fig.add_axes([0.02, 0.06, 0.96, 0.91])
    ax_img.axis("off")
    with Image.open(image_path) as im:
        ax_img.imshow(im)

    # 캡션 (하단)
    if caption:
        fig.text(0.5, 0.025, caption, ha="center", va="bottom",
                 fontsize=10, color="#64748b", style="italic")

    pdf.savefig(fig, bbox_inches=None)
    plt.close(fig)
