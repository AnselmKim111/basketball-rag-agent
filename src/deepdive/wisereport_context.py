"""deepdive 업의 본질 분석에 wisereport 리포트를 컨텍스트로 추가.

종목 ticker로 wisereport에서:
  - 기업 리포트 최신 3개 (list_type=33)
  - 산업 리포트 최신 1개 (list_type=32, 같은 cocode 필터)
다운받아 PDF 텍스트 추출. 실패 시 빈 결과 반환 (예외 던지지 않음).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class WisereportContext:
    company_texts: list[str]   # 기업 리포트 텍스트 (최대 3개)
    company_titles: list[str]
    industry_texts: list[str]  # 산업 리포트 텍스트 (최대 1개)
    industry_titles: list[str]


def collect_for_ticker(ticker: str, target_dir: Path, max_chars_per_report: int = 15_000) -> WisereportContext:
    """주어진 ticker의 wisereport 컨텍스트 수집. 실패 시 부분/빈 결과 반환."""
    empty = WisereportContext(company_texts=[], company_titles=[], industry_texts=[], industry_titles=[])

    try:
        from src.wisereport import WisereportClient
        from src import summarizer
    except Exception:
        log.exception("wisereport_context: 모듈 import 실패")
        return empty

    user_id = os.environ.get("WISEREPORT_ID")
    password = os.environ.get("WISEREPORT_PW")
    if not (user_id and password):
        log.warning("WISEREPORT 자격증명 미설정 — wisereport 컨텍스트 스킵")
        return empty

    target_dir.mkdir(parents=True, exist_ok=True)
    company_dir = target_dir / "company"
    industry_dir = target_dir / "industry"

    company_paths: list[Path] = []
    industry_paths: list[Path] = []
    company_titles: list[str] = []
    industry_titles: list[str] = []

    try:
        with WisereportClient(
            user_id=user_id,
            password=password,
            download_root=target_dir,
            headless=True,
            ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
            state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
        ) as cli:
            cli.ensure_logged_in()

            # 기업 리포트 3개
            try:
                company_items = cli.list_reports(ticker=ticker, sort_by="latest", limit=3, list_type="33")
                if company_items:
                    company_titles = [it.title for it in company_items]
                    company_paths = cli.download_reports(company_items, company_dir)
                    log.info("wisereport 기업 리포트 %d개 다운로드", len(company_paths))
            except Exception:
                log.exception("wisereport 기업 리포트 단계 실패")

            # 산업 리포트 1개 (같은 cocode 필터)
            try:
                industry_items = cli.list_reports(ticker=ticker, sort_by="latest", limit=1, list_type="32")
                if industry_items:
                    industry_titles = [it.title for it in industry_items]
                    industry_paths = cli.download_reports(industry_items, industry_dir)
                    log.info("wisereport 산업 리포트 %d개 다운로드", len(industry_paths))
            except Exception:
                log.exception("wisereport 산업 리포트 단계 실패")
    except Exception:
        log.exception("wisereport client 자체 실패 — 빈 컨텍스트로 진행")
        return empty

    # PDF 텍스트 추출
    company_texts: list[str] = []
    for p in company_paths:
        try:
            t = summarizer._extract_pdf_text(p, max_chars=max_chars_per_report)
            if t.strip():
                company_texts.append(t)
        except Exception:
            log.exception("wisereport 기업 PDF 텍스트 추출 실패: %s", p)

    industry_texts: list[str] = []
    for p in industry_paths:
        try:
            t = summarizer._extract_pdf_text(p, max_chars=max_chars_per_report)
            if t.strip():
                industry_texts.append(t)
        except Exception:
            log.exception("wisereport 산업 PDF 텍스트 추출 실패: %s", p)

    log.info(
        "wisereport 컨텍스트 수집 완료: 기업텍스트 %d, 산업텍스트 %d",
        len(company_texts), len(industry_texts),
    )
    return WisereportContext(
        company_texts=company_texts,
        company_titles=company_titles,
        industry_texts=industry_texts,
        industry_titles=industry_titles,
    )
