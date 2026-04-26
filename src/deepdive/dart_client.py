"""DART (전자공시) API 클라이언트.

dart-fss 라이브러리에 직접 의존하지 않고, OpenDART HTTP API를 httpx로 직접 호출.
이유:
  - dart-fss는 문서 다운로드 시 경로 의존성이 있어 Docker 컨테이너 권한 이슈 가능
  - 우리가 필요한 endpoint는 4-5개뿐
  - 직접 호출이 디버깅·격리 측면에서 단순

환경변수 DART_API_KEY 필수 (handler.register()에서 사전 체크).
모든 함수는 graceful — 실패 시 None/빈 dict 반환, 예외 던지지 않음.
"""

from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

DART_BASE = "https://opendart.fss.or.kr/api"
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do"
DART_DOC_DOWN = "https://dart.fss.or.kr/pdf/download/main.do"

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
KST = timezone(timedelta(hours=9))

# 다운로드 크기 가드 (압축/비압축 양쪽).
# - corpCode: 정상 ~3MB, 사업보고서 zip: 보통 ~30MB (수십 페이지 PDF).
# - 100MB 캡: 정상 응답은 절대 도달 안 하고, 악의적/오류 응답만 차단.
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 100 * 1024 * 1024

# 잠정실적 공시 LLM 파싱용 텍스트 윈도우 (실적표는 보통 첫 1-2 페이지에 위치).
PRELIMINARY_TEXT_WINDOW = 40_000


# 사업보고서·반기·분기보고서 코드 (DART pblntf_detail_ty)
PERIODIC_REPORT_CODES = ("A001", "A002", "A003")  # 사업/반기/분기

# IR 관련 공시 코드 (B001=주요사항보고, I001=거래소공시 IR관련 - 환경별 변동 가능)
# 보수적으로 보고서명 텍스트 매칭도 함께 사용
IR_REPORT_CODES = ("I001",)

# IR 자료로 인정할 보고서명 키워드 (substring match, 대소문자 구분).
# 컨퍼런스콜·어닝콜·실적설명회 같이 PDF 첨부가 더 잘 붙어있는 종류까지 포함.
IR_KEYWORDS = (
    "IR자료", "IR공시", "IR ",
    "실적발표", "실적설명회", "실적 설명회", "실적 발표",
    "기업설명회", "투자설명회", "사업설명회",
    "컨퍼런스콜", "컨퍼런스 콜", "어닝콜", "어닝 콜",
    "Earnings", "Conference Call", "Investor Day", "Analyst Day",
    "분석가 간담회", "분석가간담회", "애널리스트",
)


@dataclass
class DartReport:
    rcept_no: str       # 접수번호 (URL 파라미터)
    report_nm: str      # 보고서명
    rcept_dt: str       # 접수일자 YYYYMMDD
    flr_nm: str         # 제출인


@dataclass
class FinancialMetrics:
    """회사 전체 분기별 매출/영업이익/순이익 (단위: 백만원)."""
    revenue_qoq: dict[str, int]    # "2024Q1" → 매출액
    op_profit_qoq: dict[str, int]  # 영업이익
    net_profit_qoq: dict[str, int] # 당기순이익


def _api_key() -> str | None:
    return os.getenv("DART_API_KEY")


def _get(path: str, params: dict[str, Any], timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> dict | None:
    """OpenDART JSON API 호출. 실패 시 None."""
    key = _api_key()
    if not key:
        log.warning("DART_API_KEY 미설정")
        return None
    params = {"crtfc_key": key, **params}
    try:
        with httpx.Client(timeout=timeout, verify=False) as cli:
            r = cli.get(f"{DART_BASE}/{path}", params=params)
            r.raise_for_status()
            data = r.json()
            status = str(data.get("status", "000"))
            # 000=정상, 013=조회된 데이터가 없음 (정상이지만 결과 없음)
            if status not in ("000", "013"):
                log.warning("DART API status=%s message=%s path=%s", status, data.get("message"), path)
                return None if status != "013" else data
            return data
    except httpx.HTTPError as e:
        log.warning("DART API HTTP 에러 (%s): %s", path, e)
        return None
    except Exception:
        log.exception("DART API 예상치 못한 에러 (%s)", path)
        return None


# ------------------------------------------------------------------
# 1) ticker → corp_code 매핑
# ------------------------------------------------------------------
_CORP_MAP_CACHE: dict[str, str] | None = None  # ticker → corp_code
_CORP_NAME_CACHE: dict[str, str] = {}  # ticker → corp_name
_NAME_TO_TICKER_CACHE: dict[str, str] = {}  # corp_name (정규화) → ticker


def _load_corp_map() -> dict[str, str]:
    """DART 전체 회사 목록 ZIP 다운로드 → ticker(stock_code) → corp_code 매핑 생성.

    한 번 캐싱. 컨테이너 재시작 시까지 메모리 보관.
    """
    global _CORP_MAP_CACHE
    if _CORP_MAP_CACHE is not None:
        return _CORP_MAP_CACHE

    key = _api_key()
    if not key:
        log.warning("DART_API_KEY 미설정")
        return {}

    log.info("DART 회사목록 다운로드 시작 (CORPCODE.xml)")
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), verify=False) as cli:
            r = cli.get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": key})
            r.raise_for_status()
            if len(r.content) > MAX_DOWNLOAD_BYTES:
                log.warning("corpCode 응답 비정상 크기 %d bytes (cap %d)", len(r.content), MAX_DOWNLOAD_BYTES)
                return {}
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            xml_member = zf.getinfo("CORPCODE.xml")
            if xml_member.file_size > MAX_ZIP_MEMBER_BYTES:
                log.warning("CORPCODE.xml 비정상 크기 %d bytes", xml_member.file_size)
                return {}
            xml_content = zf.read(xml_member).decode("utf-8")

        from xml.etree import ElementTree as ET
        root = ET.fromstring(xml_content)
        m: dict[str, str] = {}
        for el in root.iter("list"):
            stock = (el.findtext("stock_code") or "").strip()
            corp = (el.findtext("corp_code") or "").strip()
            name = (el.findtext("corp_name") or "").strip()
            if stock and corp:
                # ticker는 보통 6자리 숫자
                ticker6 = stock.zfill(6)
                m[ticker6] = corp
                _CORP_NAME_CACHE[ticker6] = name
                # 종목명 → ticker 역색인 (lookup_ticker_by_name용)
                if name:
                    _NAME_TO_TICKER_CACHE[name] = ticker6
                    # 정규화 키도 추가 (공백/괄호 제거)
                    norm = re.sub(r"[\s()㈜（）]+", "", name)
                    if norm and norm != name:
                        _NAME_TO_TICKER_CACHE.setdefault(norm, ticker6)
        log.info("DART 회사목록 파싱 완료: %d개 (이름역색인 %d개)", len(m), len(_NAME_TO_TICKER_CACHE))
        _CORP_MAP_CACHE = m
        return m
    except Exception:
        log.exception("DART 회사목록 다운로드 실패")
        return {}


def get_corp_code(ticker: str) -> tuple[str, str] | None:
    """ticker → (corp_code, corp_name) 또는 None."""
    t = ticker.strip().zfill(6)
    m = _load_corp_map()
    code = m.get(t)
    if not code:
        return None
    return code, _CORP_NAME_CACHE.get(t, ticker)


def lookup_ticker_by_name(query: str) -> str | None:
    """종목명 → 6자리 ticker. 정확 일치 → contains 매칭 순.

    매칭 우선순위:
      1. 정확히 일치 ("삼성전자" == "삼성전자")
      2. 정규화 후 일치 (공백/괄호 제거: "(주)카카오" → "카카오")
      3. startswith 매칭, 짧은 이름 우선
      4. contains 매칭, 짧은 이름 우선

    상장사만 검색 (stock_code 있는 회사). 못 찾으면 None.
    """
    if not query:
        return None
    _load_corp_map()  # cache 빌드
    if not _NAME_TO_TICKER_CACHE:
        return None

    q = query.strip()

    # 1) 정확 일치
    if q in _NAME_TO_TICKER_CACHE:
        log.info("종목명 정확 매칭: '%s' → %s", q, _NAME_TO_TICKER_CACHE[q])
        return _NAME_TO_TICKER_CACHE[q]

    # 2) 정규화 후 일치
    q_norm = re.sub(r"[\s()㈜（）]+", "", q)
    if q_norm in _NAME_TO_TICKER_CACHE:
        log.info("종목명 정규화 매칭: '%s'→'%s' → %s", q, q_norm, _NAME_TO_TICKER_CACHE[q_norm])
        return _NAME_TO_TICKER_CACHE[q_norm]

    # 3, 4) startswith → contains, 짧은 이름 우선 (모회사가 보통 짧음)
    starts: list[tuple[str, str]] = []
    contains: list[tuple[str, str]] = []
    for name, ticker in _NAME_TO_TICKER_CACHE.items():
        if name.startswith(q):
            starts.append((name, ticker))
        elif q in name:
            contains.append((name, ticker))

    candidates = sorted(starts, key=lambda x: len(x[0])) or sorted(contains, key=lambda x: len(x[0]))
    if candidates:
        chosen = candidates[0]
        log.info(
            "종목명 부분 매칭: '%s' → '%s' (%s) [총 %d 후보]",
            q, chosen[0], chosen[1], len(candidates),
        )
        return chosen[1]

    log.warning("종목명 매칭 실패: '%s'", q)
    return None


# ------------------------------------------------------------------
# 2) 사업보고서 메타데이터 + PDF 다운로드
# ------------------------------------------------------------------
def fetch_latest_business_report(corp_code: str) -> DartReport | None:
    """가장 최근 사업/반기/분기 보고서 메타데이터 조회 (실 PDF는 별도)."""
    today = datetime.now(KST).strftime("%Y%m%d")
    one_year_ago = (datetime.now(KST) - timedelta(days=400)).strftime("%Y%m%d")

    for code in PERIODIC_REPORT_CODES:
        data = _get("list.json", {
            "corp_code": corp_code,
            "bgn_de": one_year_ago,
            "end_de": today,
            "pblntf_detail_ty": code,
            "page_count": 5,
        })
        if not data:
            continue
        items = data.get("list") or []
        if not items:
            continue
        # 최신 정렬 (rcept_dt 내림차순)
        items.sort(key=lambda x: x.get("rcept_dt", ""), reverse=True)
        top = items[0]
        log.info("사업/반기/분기 보고서 발견 (%s): %s", code, top.get("report_nm"))
        return DartReport(
            rcept_no=top["rcept_no"],
            report_nm=top["report_nm"],
            rcept_dt=top["rcept_dt"],
            flr_nm=top.get("flr_nm", ""),
        )
    log.warning("사업보고서를 찾지 못함: corp_code=%s", corp_code)
    return None


def fetch_ir_candidates(corp_code: str, days_back: int = 365 * 5) -> list[DartReport]:
    """최근 IR자료/실적발표/컨퍼런스콜 등 후보 리스트 (날짜 내림차순). 빈 리스트 가능.

    PDF 첨부 비율을 높이기 위해 5년치까지 수집. 키워드는 IR_KEYWORDS 참조.
    """
    today = datetime.now(KST).strftime("%Y%m%d")
    bgn_de = (datetime.now(KST) - timedelta(days=days_back)).strftime("%Y%m%d")

    seen_rcept: set[str] = set()
    candidates: list[DartReport] = []

    def _matches(nm: str) -> bool:
        return any(kw in nm for kw in IR_KEYWORDS)

    # 1순위: pblntf_detail_ty=I001 (거래소 공시 - IR자료)
    for code in IR_REPORT_CODES:
        data = _get("list.json", {
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": today,
            "pblntf_detail_ty": code,
            "page_count": 100,
        })
        if not data:
            continue
        for it in data.get("list") or []:
            nm = it.get("report_nm", "")
            if _matches(nm):
                rcept = it["rcept_no"]
                if rcept in seen_rcept:
                    continue
                seen_rcept.add(rcept)
                candidates.append(DartReport(
                    rcept_no=rcept, report_nm=nm,
                    rcept_dt=it["rcept_dt"], flr_nm=it.get("flr_nm", ""),
                ))

    # 2순위: 전체 공시에서 IR 키워드 매칭 (페이지 여러 장 순회)
    for page_no in range(1, 4):  # 최대 300건 (page_count=100 * 3 페이지)
        data = _get("list.json", {
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": today,
            "page_count": 100,
            "page_no": page_no,
        })
        if not data:
            break
        items = data.get("list") or []
        if not items:
            break
        any_added = False
        for it in items:
            nm = it.get("report_nm", "")
            if _matches(nm):
                rcept = it["rcept_no"]
                if rcept in seen_rcept:
                    continue
                seen_rcept.add(rcept)
                candidates.append(DartReport(
                    rcept_no=rcept, report_nm=nm,
                    rcept_dt=it["rcept_dt"], flr_nm=it.get("flr_nm", ""),
                ))
                any_added = True
        # 다음 페이지에 더 오래된 데이터만 있고 마지막 페이지면 종료
        if data.get("total_page") and page_no >= int(data["total_page"]):
            break
        if not any_added and page_no >= 1:
            # 페이지에 매칭 없으면 더 멀리까지 안 봐도 됨 (보수적으로 page 2까지는 봄)
            if page_no >= 2:
                break

    candidates.sort(key=lambda x: x.rcept_dt, reverse=True)
    log.info("IR 후보 수집: %d건 (corp_code=%s, days_back=%d)", len(candidates), corp_code, days_back)
    return candidates


def fetch_latest_ir_with_pdf(
    corp_code: str, target_dir: Path, max_attempts: int = 15, days_back: int = 365 * 5,
) -> tuple[DartReport, Path] | None:
    """가장 최근 IR 공시 중 PDF 첨부가 있는 첫 번째를 다운로드.

    후보를 max_attempts개까지 순차 다운받아 PDF가 있는 것 발견 즉시 반환.
    PDF 있는 후보 없으면 None. 후보가 없는 경우도 None.

    days_back을 길게 잡는 이유: 최근 IR자료가 텍스트 공시뿐인 케이스를 위해
    과거의 컨퍼런스콜·실적발표 PDF까지 폴백.
    """
    candidates = fetch_ir_candidates(corp_code, days_back=days_back)
    if not candidates:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    n_try = min(max_attempts, len(candidates))
    for i, cand in enumerate(candidates[:max_attempts]):
        log.info("IR 후보 %d/%d 다운로드 시도: %s (%s)", i + 1, n_try, cand.report_nm, cand.rcept_dt)
        pdf = download_report_archive(cand.rcept_no, target_dir, require_pdf=True)
        if pdf:
            log.info("IR PDF 발견: %s (%s) → %s", cand.report_nm, cand.rcept_dt, pdf.name)
            return cand, pdf
    log.info("IR 공시 %d개 시도했으나 PDF 첨부 없음 (corp_code=%s)", n_try, corp_code)
    return None


def download_report_archive(
    rcept_no: str, target_dir: Path, require_pdf: bool = False,
) -> Path | None:
    """rcept_no → 보고서 첨부서류(zip) 다운로드.

    PDF가 있으면 PDF 우선. 없을 때:
      - require_pdf=True: None 반환
      - require_pdf=False: 가장 큰 파일 (보통 본문 XML) 반환
    """
    key = _api_key()
    if not key:
        return None
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), verify=False) as cli:
            r = cli.get(f"{DART_BASE}/document.xml", params={"crtfc_key": key, "rcept_no": rcept_no})
            r.raise_for_status()
            if len(r.content) > MAX_DOWNLOAD_BYTES:
                log.warning("document.xml 비정상 크기 %d bytes (rcept_no=%s)", len(r.content), rcept_no)
                return None
            content_type = r.headers.get("content-type", "").lower()
            if "xml" in content_type and b"<status>" in r.content[:200]:
                log.warning("document.xml 에러 응답 (rcept_no=%s): %s", rcept_no, r.content[:300])
                return None
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            members = zf.infolist()
            if not members:
                return None
            pdf_members = [m for m in members if m.filename.lower().endswith(".pdf")]
            if pdf_members:
                picked = max(pdf_members, key=lambda m: m.file_size)
            elif require_pdf:
                log.info("PDF 첨부 없음 (rcept_no=%s) — require_pdf=True라 스킵", rcept_no)
                return None
            else:
                picked = max(members, key=lambda m: m.file_size)
            if picked.file_size > MAX_ZIP_MEMBER_BYTES:
                log.warning(
                    "zip member 비정상 크기 %d bytes (rcept_no=%s, member=%s)",
                    picked.file_size, rcept_no, picked.filename,
                )
                return None
            ext = Path(picked.filename).suffix.lower() or ".bin"
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(picked.filename).stem)[:80] or "report"
            out_path = target_dir / f"{rcept_no}_{safe_name}{ext}"
            out_path.write_bytes(zf.read(picked))
            log.info("DART 문서 저장: %s (%d bytes)", out_path, out_path.stat().st_size)
            return out_path
    except Exception:
        log.exception("DART 문서 다운로드 실패 rcept_no=%s", rcept_no)
        return None


# ------------------------------------------------------------------
# 3) 분기별 회사 전체 재무제표 (매출/영업이익/순이익)
# ------------------------------------------------------------------
def fetch_quarterly_financials(corp_code: str, years: int = 3) -> FinancialMetrics:
    """최근 N년 분기별 누적 재무 → 분기 단위로 변환.

    API: fnlttSinglAcntAll.json (전체 계정과목)
    DART는 1Q/반기/3Q/연간 누적(YTD) 값으로 제공 → 분기값은 직접 계산.
    """
    revenue_qoq: dict[str, int] = {}
    op_qoq: dict[str, int] = {}
    net_qoq: dict[str, int] = {}

    now_year = datetime.now(KST).year
    # 보고서코드: 11013=1분기, 11012=반기, 11014=3분기, 11011=사업보고서(연간)
    quarter_codes = [
        ("11013", "Q1"),
        ("11012", "Q2"),  # 반기 누적
        ("11014", "Q3"),  # 3분기 누적
        ("11011", "Q4"),  # 사업보고서 누적(연간)
    ]

    for year in range(now_year - years, now_year + 1):
        prev_revenue = 0
        prev_op = 0
        prev_net = 0
        for reprt_code, q_label in quarter_codes:
            data = _get("fnlttSinglAcntAll.json", {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": reprt_code,
                "fs_div": "CFS",  # 연결재무제표 (없으면 OFS도 시도)
            })
            list_items = (data or {}).get("list") or []
            if not list_items:
                # 연결 없으면 별도(OFS) 시도
                data = _get("fnlttSinglAcntAll.json", {
                    "corp_code": corp_code,
                    "bsns_year": str(year),
                    "reprt_code": reprt_code,
                    "fs_div": "OFS",
                })
                list_items = (data or {}).get("list") or []
            if not list_items:
                continue

            ytd = _extract_pl_metrics(list_items)
            if not ytd:
                continue
            rev_ytd, op_ytd, net_ytd = ytd

            # 분기값 = YTD - 이전까지 YTD
            label = f"{year}{q_label}"
            revenue_qoq[label] = rev_ytd - prev_revenue
            op_qoq[label] = op_ytd - prev_op
            net_qoq[label] = net_ytd - prev_net
            prev_revenue, prev_op, prev_net = rev_ytd, op_ytd, net_ytd

    log.info(
        "분기 재무 추출: revenue=%d, op=%d, net=%d 분기",
        len(revenue_qoq), len(op_qoq), len(net_qoq),
    )
    return FinancialMetrics(
        revenue_qoq=revenue_qoq,
        op_profit_qoq=op_qoq,
        net_profit_qoq=net_qoq,
    )


# ------------------------------------------------------------------
# 3-bis) 잠정실적 (영업(잠정)실적공시) — 정식 분기보고서 전 회사가 미리 발표하는 매출/영업이익
# ------------------------------------------------------------------
PRELIMINARY_KEYWORDS = (
    "영업(잠정)실적", "영업잠정실적",
    "잠정실적", "잠정 실적",
    "(잠정)실적", "(잠정) 실적",
    "Earnings Release", "earnings release",
)


def fetch_latest_preliminary_quarter(
    corp_code: str, target_dir: Path, days_back: int = 90,
) -> tuple[str, int, int, int] | None:
    """가장 최근 영업(잠정)실적 공시에서 매출/영업이익/순이익 + 분기 라벨 추출.

    반환: ("2026Q1", revenue_won, op_profit_won, net_profit_won) 또는 None.

    흐름:
      1. list.json — PRELIMINARY_KEYWORDS 매칭, 최근 days_back일.
      2. 첫 번째(최근) 공시 archive 다운로드 → 텍스트 추출.
      3. LLM이 분기 라벨 + 3개 수치를 JSON으로 추출.

    LLM/네트워크/파싱 어느 단계가 실패해도 None.
    """
    today = datetime.now(KST).strftime("%Y%m%d")
    bgn_de = (datetime.now(KST) - timedelta(days=days_back)).strftime("%Y%m%d")

    # I001 (거래소 공시) + 전체 공시 둘 다 검색 — 회사마다 분류가 다름
    candidates: list[DartReport] = []
    seen: set[str] = set()

    def _matches(nm: str) -> bool:
        return any(kw in nm for kw in PRELIMINARY_KEYWORDS)

    queries = [
        {"corp_code": corp_code, "bgn_de": bgn_de, "end_de": today,
         "pblntf_detail_ty": "I001", "page_count": 50},
        {"corp_code": corp_code, "bgn_de": bgn_de, "end_de": today,
         "page_count": 100},
    ]
    for q in queries:
        data = _get("list.json", q)
        if not data:
            continue
        for it in data.get("list") or []:
            nm = it.get("report_nm", "")
            if _matches(nm):
                rcept = it["rcept_no"]
                if rcept in seen:
                    continue
                seen.add(rcept)
                candidates.append(DartReport(
                    rcept_no=rcept, report_nm=nm,
                    rcept_dt=it["rcept_dt"], flr_nm=it.get("flr_nm", ""),
                ))

    if not candidates:
        log.info("잠정실적 공시 없음 (corp_code=%s, %d일 이내)", corp_code, days_back)
        return None
    candidates.sort(key=lambda x: x.rcept_dt, reverse=True)
    log.info("잠정실적 후보 %d건, 최신=%s (%s)", len(candidates), candidates[0].report_nm, candidates[0].rcept_dt)

    target_dir.mkdir(parents=True, exist_ok=True)
    # 최대 3개 후보까지 시도 (일부는 빈 archive 가능)
    for cand in candidates[:3]:
        path = download_report_archive(cand.rcept_no, target_dir, require_pdf=False)
        if not path:
            continue
        text = extract_doc_text(path, max_chars=PRELIMINARY_TEXT_WINDOW, prefer_section="")
        if not text.strip():
            continue
        parsed = _llm_parse_preliminary(text, cand.report_nm)
        if parsed:
            log.info("잠정실적 추출 성공: %s → %s", cand.report_nm, parsed)
            return parsed
    log.warning("잠정실적 후보 모두에서 LLM 추출 실패 (corp_code=%s)", corp_code)
    return None


def _llm_parse_preliminary(text: str, report_nm: str) -> tuple[str, int, int, int] | None:
    """잠정실적 본문 텍스트 → ("2026Q1", revenue, op, net). 실패 시 None."""
    try:
        from src import summarizer
    except Exception:
        log.exception("summarizer import 실패 (잠정실적 파싱)")
        return None
    try:
        client = summarizer.get_client()
    except Exception:
        log.exception("OpenRouter client 초기화 실패 (잠정실적 파싱)")
        return None

    sys_prompt = (
        "당신은 한국 기업의 영업(잠정)실적 공시에서 숫자를 추출하는 도구입니다. "
        "입력 텍스트에서 다음 4개를 JSON으로만 출력하세요. 자연어 설명 금지.\n"
        "  - year_quarter: 'YYYYQn' 형식 (예: '2026Q1')\n"
        "  - revenue: 매출액 (원 단위 정수, 조원이면 ×1조)\n"
        "  - op_profit: 영업이익 (원 단위 정수, 적자면 음수)\n"
        "  - net_profit: 당기순이익 (원 단위 정수, 적자면 음수)\n"
        "당기 실적(이번 분기)만 추출 (전년동기·전분기 비교는 무시).\n"
        "찾지 못한 항목은 null. 모두 못 찾으면 빈 객체 {}.\n\n"
        '예: {"year_quarter": "2026Q1", "revenue": 17000000000000, "op_profit": 8000000000000, "net_profit": 5000000000000}'
    )
    try:
        resp = client.chat.completions.create(
            model=summarizer.DEFAULT_MODEL,
            max_tokens=400,
            temperature=0.0,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": (
                    f"공시명: {report_nm}\n\n<doc_text>\n{text[:25_000]}\n</doc_text>"
                )},
            ],
        )
    except Exception:
        log.exception("LLM 잠정실적 파싱 호출 실패")
        return None

    content = (resp.choices[0].message.content or "").strip()
    import json as _json
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        content = fence.group(1)
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        log.warning("잠정실적 LLM 응답에 JSON 없음: %s", content[:300])
        return None
    try:
        obj = _json.loads(m.group(0))
    except Exception:
        log.exception("잠정실적 JSON 파싱 실패: %s", content[:300])
        return None
    if not isinstance(obj, dict):
        return None
    yq = obj.get("year_quarter")
    rev = obj.get("revenue")
    op = obj.get("op_profit")
    net = obj.get("net_profit")
    if not isinstance(yq, str) or not re.match(r"^\d{4}Q[1-4]$", yq):
        log.warning("잠정실적 year_quarter 형식 이상: %r", yq)
        return None
    if not isinstance(rev, (int, float)):
        log.warning("잠정실적 revenue 미발견")
        return None
    return (
        yq,
        int(rev),
        int(op) if isinstance(op, (int, float)) else 0,
        int(net) if isinstance(net, (int, float)) else 0,
    )


def _extract_pl_metrics(items: list[dict]) -> tuple[int, int, int] | None:
    """fnlttSinglAcntAll 결과에서 매출액/영업이익/당기순이익 YTD(누적) 추출 (원 단위).

    DART API의 IS/CIS 항목에는 두 금액 필드가 있다:
      - thstrm_amount: 당분기 standalone (3개월)
      - thstrm_add_amount: 당기 누적 (반기 6개월/3분기 9개월/연간 12개월 = YTD)

    우리는 분기 단위 추출을 위해 YTD를 사용 (Q2 = 반기누적 - Q1, ...).
    1Q/연간 보고서에서는 add_amount가 비어있을 수 있으므로 standalone fallback.
    """
    def _parse(s) -> int | None:
        if not s:
            return None
        try:
            return int(str(s).replace(",", "").strip())
        except (ValueError, AttributeError):
            return None

    rev = op = net = None
    for it in items:
        sj = it.get("sj_div", "")  # IS=손익계산서, CIS=포괄손익계산서
        if sj not in ("IS", "CIS"):
            continue
        nm = (it.get("account_nm") or "").replace(" ", "")

        # 누적(YTD) 우선, 없으면 standalone
        add_amt = _parse(it.get("thstrm_add_amount"))
        std_amt = _parse(it.get("thstrm_amount"))
        amount = add_amt if add_amt is not None else std_amt
        if amount is None:
            continue

        if rev is None and ("매출액" in nm or "수익(매출액)" in nm or nm == "영업수익"):
            rev = amount
        elif op is None and "영업이익" in nm and "영업이익률" not in nm:
            op = amount
        elif net is None and ("당기순이익" in nm or "분기순이익" in nm or "반기순이익" in nm) and "비지배" not in nm:
            net = amount
    if rev is None:
        return None
    return rev, op or 0, net or 0


# ------------------------------------------------------------------
# 4) 보고서 본문 텍스트 추출 (PDF / XML / HTML 통합)
# ------------------------------------------------------------------
def extract_doc_text(path: Path, max_chars: int = 80_000, prefer_section: str = "사업의 내용") -> str:
    """DART 보고서 파일에서 텍스트 추출 - 매우 견고하게 (다단계 폴백).

    DART는 사업보고서를 자체 XML 포맷으로 제공 (PDF 아님). 파일 확장자에 따라
    적절한 파서 선택. 가능하면 'prefer_section'에 해당하는 섹션만 추출,
    못 찾으면 전체 텍스트 + 앞에서부터 max_chars로 잘림.

    실패 시 빈 문자열 반환 (예외 던지지 않음).
    """
    if not path or not path.exists():
        log.warning("extract_doc_text: path 없음 (%s)", path)
        return ""

    size = path.stat().st_size
    ext = path.suffix.lower()
    log.info("extract_doc_text: %s (%d bytes, ext=%s)", path.name, size, ext)

    try:
        if ext == ".pdf":
            text = _extract_pdf(path, max_chars)
        elif ext in (".xml", ".html", ".htm"):
            text = _extract_xml_or_html(path, max_chars, prefer_section)
        elif ext in (".txt",):
            text = path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
        else:
            # 알 수 없는 형식: 일단 텍스트로 시도
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
            except Exception:
                text = ""

        if text and text.strip():
            log.info("extract_doc_text: %d자 추출 성공", len(text))
            return text

        # 1차 실패 - 폴백: 파일을 최대한 일반 텍스트로 읽고 태그 제거
        log.warning("extract_doc_text 1차 시도 빈 텍스트 — fallback 시도")
        return _fallback_strip_tags(path, max_chars)
    except Exception:
        log.exception("보고서 본문 텍스트 추출 1차 실패: %s — fallback 시도", path)
        try:
            return _fallback_strip_tags(path, max_chars)
        except Exception:
            log.exception("보고서 본문 텍스트 추출 fallback도 실패")
            return ""


def _fallback_strip_tags(path: Path, max_chars: int) -> str:
    """최후 수단: 파일을 텍스트로 읽고 정규식으로 XML/HTML 태그 제거."""
    raw = path.read_bytes()
    for enc in ("utf-8", "cp949", "euc-kr", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="ignore")
    # XML/HTML 태그 제거
    text = re.sub(r"<[^>]+>", " ", text)
    # 연속 공백 정리
    text = re.sub(r"\s+", " ", text).strip()
    log.info("_fallback_strip_tags: %d자 추출 (정규식 폴백)", len(text))
    return text[:max_chars]


def _extract_pdf(path: Path, max_chars: int) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    parts: list[str] = []
    total = 0
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        chunk = f"[Page {i}]\n{text}"
        parts.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            parts.append(f"\n... (이후 {len(reader.pages) - i}페이지 생략)")
            break
    return "\n\n".join(parts)


def _extract_xml_or_html(path: Path, max_chars: int, prefer_section: str | None) -> str:
    """DART XML/HTML 보고서에서 텍스트 추출.

    DART XML 구조 예시:
      <DOCUMENT>
        <SECTION-1>
          <TITLE>I. 회사의 개요</TITLE>
          ...
        </SECTION-1>
        <SECTION-1>
          <TITLE>II. 사업의 내용</TITLE>  ← prefer_section과 매칭
          ...
        </SECTION-1>
      </DOCUMENT>

    'prefer_section'이 들어 있는 섹션을 우선 추출. 못 찾으면 전체.
    """
    from bs4 import BeautifulSoup

    raw = path.read_bytes()
    # 인코딩 감지
    text_content: str = ""
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            text_content = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text_content:
        text_content = raw.decode("utf-8", errors="ignore")

    # XML/HTML 파서로 시도
    soup = None
    for parser in ("lxml-xml", "xml", "html.parser"):
        try:
            soup = BeautifulSoup(text_content, parser)
            break
        except Exception:
            continue
    if soup is None:
        return text_content[:max_chars]

    # prefer_section 검색 - 모든 태그를 순회하며 텍스트가 일치하는 노드 찾기.
    # prefer_section이 비어있으면 섹션 검색 skip → 전체 본문 사용.
    target_text: str = ""
    if not prefer_section:
        target_text = soup.get_text(separator="\n", strip=True)
        return target_text[:max_chars]

    def _is_section_tag(t) -> bool:
        if not getattr(t, "name", None):
            return False
        return "section" in t.name.lower()

    # 직접 텍스트(자식 태그 제외)에 prefer_section이 들어있는 leaf-like 태그 찾기.
    # find_all(True)는 DOCUMENT 루트부터 시작하므로 .get_text()로 검색하면
    # 루트가 매칭되어 버림. direct text만 봐야 TITLE 노드를 정확히 잡음.
    title_tag = None
    for tag in soup.find_all(True):
        direct = "".join(c for c in tag.contents if isinstance(c, str)).strip()
        if not direct:
            continue
        if prefer_section in direct and len(direct) < 80:
            title_tag = tag
            break

    if title_tag is not None:
        # 가장 가까운 SECTION-* 부모 찾기
        parent = title_tag.find_parent(_is_section_tag)
        if parent is not None:
            target_text = parent.get_text(separator="\n", strip=True)
        else:
            # 부모 SECTION 없으면, title_tag의 부모를 기준으로 형제 노드 수집,
            # 다음 SECTION 만나면 중단
            container = title_tag.parent or title_tag
            collected: list[str] = [title_tag.get_text(separator="\n", strip=True)]
            sibling = title_tag
            total = sum(len(s) for s in collected)
            while sibling.next_sibling and total < max_chars:
                sibling = sibling.next_sibling
                if _is_section_tag(sibling):
                    break
                txt = (
                    sibling.get_text(separator="\n", strip=True)
                    if hasattr(sibling, "get_text")
                    else str(sibling).strip()
                )
                if txt:
                    collected.append(txt)
                    total += len(txt)
            target_text = "\n".join(collected)

    if not target_text:
        # 섹션 못 찾으면 전체 텍스트
        target_text = soup.get_text(separator="\n", strip=True)
        log.info("'%s' 섹션을 찾지 못함 → 전체 본문 사용", prefer_section)
    else:
        log.info("'%s' 섹션 추출 성공 (%d자)", prefer_section, len(target_text))

    return target_text[:max_chars]
