"""CLI entry point.

흐름:
  1) wisereport 자동 로그인 → 기업 검색 → 상위 N개 PDF 다운로드
  2) OpenRouter (기본: anthropic/claude-sonnet-4.5) 로 개별 + 종합 요약
  3) 종합 리포트 텍스트 파일 저장
  4) (선택) 텔레그램으로 PDF + 요약 발송

사용 예시:
  python -m src.main 삼성전자 --ticker 005930
  python -m src.main "SK하이닉스" --ticker 000660 --top 5
  python -m src.main 카카오 --ticker 035720 --no-summarize --no-telegram
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.summarizer import (
    IndividualSummary,
    get_client,
    summarize_combined,
    summarize_pdf,
    write_report,
)
from src.telegram_sender import send_all as telegram_send_all
from src.wisereport import WisereportClient


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def safe_dirname(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name)
    return name.strip().strip(".") or "company"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="wisereport.co.kr 리포트 자동 다운로드 + 요약 + 텔레그램 발송"
    )
    p.add_argument("company", help="기업명 (예: 삼성전자)")
    p.add_argument(
        "--ticker",
        default=None,
        help="ticker code (6자리, 예: 005930). 지정하면 자동 룩업을 건너뜀",
    )
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
        help="정렬 기준 (현재 latest만 지원)",
    )
    p.add_argument("--headed", action="store_true", help="브라우저 화면 표시 (디버깅)")
    p.add_argument("--no-summarize", action="store_true", help="요약 단계 스킵")
    p.add_argument("--no-telegram", action="store_true", help="텔레그램 발송 스킵")
    p.add_argument("--slow-mo", type=int, default=0, help="Playwright 액션 지연(ms)")
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
        log.error("WISEREPORT_ID / WISEREPORT_PW 가 설정되지 않았습니다.")
        return 2

    download_root = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
    summary_root = Path(os.getenv("SUMMARY_DIR", "./summaries"))
    headless = not args.headed and os.getenv("HEADLESS", "true").lower() != "false"
    ignore_https_errors = os.getenv("IGNORE_HTTPS_ERRORS", "false").lower() == "true"
    state_file_str = os.getenv("STORAGE_STATE", "./.wisereport_state.json")
    state_file = Path(state_file_str) if state_file_str else None

    company_dir = download_root / safe_dirname(args.company)

    # ------------------------------------------------------------------
    # STEP 1: PDF 다운로드
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
            ignore_https_errors=ignore_https_errors,
            state_file=state_file,
        ) as client:
            client.ensure_logged_in()
            ticker = args.ticker
            if not ticker:
                ticker = client.lookup_ticker(args.company)
                if not ticker:
                    log.error(
                        "ticker 자동 룩업 실패. --ticker로 직접 지정해주세요."
                    )
                    return 1
            log.info("사용할 ticker: %s", ticker)

            items = client.list_reports(ticker=ticker, sort_by=args.sort, limit=args.top)
            if not items:
                log.error("리포트가 없습니다: %s (ticker=%s)", args.company, ticker)
                return 1
            saved_paths = client.download_reports(items, company_dir)
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
    # STEP 2: OpenRouter로 요약
    # ------------------------------------------------------------------
    log.info("=" * 60)
    log.info("STEP 2: OpenRouter로 요약")
    log.info("=" * 60)

    if not os.getenv("OPENROUTER_API_KEY"):
        log.error("OPENROUTER_API_KEY가 .env에 없습니다.")
        return 2

    api_client = get_client()

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
    log.info("종합 리포트 파일: %s", out_path)

    # ------------------------------------------------------------------
    # STEP 3: 텔레그램 발송 (선택)
    # ------------------------------------------------------------------
    if args.no_telegram or os.getenv("DISABLE_TELEGRAM", "false").lower() == "true":
        log.info("텔레그램 발송 스킵")
        return 0

    log.info("=" * 60)
    log.info("STEP 3: 텔레그램 발송")
    log.info("=" * 60)
    sent = telegram_send_all(
        pdf_paths=saved_paths,
        summary_text_path=out_path,
        company_name=args.company,
        summary_combined_text=combined,
    )
    if not sent:
        log.warning(
            "텔레그램 발송 실패 또는 미설정. "
            ".env의 TELEGRAM_BOT_TOKEN, TELEGRAM_USERNAME 또는 TELEGRAM_CHAT_ID 확인."
        )
    log.info("=" * 60)
    log.info("완료. 결과 파일: %s", out_path)
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
