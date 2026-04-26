"""wisereport.co.kr 자동 로그인 + 기업 리포트 PDF 다운로드.

검증된 흐름 (2026-04-25 기준):
  1) 로그인:  메인 상단바 #UsrID + #UsrPassWD → Enter
              중복 로그인 알림이 뜨면 #popup_ok 클릭
  2) 기업명 → ticker:  팝업 룩업 (parent.opener 의존성으로 직접 호출 어려움)
              → 메인 페이지 통합검색 폼을 사용해 팝업을 띄우고 첫 결과 클릭
              → 또는 사용자가 ticker code를 직접 제공
  3) 리포트 목록:  POST /wiseReport/reports/contentList.aspx
              - hiddencd=ticker, hiddengubun=cocode, ListType=33 등
              - 응답 HTML에 gotoSearchContent(rpt_id, sch_db, sch_lang, sch_dt, sch_gubun, sch_cont) 호출 포함
  4) PDF 다운로드:
              4-1) GET /comm/LoadReportBody.aspx?rpt_id=...&sch_db=...&sch_lang=...&sch_dt=...&sch_cont=...
                   응답 HTML에서 openContent('rpt_id','brk_cd','fpath','usr_id','') 호출 추출
              4-2) GET /comm/LoadReport.aspx?rpt_id=...&brk_cd=...&fpath=...&view_lang=K
                   → Content-Disposition으로 PDF 직접 다운로드
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

log = logging.getLogger(__name__)

BASE_URL = "https://www.wisereport.co.kr"
REPORT_LIST_URL = f"{BASE_URL}/wiseReport/reports/ReportList.aspx"
TOPHITS_URL = f"{BASE_URL}/wiseReport/reports/TopHits.aspx"
CONTENT_LIST_AJAX = f"{BASE_URL}/wiseReport/reports/contentList.aspx"
LOOKUP_URL_TPL = f"{BASE_URL}/comm/New_commdl_LookUp.aspx?type=elmCmp&searchValue={{q}}"

# TopHits AJAX 엔드포인트 (카테고리별)
# (path, rptTyp, rptSubTyp, default_min_visit)
TOPHITS_ENDPOINTS = {
    "company":  ("ajax/topHits_company.aspx",  "1", "0",  "100"),
    "industry": ("ajax/topHits_Industry.aspx", "2", "0",  "100"),
    "strategy": ("ajax/topHits_Strategy.aspx", "6", "0",  "10"),
    "global":   ("ajax/topHits_Global.aspx",   "3", "29", "10"),
}

# wisereport.co.kr는 UA의 Win64;x64나 AppleWebKit 헤더가 들어가면 일부 JS
# 코드 경로가 달라져 로그인 form 제출이 무력화된다. 짧은 UA를 그대로 유지하자.
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0) Chrome/131.0.0.0 Safari/537.36"


@dataclass
class ReportItem:
    rpt_id: str
    sch_db: str  # 'Y' or 'N'
    sch_lang: str  # 'K' or 'E'
    sch_dt: str  # YYYYMMDDHHMMSS
    sch_gubun: str
    sch_cont: str  # ticker
    title: str

    @property
    def date_short(self) -> str:
        # sch_dt is YYYYMMDD or YYYYMMDDHHMMSS — take the first 8
        return f"{self.sch_dt[:4]}-{self.sch_dt[4:6]}-{self.sch_dt[6:8]}" if len(self.sch_dt) >= 8 else self.sch_dt


@dataclass
class PdfTarget:
    rpt_id: str
    brk_cd: str
    fpath: str  # PDF filename (e.g. 1F00320260421_005930.pdf)


class WisereportClient:
    """wisereport.co.kr 자동화 클라이언트."""

    def __init__(
        self,
        user_id: str,
        password: str,
        download_root: Path,
        headless: bool = True,
        slow_mo: int = 0,
        ignore_https_errors: bool = False,
        state_file: Path | None = None,
    ) -> None:
        self.user_id = user_id
        self.password = password
        self.download_root = download_root
        self.headless = headless
        self.slow_mo = slow_mo
        self.ignore_https_errors = ignore_https_errors
        self.state_file = state_file
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
        ctx_kwargs = dict(
            accept_downloads=True,
            user_agent=DEFAULT_USER_AGENT,
            ignore_https_errors=self.ignore_https_errors,
        )
        if self.state_file and self.state_file.exists():
            ctx_kwargs["storage_state"] = str(self.state_file)
        self._context = self._browser.new_context(**ctx_kwargs)
        self._page = self._context.new_page()
        self._page.set_default_timeout(30_000)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context and self.state_file:
                try:
                    self._context.storage_state(path=str(self.state_file))
                except Exception:
                    pass
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

    # ------------------------------------------------------------------
    # 로그인
    # ------------------------------------------------------------------
    def ensure_logged_in(self) -> None:
        """저장된 세션이 있으면 그대로 사용, 만료됐으면 새로 로그인.

        주의: 사전 체크에서 ReportList를 한번 두드리는 동작은 wisereport의
        세션 쿠키 상태를 흐트러뜨려 이후 신규 로그인이 무력화됨. 따라서 신규
        로그인은 항상 깨끗한 세션에서 시작.
        """
        if self.state_file and self.state_file.exists():
            self.page.goto(REPORT_LIST_URL, wait_until="networkidle")
            if "ReportList" in self.page.url and "returnUrl" not in self.page.url:
                log.info("기존 세션 재사용")
                self.page.wait_for_timeout(1500)
                return
            log.info("저장된 세션이 만료되었습니다. 새 컨텍스트에서 재로그인.")
            # 컨텍스트 쿠키 비움
            try:
                self._context.clear_cookies()  # type: ignore[union-attr]
            except Exception:
                pass
        self.login()

    def login(self) -> None:
        log.info("로그인 시도")
        self.page.goto(BASE_URL + "/", wait_until="networkidle")
        self.page.wait_for_selector("#UsrID", state="attached", timeout=15_000)
        # JS handler 바인딩 완료 대기
        self.page.wait_for_function(
            "typeof CheckVal === 'function' && document.forms['frmuser']",
            timeout=10_000,
        )

        # 사이트의 placeholder swap 트릭(UsrPassWD가 _Text input 뒤에 숨겨짐)
        # 때문에 page.fill의 force=True도 비밀번호를 엉뚱한 input(UsrID)에
        # 붙여버린다. JS로 직접 값을 설정한 뒤 form 제출.
        self.page.evaluate(
            """({uid, upw}) => {
                $('#UsrID').val(uid);
                $('#UsrPassWD').val(upw);
                $('#login_flag').val('top');
            }""",
            {"uid": self.user_id, "upw": self.password},
        )
        # CheckVal() 안에서 frmuser.submit()을 실행 → LoginProcess.aspx로 이동
        try:
            with self.page.expect_navigation(timeout=15_000, wait_until="domcontentloaded"):
                self.page.evaluate("CheckVal()")
        except PlaywrightTimeoutError:
            log.warning("로그인 form 제출 후 navigation 대기 타임아웃")

        # 로그인 후 networkidle 대기. URL 매칭으로는 returnUrl이 포함된 redirect
        # ('/?returnUrl=...ReportList.aspx')와 진짜 LoginProcess.aspx를 구분 못함.
        try:
            self.page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass
        log.debug("login Enter 후 URL=%s", self.page.url)

        # 중복 로그인 팝업 처리 (popup_ok이 있으면 클릭)
        if self.page.query_selector("#popup_ok"):
            log.info("중복 로그인 감지 → 강제 로그인")
            self.page.click("#popup_ok")
            try:
                self.page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeoutError:
                pass
            log.debug("popup_ok 후 URL=%s", self.page.url)

        # 추가 정착 시간 (서버 세션 쿠키 set 시간)
        self.page.wait_for_timeout(1500)

        # 검증 - ReportList 직접 접근
        self.page.goto(REPORT_LIST_URL, wait_until="networkidle")
        if "ReportList" not in self.page.url or "returnUrl" in self.page.url:
            # 마지막 디버그 정보
            body_snip = ""
            try:
                body_snip = self.page.inner_text("body")[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"로그인 실패. 마지막 URL: {self.page.url}\n"
                f"body 일부: {body_snip!r}"
            )
        self.page.wait_for_timeout(1500)
        log.info("로그인 성공")

    # ------------------------------------------------------------------
    # 기업명 → ticker code
    # ------------------------------------------------------------------
    def lookup_ticker(self, company_name: str) -> str | None:
        """기업명으로 ticker code 검색.

        wisereport의 메인 검색 → 팝업 룩업 흐름을 사용.
        팝업 페이지(parent.opener 의존성)는 직접 navigate가 불가하므로
        메인 페이지에서 na_search_lookup을 호출해 팝업을 띄우고
        팝업 내에서 첫 결과의 종목코드를 추출.
        """
        log.info("ticker 룩업: %s", company_name)
        self.page.goto(REPORT_LIST_URL, wait_until="networkidle")
        self.page.wait_for_timeout(1500)

        # 검색 텍스트와 cocode 모드 설정
        self.page.evaluate(
            """(name) => {
                $('#txtSearch').val(name);
                // 상단 검색 버튼셋에서 cocode 활성화 (대부분 첫 버튼)
                const $cocode = $('#schOpt02 [data-val="cocode"]');
                if ($cocode.length) {
                    $('#schOpt02 [type="button"]').removeClass('on');
                    $cocode.addClass('on');
                }
            }""",
            company_name,
        )
        # 팝업 캡처
        with self.page.expect_popup(timeout=10_000) as popup_info:
            self.page.evaluate("typeof na_search_lookup === 'function' && na_search_lookup(1)")
        try:
            popup = popup_info.value
            popup.wait_for_load_state("networkidle", timeout=10_000)
            popup_html = popup.content()
            popup.close()
        except Exception as e:
            log.warning("ticker 룩업 팝업 캡처 실패: %s", e)
            return None

        # 팝업 결과에서 첫 6자리 ticker 추출
        # 매칭 패턴: onclick="...selectCmp('005930','삼성전자')..." 같은 형태
        candidates = re.findall(r"['\"](\d{6})['\"]", popup_html)
        if not candidates:
            log.warning("ticker 후보를 찾지 못함")
            return None
        # 가장 빈도 높은 코드를 선택 (회사 자체 코드일 가능성)
        from collections import Counter
        ticker = Counter(candidates).most_common(1)[0][0]
        log.info("ticker 결정: %s", ticker)
        return ticker

    # ------------------------------------------------------------------
    # 리포트 목록 (AJAX)
    # ------------------------------------------------------------------
    def list_reports(
        self,
        ticker: str,
        sort_by: Literal["latest", "popular"] = "latest",
        limit: int = 10,
        list_type: str = "33",
    ) -> list[ReportItem]:
        """ticker code로 리포트 목록 가져오기.

        wisereport는 응답 정렬을 별도 옵션으로 받지 않고 기본 최신순으로 반환.
        sort_by='popular'은 클라이언트 사이드에서 별도 호출이 필요해 현재는 latest와 동일.
        """
        log.info("리포트 목록 AJAX: ticker=%s (top %d)", ticker, limit)
        if "ReportList" not in self.page.url:
            self.page.goto(REPORT_LIST_URL, wait_until="networkidle")
        # jQuery + #hiddenUserID 가 준비될 때까지 대기 (세션 재사용 경로 보호)
        try:
            self.page.wait_for_function(
                "() => document.getElementById('hiddenUserID') !== null",
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            log.warning("#hiddenUserID 대기 타임아웃")
        self.page.wait_for_timeout(500)

        html = self.page.evaluate(
            """async ({ticker, listType}) => {
                const userIdEl = document.getElementById('hiddenUserID');
                const userId = userIdEl ? userIdEl.value : '';
                const r = await fetch('/wiseReport/reports/contentList.aspx', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: new URLSearchParams({
                        ListType: listType,
                        hiddencd: ticker,
                        hiddengubun: 'cocode',
                        bottomgubun: '',
                        hiddenbottomgubun: 'COCODE',
                        Content: '', Start: '', End: '', docDate: '',
                        langTyp: '1', hiddenQuery: '',
                        userId: userId,
                        pageNum: '1', flashYN: '1', isoCd: '', rptTyp: '0',
                    }).toString(),
                });
                return await r.text();
            }""",
            {"ticker": ticker, "listType": list_type},
        )

        items: list[ReportItem] = []
        # gotoSearchContent 패턴: (rpt_id, 'N', 'K', 'YYYYMMDDHHMMSS', 'sch_gubun', 'sch_cont', maybe more)
        pattern = re.compile(
            r"gotoSearchContent\((\d+)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'(\d+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'(?:\s*,\s*'[^']*')?\)[^>]*>([^<]+)</a>",
            re.S,
        )
        seen = set()
        for m in pattern.finditer(html):
            rpt_id = m.group(1)
            if rpt_id in seen:
                continue
            seen.add(rpt_id)
            items.append(
                ReportItem(
                    rpt_id=rpt_id,
                    sch_db=m.group(2),
                    sch_lang=m.group(3),
                    sch_dt=m.group(4),
                    sch_gubun=m.group(5),
                    sch_cont=m.group(6),
                    title=re.sub(r"\s+", " ", m.group(7)).strip()[:200],
                )
            )

        # wisereport 응답이 항상 최신순이라는 보장이 없어 명시적으로 sch_dt 내림차순.
        # sort_by="popular"은 별도 popular 호출이 필요해 현재는 latest와 동일하게 처리.
        items.sort(key=lambda it: it.sch_dt, reverse=True)
        if items:
            log.info(
                "list_reports ticker=%s 응답 %d건 (날짜 범위 %s ~ %s) → 상위 %d건 사용",
                ticker, len(items),
                items[-1].sch_dt[:8] if len(items) > 1 else items[0].sch_dt[:8],
                items[0].sch_dt[:8],
                min(limit, len(items)),
            )
        items = items[:limit]
        return items

    # ------------------------------------------------------------------
    # 조회수 Top 리포트 (산업/시황/글로벌/기업)
    # ------------------------------------------------------------------
    def list_top_reports(
        self,
        category: Literal["company", "industry", "strategy", "global"],
        sort_by: Literal["popular", "latest"] = "popular",
        limit: int = 10,
        days_back: int = 7,
        industry_gics: str | None = None,
    ) -> list[ReportItem]:
        """조회수 Top 페이지의 카테고리별 리포트 목록.

        sort_by="popular"은 wisereport 기본 (visit_cnt 내림차순) 그대로.
        sort_by="latest"는 응답에서 sch_dt 기준 client-side 재정렬.
        industry_gics를 주면 해당 산업으로 필터링 (산업 카테고리에서 유용).
        """
        if category not in TOPHITS_ENDPOINTS:
            raise ValueError(f"unknown category: {category}")
        path, rpt_typ, rpt_sub_typ, min_visit = TOPHITS_ENDPOINTS[category]

        # TopHits 페이지로 이동 (세션 쿠키 + JS context 보장)
        if "TopHits" not in self.page.url:
            self.page.goto(TOPHITS_URL, wait_until="networkidle")
        try:
            self.page.wait_for_function(
                "() => document.getElementById('hiddenUserID') !== null",
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            log.warning("TopHits #hiddenUserID 대기 타임아웃")
        self.page.wait_for_timeout(500)

        from datetime import datetime, timedelta, timezone

        kst = timezone(timedelta(hours=9))
        end = datetime.now(kst).strftime("%Y%m%d")
        start = (datetime.now(kst) - timedelta(days=days_back)).strftime("%Y%m%d")

        search_typ = "gicscode" if industry_gics else "cocode"
        search_val = industry_gics or ""

        log.info(
            "TopHits AJAX category=%s sort=%s days=%d industry=%s",
            category, sort_by, days_back, industry_gics,
        )

        html = self.page.evaluate(
            """async ({path, params}) => {
                const r = await fetch(path, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: new URLSearchParams(params).toString(),
                });
                return await r.text();
            }""",
            {
                "path": "/wiseReport/reports/" + path,
                "params": {
                    "startDT": start,
                    "endDt": end,
                    "langTyp": "1",
                    "rptTyp": rpt_typ,
                    "rptSubTyp": rpt_sub_typ,
                    "orderItem": "visit_cnt",
                    "orderTyp": "d",
                    "currentPage": "1",
                    "limit": "",
                    "minVisitCnt": min_visit,
                    "searchTyp": search_typ,
                    "searchVal": search_val,
                    "flashYN": "1",
                },
            },
        )

        # gotoSearch(rpt_id, sch_db, sch_lang, sch_dt, sch_cont) 5-args
        # (TopHits 페이지는 gotoSearchContent가 아닌 gotoSearch 사용)
        items: list[ReportItem] = []
        pattern = re.compile(
            r"gotoSearch\((\d+)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*(\d+)\s*,\s*'([^']*)'\)[^>]*>([^<]+)</a>",
            re.S,
        )
        seen = set()
        for m in pattern.finditer(html):
            rpt_id = m.group(1)
            if rpt_id in seen:
                continue
            seen.add(rpt_id)
            items.append(
                ReportItem(
                    rpt_id=rpt_id,
                    sch_db=m.group(2),
                    sch_lang=m.group(3),
                    sch_dt=m.group(4),
                    sch_gubun="",
                    sch_cont=m.group(5),
                    title=re.sub(r"\s+", " ", m.group(6)).strip()[:200],
                )
            )

        # 정렬 — list_reports와 동일 패턴 (명시적 sch_dt 내림차순).
        # sort_by="popular"은 wisereport 응답이 이미 visit_cnt 내림차순.
        if sort_by == "latest":
            items.sort(key=lambda it: it.sch_dt, reverse=True)

        if items:
            log.info(
                "list_top_reports category=%s sort=%s 응답 %d건 (날짜 범위 %s ~ %s) → 상위 %d건 사용",
                category, sort_by, len(items),
                items[-1].sch_dt[:8] if len(items) > 1 else items[0].sch_dt[:8],
                items[0].sch_dt[:8],
                min(limit, len(items)),
            )
        items = items[:limit]
        return items

    # ------------------------------------------------------------------
    # 산업명 → gicscode 룩업
    # ------------------------------------------------------------------
    def lookup_industry_code(self, industry_name: str) -> str | None:
        """산업명으로 gicscode 검색.

        wisereport의 popup 룩업을 이용. 회사명 룩업과 같은 방식.
        실패해도 None 반환하고 호출자가 처리.
        """
        log.info("산업 코드 룩업: %s", industry_name)
        if "ReportList" not in self.page.url:
            self.page.goto(REPORT_LIST_URL, wait_until="networkidle")
            self.page.wait_for_timeout(1500)

        # 검색 텍스트와 gicscode 모드 활성화
        self.page.evaluate(
            """(name) => {
                $('#txtSearch').val(name);
                const $b = $('#schOpt02 [data-val="gicscode"]');
                if ($b.length) {
                    $('#schOpt02 [type="button"]').removeClass('on');
                    $b.addClass('on');
                }
            }""",
            industry_name,
        )
        try:
            with self.page.expect_popup(timeout=10_000) as popup_info:
                self.page.evaluate("typeof na_search_lookup === 'function' && na_search_lookup(1)")
            popup = popup_info.value
            popup.wait_for_load_state("networkidle", timeout=10_000)
            popup_html = popup.content()
            popup.close()
        except Exception as e:
            log.warning("산업 룩업 팝업 캡처 실패: %s", e)
            return None

        # gicscode는 보통 4~10자리 숫자/알파벳 조합. 다양한 형태가 있어 첫 후보 추출.
        # 일반적인 코드: G2510, G45, etc. 또는 4-10자리 숫자.
        m = re.search(r"selectInd\('([A-Z0-9]+)'", popup_html)
        if m:
            log.info("산업 코드 결정 (selectInd): %s", m.group(1))
            return m.group(1)
        # 백업: 단순 추출
        m = re.search(r"['\"]([A-Z][0-9]{2,8}|[0-9]{4,8})['\"]\s*,\s*['\"][^'\"]+['\"]", popup_html)
        if m:
            log.info("산업 코드 결정 (fallback): %s", m.group(1))
            return m.group(1)
        log.warning("산업 코드 후보 없음")
        return None

    # ------------------------------------------------------------------
    # 산업코드 → 리포트 목록 (인기/최신)
    # ------------------------------------------------------------------
    def list_industry_reports(
        self,
        industry_gics: str,
        sort_by: Literal["popular", "latest"] = "popular",
        limit: int = 5,
        days_back: int = 30,
    ) -> list[ReportItem]:
        """특정 산업의 리포트 — 산업 카테고리 TopHits에서 gicscode 필터."""
        return self.list_top_reports(
            category="industry",
            sort_by=sort_by,
            limit=limit,
            days_back=days_back,
            industry_gics=industry_gics,
        )

    # ------------------------------------------------------------------
    # 개별 PDF 다운로드
    # ------------------------------------------------------------------
    def _resolve_pdf_target(self, item: ReportItem) -> PdfTarget | None:
        """리포트의 LoadReportBody.aspx 응답에서 PDF 다운로드용 파라미터 추출."""
        body_url = (
            f"{BASE_URL}/comm/LoadReportBody.aspx"
            f"?rpt_id={item.rpt_id}"
            f"&sch_db={item.sch_db}"
            f"&sch_lang={item.sch_lang}"
            f"&sch_dt={item.sch_dt}"
            f"&sch_cont={item.sch_cont}"
            f"&view_lang=K"
        )
        body_page = self._context.new_page()  # type: ignore[union-attr]
        try:
            body_page.goto(body_url, wait_until="networkidle")
            body_html = body_page.content()
        finally:
            body_page.close()

        m = re.search(
            r"openContent\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'([^']+\.pdf)'\s*,",
            body_html,
        )
        if not m:
            log.warning("PDF target 추출 실패: rpt_id=%s", item.rpt_id)
            return None
        return PdfTarget(rpt_id=m.group(1), brk_cd=m.group(2), fpath=m.group(3))

    @staticmethod
    def _safe_filename(s: str) -> str:
        s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", s)
        return s.strip().strip(".")[:120] or "report"

    def download_report(
        self,
        item: ReportItem,
        target_dir: Path,
        index: int = 0,
    ) -> Path | None:
        target_dir.mkdir(parents=True, exist_ok=True)
        pdf = self._resolve_pdf_target(item)
        if not pdf:
            return None

        load_url = (
            f"{BASE_URL}/comm/LoadReport.aspx"
            f"?rpt_id={pdf.rpt_id}"
            f"&brk_cd={pdf.brk_cd}"
            f"&fpath={quote(pdf.fpath)}"
            f"&view_lang=K"
        )
        viewer = self._context.new_page()  # type: ignore[union-attr]
        try:
            with viewer.expect_download(timeout=30_000) as dl_info:
                viewer.goto(load_url, wait_until="commit")
            download = dl_info.value
            safe_title = self._safe_filename(item.title)
            filename = f"{index:02d}_{item.date_short}_{safe_title}.pdf"
            out = target_dir / filename
            download.save_as(out)
            log.info("저장: %s (%d bytes)", out, out.stat().st_size)
            return out
        except Exception:
            log.exception("다운로드 실패: rpt_id=%s", item.rpt_id)
            return None
        finally:
            viewer.close()

    def download_reports(
        self,
        items: list[ReportItem],
        target_dir: Path,
    ) -> list[Path]:
        saved: list[Path] = []
        for i, item in enumerate(items, start=1):
            log.info("[%d/%d] %s", i, len(items), item.title[:60])
            try:
                p = self.download_report(item, target_dir, index=i)
                if p:
                    saved.append(p)
            except Exception:
                log.exception("실패: %s", item.title)
            self.page.wait_for_timeout(500)
        return saved
