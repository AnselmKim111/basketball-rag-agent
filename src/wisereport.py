"""wisereport.co.kr 자동 로그인 + 기업 리포트 PDF 다운로드.

사이트 DOM이 변경되면 SELECTORS 딕셔너리만 수정하면 됩니다.
첫 실행 시 HEADLESS=false로 두고 동작을 눈으로 확인한 뒤 셀렉터를 보정하세요.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

log = logging.getLogger(__name__)

BASE_URL = "https://www.wisereport.co.kr"
LOGIN_URL = f"{BASE_URL}/login/login.aspx"


SELECTORS = {
    "id_input": [
        'input[name="userid"]',
        'input[name="userId"]',
        'input[name="id"]',
        'input[id="userid"]',
        'input[id="userId"]',
        '#txtUserId',
    ],
    "pw_input": [
        'input[name="passwd"]',
        'input[name="password"]',
        'input[name="pw"]',
        'input[type="password"]',
        '#txtPasswd',
    ],
    "login_button": [
        'button:has-text("로그인")',
        'input[type="submit"][value*="로그인"]',
        'a:has-text("로그인")',
        '#btnLogin',
    ],
    "search_input": [
        'input[name="search_word"]',
        'input[name="searchWord"]',
        'input[name="keyword"]',
        'input[placeholder*="검색"]',
        'input[placeholder*="종목"]',
        'input[placeholder*="기업"]',
        '#txtSearch',
    ],
    "search_submit": [
        'button:has-text("검색")',
        'input[type="submit"][value*="검색"]',
        'button[type="submit"]',
    ],
    "report_row": [
        "table tbody tr",
        ".report_list tr",
        "ul.report_list li",
    ],
    "pdf_link": [
        'a[href*=".pdf"]',
        'a[href*="pdf"]',
        'a:has-text("PDF")',
        'a[onclick*="pdf"]',
        'img[alt*="pdf" i]',
        'img[src*="pdf"]',
    ],
    "sort_latest": [
        'a:has-text("최신순")',
        'button:has-text("최신순")',
    ],
    "sort_popular": [
        'a:has-text("조회순")',
        'a:has-text("인기순")',
    ],
}


@dataclass
class ReportItem:
    """리포트 리스트의 한 행을 표현."""

    title: str
    broker: str  # 발행 증권사/기관
    date: str
    pdf_url: str | None
    row_index: int  # 페이지 내 행 인덱스 (PDF 다운로드 트리거용)


class WisereportClient:
    """wisereport.co.kr 자동화 클라이언트.

    with 문으로 사용하세요:
        with WisereportClient(user_id, password) as client:
            client.login()
            reports = client.search_company("삼성전자")
            paths = client.download_reports(reports[:10], Path("./downloads/삼성전자"))
    """

    def __init__(
        self,
        user_id: str,
        password: str,
        download_root: Path,
        headless: bool = True,
        slow_mo: int = 0,
    ) -> None:
        self.user_id = user_id
        self.password = password
        self.download_root = download_root
        self.headless = headless
        self.slow_mo = slow_mo
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> "WisereportClient":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        self._context = self._browser.new_context(
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(30_000)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("WisereportClient must be used as a context manager.")
        return self._page

    def _try_selectors(self, selectors: list[str], timeout: int = 5_000):
        """셀렉터 후보 목록 중 첫 번째로 찾아지는 요소를 반환."""
        last_err: Exception | None = None
        for sel in selectors:
            try:
                el = self.page.wait_for_selector(sel, timeout=timeout, state="visible")
                if el:
                    return el
            except PlaywrightTimeoutError as e:
                last_err = e
                continue
        raise PlaywrightTimeoutError(
            f"None of selectors found within {timeout}ms: {selectors}"
        ) from last_err

    # ------------------------------------------------------------------
    # 로그인
    # ------------------------------------------------------------------
    def login(self) -> None:
        log.info("로그인 페이지로 이동: %s", LOGIN_URL)
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")

        id_el = self._try_selectors(SELECTORS["id_input"])
        id_el.fill(self.user_id)

        pw_el = self._try_selectors(SELECTORS["pw_input"])
        pw_el.fill(self.password)

        btn = self._try_selectors(SELECTORS["login_button"])
        btn.click()

        # 로그인 완료 대기 - URL이 login에서 벗어나거나 메인으로 이동
        try:
            self.page.wait_for_url(
                lambda url: "login" not in url.lower(),
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            # URL이 그대로면 에러 메시지 확인
            body_text = self.page.inner_text("body")[:500]
            raise RuntimeError(
                f"로그인 실패. 응답 내용 일부: {body_text!r}"
            )
        log.info("로그인 성공")

    # ------------------------------------------------------------------
    # 기업 검색 + 리포트 리스트 수집
    # ------------------------------------------------------------------
    def search_company(
        self,
        company_name: str,
        sort_by: Literal["latest", "popular"] = "latest",
        limit: int = 10,
    ) -> list[ReportItem]:
        """기업명으로 검색 후 리포트 리스트 상위 N개 반환."""
        log.info("기업 검색: %s (정렬: %s, 상위 %d개)", company_name, sort_by, limit)

        # 1) 메인으로 이동 후 통합검색 사용
        self.page.goto(BASE_URL, wait_until="domcontentloaded")

        search_el = self._try_selectors(SELECTORS["search_input"])
        search_el.fill(company_name)
        search_el.press("Enter")

        # 2) 검색 결과 페이지 - 리포트 탭으로 이동
        # wisereport는 보통 종목별 페이지(stock_jisu/jisu_main 등)에서
        # 리포트 섹션을 제공. 여기서는 통합 검색 결과 페이지의 리포트 목록을 가정.
        self.page.wait_for_load_state("networkidle", timeout=15_000)

        # 3) 정렬 옵션 적용
        sort_selectors = (
            SELECTORS["sort_latest"] if sort_by == "latest" else SELECTORS["sort_popular"]
        )
        try:
            sort_el = self._try_selectors(sort_selectors, timeout=3_000)
            sort_el.click()
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            log.warning(
                "정렬 옵션 셀렉터를 찾지 못했습니다. 기본 정렬로 진행합니다."
            )

        # 4) 리포트 행 수집
        reports = self._collect_report_rows(limit)
        log.info("리포트 %d개 수집됨", len(reports))
        return reports

    def _collect_report_rows(self, limit: int) -> list[ReportItem]:
        """현재 페이지에서 리포트 리스트 행을 파싱."""
        rows = []
        for sel in SELECTORS["report_row"]:
            rows = self.page.query_selector_all(sel)
            if rows:
                break
        if not rows:
            raise RuntimeError(
                "리포트 행을 찾을 수 없습니다. SELECTORS['report_row']를 사이트 구조에 맞게 수정하세요."
            )

        items: list[ReportItem] = []
        for idx, row in enumerate(rows):
            if len(items) >= limit:
                break
            text = (row.inner_text() or "").strip()
            if not text:
                continue

            # 제목, 날짜, 발행사 추출 - 컬럼 텍스트로 휴리스틱 분리
            cells = [
                (c.inner_text() or "").strip()
                for c in row.query_selector_all("td, span, div")
            ]
            cells = [c for c in cells if c]

            title = cells[0] if cells else text
            broker = ""
            date = ""

            # 날짜 패턴 (YYYY-MM-DD or YYYY.MM.DD) 추출
            date_pattern = re.compile(r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})")
            for c in cells:
                m = date_pattern.search(c)
                if m:
                    date = m.group(1)
                    break

            # 발행사: 보통 두 번째 또는 세 번째 컬럼
            for c in cells[1:]:
                if not date_pattern.search(c) and 2 <= len(c) <= 30:
                    broker = c
                    break

            # PDF 링크 추출
            pdf_url = None
            for sel in SELECTORS["pdf_link"]:
                link_el = row.query_selector(sel)
                if link_el:
                    href = link_el.get_attribute("href")
                    onclick = link_el.get_attribute("onclick") or ""
                    if href and ".pdf" in href.lower():
                        pdf_url = urljoin(BASE_URL, href)
                        break
                    if onclick:
                        # onclick="downloadPdf('xxx.pdf')" 같은 패턴 추출
                        m = re.search(r"['\"]([^'\"]+\.pdf[^'\"]*)['\"]", onclick)
                        if m:
                            pdf_url = urljoin(BASE_URL, m.group(1))
                            break

            items.append(
                ReportItem(
                    title=title[:200],
                    broker=broker,
                    date=date,
                    pdf_url=pdf_url,
                    row_index=idx,
                )
            )
        return items

    # ------------------------------------------------------------------
    # PDF 다운로드
    # ------------------------------------------------------------------
    def download_reports(
        self,
        reports: list[ReportItem],
        target_dir: Path,
    ) -> list[Path]:
        """리포트 리스트의 PDF를 target_dir에 저장."""
        target_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []

        for i, report in enumerate(reports, start=1):
            log.info("[%d/%d] 다운로드: %s (%s)", i, len(reports), report.title, report.date)

            try:
                if report.pdf_url:
                    path = self._download_via_url(report, i, target_dir)
                else:
                    path = self._download_via_click(report, i, target_dir)
                saved.append(path)
            except Exception:
                log.exception("다운로드 실패: %s", report.title)
                continue

            # 매너 있게 약간 쉬기
            time.sleep(0.5)

        return saved

    def _download_via_url(self, report: ReportItem, idx: int, target_dir: Path) -> Path:
        """직접 PDF URL로 다운로드."""
        with self.page.expect_download(timeout=60_000) as dl_info:
            self.page.goto(report.pdf_url)  # type: ignore[arg-type]
        download: Download = dl_info.value
        return self._save_download(download, report, idx, target_dir)

    def _download_via_click(self, report: ReportItem, idx: int, target_dir: Path) -> Path:
        """행을 클릭해서 다운로드 트리거."""
        # 리스트 페이지로 돌아갔다고 가정. 안 그러면 재검색 필요.
        rows = []
        for sel in SELECTORS["report_row"]:
            rows = self.page.query_selector_all(sel)
            if rows:
                break
        if report.row_index >= len(rows):
            raise RuntimeError(f"row_index {report.row_index} out of range")

        row = rows[report.row_index]
        link_el = None
        for sel in SELECTORS["pdf_link"]:
            link_el = row.query_selector(sel)
            if link_el:
                break
        if link_el is None:
            raise RuntimeError("PDF 링크를 찾을 수 없습니다.")

        with self.page.expect_download(timeout=60_000) as dl_info:
            link_el.click()
        download: Download = dl_info.value
        return self._save_download(download, report, idx, target_dir)

    @staticmethod
    def _safe_filename(name: str) -> str:
        # 윈도우/리눅스 양쪽에서 안전한 파일명으로 변환
        name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name)
        name = name.strip().strip(".")
        return name[:120] or "report"

    def _save_download(
        self, download: Download, report: ReportItem, idx: int, target_dir: Path
    ) -> Path:
        suggested = download.suggested_filename or f"{idx:02d}.pdf"
        safe_title = self._safe_filename(report.title)
        date_prefix = report.date.replace(".", "-").replace("/", "-") if report.date else "0000-00-00"
        filename = f"{idx:02d}_{date_prefix}_{safe_title}.pdf"
        path = target_dir / filename
        download.save_as(path)
        log.info("저장: %s", path)
        return path
