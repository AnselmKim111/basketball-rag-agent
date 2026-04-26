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


# 사업보고서·반기·분기보고서 코드 (DART pblntf_detail_ty)
PERIODIC_REPORT_CODES = ("A001", "A002", "A003")  # 사업/반기/분기

# IR 관련 공시 코드 (B001=주요사항보고, I001=거래소공시 IR관련 - 환경별 변동 가능)
# 보수적으로 보고서명 텍스트 매칭도 함께 사용
IR_REPORT_CODES = ("I001",)


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
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            xml_content = zf.read("CORPCODE.xml").decode("utf-8")

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


def fetch_latest_ir_doc(corp_code: str) -> DartReport | None:
    """가장 최근 IR자료/실적발표/사업설명회 메타데이터."""
    today = datetime.now(KST).strftime("%Y%m%d")
    six_months_ago = (datetime.now(KST) - timedelta(days=200)).strftime("%Y%m%d")

    candidates: list[DartReport] = []

    # 1순위: pblntf_detail_ty=I001 (거래소 공시 - IR자료)
    for code in IR_REPORT_CODES:
        data = _get("list.json", {
            "corp_code": corp_code,
            "bgn_de": six_months_ago,
            "end_de": today,
            "pblntf_detail_ty": code,
            "page_count": 30,
        })
        if not data:
            continue
        for it in data.get("list") or []:
            nm = it.get("report_nm", "")
            if any(kw in nm for kw in ("IR", "실적발표", "기업설명회", "투자설명회", "컨퍼런스콜", "사업설명회")):
                candidates.append(DartReport(
                    rcept_no=it["rcept_no"], report_nm=nm,
                    rcept_dt=it["rcept_dt"], flr_nm=it.get("flr_nm", ""),
                ))

    # 2순위: 전체 공시 검색에서 IR 키워드 포함 보고서
    if not candidates:
        data = _get("list.json", {
            "corp_code": corp_code,
            "bgn_de": six_months_ago,
            "end_de": today,
            "page_count": 30,
        })
        if data:
            for it in data.get("list") or []:
                nm = it.get("report_nm", "")
                if any(kw in nm for kw in ("IR자료", "실적발표", "기업설명회", "투자설명회", "사업설명회")):
                    candidates.append(DartReport(
                        rcept_no=it["rcept_no"], report_nm=nm,
                        rcept_dt=it["rcept_dt"], flr_nm=it.get("flr_nm", ""),
                    ))

    if not candidates:
        log.info("IR 자료 없음: corp_code=%s", corp_code)
        return None

    candidates.sort(key=lambda x: x.rcept_dt, reverse=True)
    top = candidates[0]
    log.info("IR자료 발견: %s (%s)", top.report_nm, top.rcept_dt)
    return top


def download_report_archive(rcept_no: str, target_dir: Path) -> Path | None:
    """rcept_no → 보고서 첨부서류(zip) 다운로드 후 가장 큰 PDF/HWP/문서 파일 반환.

    DART의 document.xml endpoint는 여러 파일이 zip으로 묶여 옴.
    주요 본문 파일 1개 추출.
    """
    key = _api_key()
    if not key:
        return None
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), verify=False) as cli:
            r = cli.get(f"{DART_BASE}/document.xml", params={"crtfc_key": key, "rcept_no": rcept_no})
            r.raise_for_status()
            content_type = r.headers.get("content-type", "").lower()
            # XML 응답이면 status 필드 있음 → 에러
            if "xml" in content_type and b"<status>" in r.content[:200]:
                # 본문 zip이 아닌 에러 XML
                log.warning("document.xml 에러 응답 (rcept_no=%s): %s", rcept_no, r.content[:300])
                return None
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            # PDF가 있으면 최우선, 없으면 가장 큰 파일
            members = zf.infolist()
            if not members:
                return None
            pdf_members = [m for m in members if m.filename.lower().endswith(".pdf")]
            picked = max(pdf_members, key=lambda m: m.file_size) if pdf_members else max(members, key=lambda m: m.file_size)
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
