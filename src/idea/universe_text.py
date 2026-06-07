"""Universe-wide 사업 description fetcher — DART 사업의 내용 bulk extraction.

IdeaBot universe_scan을 위해 screener universe(~2000) 전체 종목의 사업 description을
DART 사업보고서 "사업의 내용" 섹션에서 1회 추출해 screener.db에 캐시.

기존 자산 재사용:
  - src.deepdive.dart_client.get_corp_code(ticker) — ticker → corp_code
  - src.deepdive.dart_client.fetch_latest_business_report(corp_code) — 최신 보고서
  - src.deepdive.dart_client.download_report_archive(rcept_no, dir) — ZIP 추출
  - src.deepdive.dart_client.extract_doc_text(path, prefer_section="사업의 내용")

쿼터·rate-limit:
  - DART는 일 10000건 한도. 2000 종목 × 1 fetch < 한도 안전.
  - 동시 4 concurrent로 안전 마진 (기존 disclosure_watcher는 1-5).

graceful:
  - corp_code 없거나 사업보고서 없는 종목(SPAC·신규상장) → skip + fail count
  - 추출 텍스트 빈 문자열이면 skip
  - 90일 내 추출된 종목은 default skip (분기별 갱신 사이클 가정)
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.screener import db as screener_db

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 사업의 내용 텍스트는 보통 5-20K 길이. 임베딩 입력에는 4000자면 충분.
MAX_BUSINESS_TEXT_CHARS = 12_000  # DB 저장은 넉넉히, 임베딩은 호출자가 truncate


def _today_kst_date() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _cleanup_business_text(text: str) -> str:
    """1차 cleanup — DART XML/PDF 추출물의 노이즈 정리.

    - 연속 공백/줄바꿈 정리
    - [Page N] 마커 제거 (extract_doc_text PDF 폴백이 추가)
    - 페이지 번호 같은 짧은 라인 제거
    """
    if not text:
        return ""
    text = re.sub(r"\[Page \d+\]", "", text)
    text = re.sub(r"\n\s*\d+\s*\n", "\n", text)  # 페이지 번호만 있는 라인
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text[:MAX_BUSINESS_TEXT_CHARS]


def _recent_enough(extracted_dt: Optional[str], skip_if_recent_days: int) -> bool:
    if not extracted_dt:
        return False
    try:
        dt = datetime.strptime(extracted_dt, "%Y-%m-%d").replace(tzinfo=KST)
    except Exception:
        return False
    return (datetime.now(KST) - dt) < timedelta(days=skip_if_recent_days)


async def _fetch_single(ticker: str, name: str, skip_if_recent_days: int) -> tuple[str, str]:
    """단일 ticker → "fetched"/"skipped_recent"/"failed_*" 상태 반환.

    동기 DART 호출을 thread pool에서 실행 (httpx sync client 사용).
    """
    from src.deepdive import dart_client

    # 0) 90일 내 갱신 skip
    row = screener_db.get_business_text_row(ticker)
    if row and _recent_enough(row.get("business_text_dt"), skip_if_recent_days):
        return ticker, "skipped_recent"

    # 1) corp_code lookup
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, dart_client.get_corp_code, ticker)
    except Exception:
        log.exception("[universe_text] corp_code lookup 예외 ticker=%s", ticker)
        return ticker, "failed_corp_code"
    if not result:
        return ticker, "failed_no_corp_code"
    corp_code, _corp_name = result

    # 2) 사업보고서 메타
    try:
        report = await loop.run_in_executor(None, dart_client.fetch_latest_business_report, corp_code)
    except Exception:
        log.exception("[universe_text] business report fetch 예외 ticker=%s", ticker)
        return ticker, "failed_report_meta"
    if not report:
        return ticker, "failed_no_report"

    # 3) archive 다운로드 + 사업의 내용 추출
    with tempfile.TemporaryDirectory(prefix=f"universe_text_{ticker}_") as tmp:
        tmp_dir = Path(tmp)
        try:
            archive_path = await loop.run_in_executor(
                None, dart_client.download_report_archive, report.rcept_no, tmp_dir, False,
            )
        except Exception:
            log.exception("[universe_text] archive 다운로드 예외 ticker=%s", ticker)
            return ticker, "failed_download"
        if not archive_path:
            return ticker, "failed_archive"

        try:
            raw_text = await loop.run_in_executor(
                None, dart_client.extract_doc_text, archive_path, 80_000, "사업의 내용",
            )
        except Exception:
            log.exception("[universe_text] 텍스트 추출 예외 ticker=%s", ticker)
            return ticker, "failed_extract"

    cleaned = _cleanup_business_text(raw_text or "")
    if len(cleaned) < 200:
        # 200자 미만은 사업 description으로 못 씀 (스팩·신규상장·청산예정)
        return ticker, "failed_short_text"

    # 4) DB upsert (text 갱신 시 기존 embedding은 자동 무효화 — upsert_business_text가 NULL 세팅)
    try:
        screener_db.upsert_business_text(
            ticker=ticker,
            text=cleaned,
            extracted_dt=_today_kst_date(),
            report_no=report.rcept_no,
        )
    except Exception:
        log.exception("[universe_text] DB upsert 예외 ticker=%s", ticker)
        return ticker, "failed_db"
    return ticker, "fetched"


async def bulk_refresh_business_text(
    tickers: Optional[list[str]] = None,
    max_concurrency: int = 4,
    skip_if_recent_days: int = 90,
    progress_every: int = 50,
) -> dict[str, int]:
    """universe 전체(또는 지정 ticker)의 DART 사업의 내용 bulk fetch + cache.

    Args:
        tickers: 명시 ticker 리스트. None이면 screener.db의 active universe 전체.
        max_concurrency: 동시 DART 호출 수 (rate-limit 안전 마진).
        skip_if_recent_days: business_text_dt가 이 일수 내면 스킵 (분기별 갱신 가정).
        progress_every: 매 N건 처리 후 로그.

    Returns:
        {fetched, skipped_recent, failed_no_corp_code, failed_no_report,
         failed_short_text, failed_*, total} 통계.
    """
    # universe 로드
    if tickers is None:
        rows = screener_db.get_active_tickers()
        targets = [(r["ticker"], r.get("name") or "") for r in rows]
    else:
        targets = [(t, screener_db.get_ticker_name(t) or "") for t in tickers]
    if not targets:
        log.warning("[universe_text] 대상 ticker 없음 — universe.refresh가 먼저 필요?")
        return {"total": 0}

    log.info("[universe_text] bulk fetch 시작 — %d종목, 동시=%d, recent_skip=%d일",
             len(targets), max_concurrency, skip_if_recent_days)

    sem = asyncio.Semaphore(max_concurrency)
    stats: dict[str, int] = {}
    completed = 0

    async def _one(t: str, n: str) -> None:
        nonlocal completed
        async with sem:
            _, status = await _fetch_single(t, n, skip_if_recent_days)
        stats[status] = stats.get(status, 0) + 1
        # 진단: status별 첫 3건의 ticker·name 로그 (디버깅용 sample)
        if stats[status] <= 3:
            log.info("[universe_text] SAMPLE status=%s ticker=%s name=%s", status, t, n)
        completed += 1
        if completed % progress_every == 0:
            log.info("[universe_text] 진행: %d/%d (fetched=%d, skipped=%d, failed=%d)",
                     completed, len(targets),
                     stats.get("fetched", 0), stats.get("skipped_recent", 0),
                     sum(v for k, v in stats.items() if k.startswith("failed")))

    await asyncio.gather(*[_one(t, n) for t, n in targets])
    stats["total"] = len(targets)
    log.info("[universe_text] bulk fetch 완료 — %s", stats)
    return stats
