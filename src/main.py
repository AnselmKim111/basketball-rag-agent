"""CLI entry point.

사용 예시:
    python -m src.main 삼성전자
    python -m src.main "SK하이닉스" --top 5 --no-summarize
    python -m src.main 카카오 --sort popular --headed
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.summarizer import (
    IndividualSummary,
    summarize_combined,
    summarize_pdf,
    write_report,
)
from src.wisereport import WisereportClient


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Playwright 자체 로그는 너무 시끄러우니 줄임
    logging.getLogger("playwright").setLevel(logging.WARNING)


def safe_dirname(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name)
    return name.strip().strip(".") or "company"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="wisereport.co.kr 리포트 자동 다운로드 + Claude 요약"
    )
    p.add_argument("company", help="기업명 (예: 삼성전자)")
    p.add_argument(
        "--top",
        type=int,
        default=int(os.getenv("TOP_N", "10")),
        help="다운로드할 리포트 개수 (기본 10)",
    )
    p.add_argument(
        "--sort",
        choices=["latest", "popular"],
        default=os.getenv("SORT_BY", "latest"),
        help="정렬 기준: latest(최신순) | popular(조회순). 기본 latest",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="브라우저 화면 표시 (디버깅용; 기본은 headless)",
    )
    p.add_argument(
        "--no-summarize",
        action="store_true",
        help="다운로드만 하고 Claude 요약은 건너뛰기",
    )
    p.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Playwright 액션 사이 지연(ms). 디버그 시 500-1000 추천",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 로그")
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    setup_logging(args.verbose)
    log = logging.getLogger("wisereport.main")

    user_id = os.getenv("WISEREPORT_ID")
    password = os.getenv("WISEREPORT_PW")
    if not user_id or not password:
        log.error(
            "WISEREPORT_ID / WISEREPORT_PW 가 설정되지 않았습니다. "
            ".env 파일을 .env.example을 참고해 만드세요."
        )
        return 2

    download_root = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
    summary_root = Path(os.getenv("SUMMARY_DIR", "./summaries"))
    headless = not args.headed and os.getenv("HEADLESS", "true").lower() != "false"

    company_dir = download_root / safe_dirname(args.company)

    # ------------------------------------------------------------------
    # STEP 1: 로그인 + 검색 + PDF 다운로드
    # ------------------------------------------------------------------
    log.info("=" * 60)
    log.info("STEP 1: %s 리포트 다운로드 (상위 %d개, %s)", args.company, args.top, args.sort)
    log.info("=" * 60)

    saved_paths: list[Path] = []
    try:
        with WisereportClient(
            user_id=user_id,
            password=password,
            download_root=download_root,
            headless=headless,
            slow_mo=args.slow_mo,
        ) as client:
            client.login()
            reports = client.search_company(
                args.company, sort_by=args.sort, limit=args.top
            )
            if not reports:
                log.error("검색 결과 없음: %s", args.company)
                return 1
            saved_paths = client.download_reports(reports[: args.top], company_dir)
    except Exception:
        log.exception("다운로드 단계 실패")
        return 1

    if not saved_paths:
        log.error("다운로드된 PDF가 없습니다.")
        return 1

    log.info("다운로드 완료: %d개 (저장 위치: %s)", len(saved_paths), company_dir)

    if args.no_summarize:
        log.info("--no-summarize 옵션이 지정되어 요약 단계를 건너뜁니다.")
        return 0

    # ------------------------------------------------------------------
    # STEP 2: Claude로 개별 요약 + 종합 요약
    # ------------------------------------------------------------------
    log.info("=" * 60)
    log.info("STEP 2: Claude로 요약 (모델: claude-opus-4-7)")
    log.info("=" * 60)

    if not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY가 .env에 없습니다. 요약을 진행할 수 없습니다.")
        return 2

    api_client = anthropic.Anthropic()

    individual: list[IndividualSummary] = []
    for i, pdf_path in enumerate(saved_paths, start=1):
        log.info("[%d/%d] %s", i, len(saved_paths), pdf_path.name)
        try:
            summary = summarize_pdf(api_client, pdf_path)
            individual.append(summary)
        except Exception:
            log.exception("요약 실패: %s", pdf_path.name)
            continue

    if not individual:
        log.error("개별 요약이 모두 실패했습니다.")
        return 1

    log.info("종합 요약 생성 중...")
    try:
        combined = summarize_combined(api_client, args.company, individual)
    except Exception:
        log.exception("종합 요약 실패. 개별 요약만 저장합니다.")
        combined = "(종합 요약 생성에 실패했습니다.)"

    out_path = write_report(
        summary_root / safe_dirname(args.company),
        args.company,
        individual,
        combined,
    )
    log.info("=" * 60)
    log.info("완료. 결과 파일: %s", out_path)
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
