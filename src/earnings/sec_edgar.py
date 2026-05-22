"""SEC EDGAR company facts API — 미국 기업 재무 데이터 fetch.

엔드포인트: https://data.sec.gov/api/xbrl/companyfacts/CIK{10자리}.json
키 불필요, rate limit ≤ 10 req/sec, User-Agent 헤더 필수.

추출 지표 (XBRL US-GAAP 태그):
  - CapEx: PaymentsToAcquirePropertyPlantAndEquipment
  - OCF:   NetCashProvidedByUsedInOperatingActivities
  - FCF:   OCF - CapEx (derived)
  - Revenue: Revenues 또는 RevenueFromContractWithCustomerExcludingAssessedTax

연간 fiscal year 단위(FY=10-K)로 6년치 + 최신 분기 추출.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

EDGAR_BASE = "https://data.sec.gov"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# SEC 정책: User-Agent 필수 (이름 + 이메일). env로 덮어쓰기 가능.
USER_AGENT = os.getenv(
    "SEC_EDGAR_USER_AGENT",
    "earnings-call-bot research@example.com",
)

# 추출할 XBRL 태그 (우선순위 순)
CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
OCF_TAGS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
REV_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
)
RND_TAGS = ("ResearchAndDevelopmentExpense",)
NI_TAGS = ("NetIncomeLoss",)


# ------------------------------------------------------------------
# Rate limit (전역, 모든 스레드 공유 — SEC 10 req/sec 안전 마진)
# ------------------------------------------------------------------
_RATE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
MIN_INTERVAL_S = 0.15  # ≈ 6.6 req/sec


def _throttle() -> None:
    with _RATE_LOCK:
        elapsed = time.monotonic() - _LAST_CALL[0]
        if elapsed < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - elapsed)
        _LAST_CALL[0] = time.monotonic()


# ------------------------------------------------------------------
# Ticker → CIK 매핑 (메모리 캐시)
# ------------------------------------------------------------------
_TICKER_CACHE: dict[str, str] = {}
_TICKER_CACHE_LOCK = threading.Lock()


def _load_tickers() -> dict[str, str]:
    """{ticker_upper: cik_zero_padded_10}. 한 번만 load."""
    with _TICKER_CACHE_LOCK:
        if _TICKER_CACHE:
            return _TICKER_CACHE
        _throttle()
        try:
            r = httpx.get(
                SEC_TICKERS_URL,
                timeout=DEFAULT_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            log.exception("SEC tickers 다운로드 실패")
            return {}
        for entry in data.values():
            try:
                ticker = (entry.get("ticker") or "").upper()
                cik = str(entry.get("cik_str") or entry.get("cik") or "").zfill(10)
                if ticker and cik.strip("0"):
                    _TICKER_CACHE[ticker] = cik
            except Exception:
                continue
        log.info("SEC tickers 로드 완료: %d개", len(_TICKER_CACHE))
        return _TICKER_CACHE


def ticker_to_cik(ticker: str) -> str | None:
    """티커 → 10자리 zero-padded CIK. 미발견 시 None."""
    if not ticker:
        return None
    return _load_tickers().get(ticker.upper())


# ------------------------------------------------------------------
# Company facts fetch
# ------------------------------------------------------------------
@dataclass
class AnnualPoint:
    fy: int           # fiscal year (e.g. 2024)
    end: str          # period end date YYYY-MM-DD
    val: float        # USD


@dataclass
class QuarterPoint:
    fy: int           # fiscal year
    fp: str           # fiscal period: "Q1".."Q4"
    end: str          # period end date YYYY-MM-DD
    val: float        # USD (single-quarter flow)


@dataclass
class CompanyFinancials:
    ticker: str
    cik: str
    company_name: str = ""
    capex: list[AnnualPoint] = field(default_factory=list)   # ≤6 most recent FY
    ocf: list[AnnualPoint] = field(default_factory=list)
    revenue: list[AnnualPoint] = field(default_factory=list)
    rnd: list[AnnualPoint] = field(default_factory=list)
    ni: list[AnnualPoint] = field(default_factory=list)
    # 분기(단일분기 flow) — 검증 단계용
    q_revenue: list[QuarterPoint] = field(default_factory=list)
    q_capex: list[QuarterPoint] = field(default_factory=list)

    def find_quarter(self, points: list[QuarterPoint], year: int, quarter: int) -> QuarterPoint | None:
        for p in points:
            if p.fy == year and p.fp == f"Q{quarter}":
                return p
        return None

    def fcf(self) -> list[AnnualPoint]:
        """FCF = OCF - CapEx (같은 FY로 매칭). CapEx 부호는 SEC가 양수(지출)로 기록."""
        out: list[AnnualPoint] = []
        capex_by_fy = {p.fy: p for p in self.capex}
        for ocf_p in self.ocf:
            cap = capex_by_fy.get(ocf_p.fy)
            if cap is None:
                continue
            out.append(AnnualPoint(fy=ocf_p.fy, end=ocf_p.end, val=ocf_p.val - cap.val))
        out.sort(key=lambda p: p.fy)
        return out

    def ocf_capex_ratio(self) -> list[AnnualPoint]:
        """OCF / CapEx 비율 (1보다 크면 흑자 흐름)."""
        out: list[AnnualPoint] = []
        capex_by_fy = {p.fy: p for p in self.capex}
        for ocf_p in self.ocf:
            cap = capex_by_fy.get(ocf_p.fy)
            if cap is None or cap.val <= 0:
                continue
            out.append(AnnualPoint(fy=ocf_p.fy, end=ocf_p.end, val=ocf_p.val / cap.val))
        out.sort(key=lambda p: p.fy)
        return out

    def capex_yoy_change(self) -> list[AnnualPoint]:
        """CapEx YoY 증감률 (%). 첫 해는 skip."""
        sorted_pts = sorted(self.capex, key=lambda p: p.fy)
        out: list[AnnualPoint] = []
        for i in range(1, len(sorted_pts)):
            prev = sorted_pts[i - 1].val
            cur = sorted_pts[i].val
            if prev <= 0:
                continue
            pct = (cur - prev) / prev * 100.0
            out.append(AnnualPoint(fy=sorted_pts[i].fy, end=sorted_pts[i].end, val=pct))
        return out


def _extract_annual_units(facts: dict, tags: tuple[str, ...], years: int = 6) -> list[AnnualPoint]:
    """us-gaap facts에서 주어진 태그 중 첫 매칭 → FY 10-K 데이터 추출.

    SEC company facts: facts['us-gaap'][tag]['units']['USD'] = [
        {'start': ..., 'end': ..., 'val': ..., 'fy': ..., 'fp': 'FY', 'form': '10-K', ...}, ...
    ]
    """
    usgaap = (facts.get("us-gaap") or {})
    for tag in tags:
        node = usgaap.get(tag)
        if not node:
            continue
        units = node.get("units") or {}
        usd = units.get("USD") or []
        if not usd:
            continue
        # FY + 10-K 만 (분기 데이터 제외)
        annuals: dict[int, dict] = {}
        for entry in usd:
            try:
                fp = entry.get("fp")
                form = entry.get("form", "")
                fy = int(entry.get("fy") or 0)
                if fy <= 0:
                    continue
                if fp != "FY":
                    continue
                if not (form.startswith("10-K") or form == "20-F"):
                    continue
                # 같은 FY 여러 출처 — accn 최신 우선 (사후 amendments)
                cur = annuals.get(fy)
                if cur is None or (entry.get("filed") or "") > (cur.get("filed") or ""):
                    annuals[fy] = entry
            except Exception:
                continue
        if not annuals:
            continue
        # 최근 N년만
        sorted_fys = sorted(annuals.keys(), reverse=True)[:years]
        out: list[AnnualPoint] = []
        for fy in sorted_fys:
            entry = annuals[fy]
            try:
                out.append(
                    AnnualPoint(
                        fy=fy,
                        end=str(entry.get("end") or ""),
                        val=float(entry.get("val") or 0),
                    )
                )
            except Exception:
                continue
        out.sort(key=lambda p: p.fy)
        return out
    return []


def _days_between(start: str, end: str) -> int:
    """ISO date 차이 (일). 파싱 실패 시 -1."""
    from datetime import date
    try:
        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        return (date(ey, em, ed) - date(sy, sm, sd)).days
    except Exception:
        return -1


def _extract_quarterly_units(facts: dict, tags: tuple[str, ...], quarters: int = 8) -> list[QuarterPoint]:
    """단일분기 flow 데이터 추출 (검증용). YTD/누적은 제외 (기간 ~90일만).

    revenue/capex 같은 flow 항목은 10-Q에 YTD(누적)와 단일분기가 섞여 들어옴.
    start~end가 80~100일인 항목만 단일분기로 채택.
    """
    usgaap = (facts.get("us-gaap") or {})
    for tag in tags:
        node = usgaap.get(tag)
        if not node:
            continue
        usd = (node.get("units") or {}).get("USD") or []
        if not usd:
            continue
        picked: dict[tuple[int, str], dict] = {}
        for entry in usd:
            try:
                fp = entry.get("fp") or ""
                fy = int(entry.get("fy") or 0)
                start = str(entry.get("start") or "")
                end = str(entry.get("end") or "")
                if fy <= 0 or not fp.startswith("Q") or not start or not end:
                    continue
                dur = _days_between(start, end)
                if not (80 <= dur <= 100):  # 단일분기만
                    continue
                key = (fy, fp)
                cur = picked.get(key)
                if cur is None or (entry.get("filed") or "") > (cur.get("filed") or ""):
                    picked[key] = entry
            except Exception:
                continue
        if not picked:
            continue
        pts = [
            QuarterPoint(fy=fy, fp=fp, end=str(e.get("end") or ""), val=float(e.get("val") or 0))
            for (fy, fp), e in picked.items()
        ]
        pts.sort(key=lambda p: (p.fy, p.fp), reverse=True)
        return pts[:quarters]
    return []


def fetch_company_financials(ticker: str, years: int = 6) -> CompanyFinancials | None:
    """티커의 최근 N년 연간(FY) CapEx/OCF/Revenue/R&D/NI fetch.

    실패 시 None. 부분 실패는 partial CompanyFinancials 반환.
    """
    cik = ticker_to_cik(ticker)
    if not cik:
        log.warning("SEC: %s CIK 매핑 실패 (미국 상장사 아님 가능성)", ticker)
        return None
    url = f"{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    _throttle()
    try:
        r = httpx.get(
            url,
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if r.status_code == 404:
            log.warning("SEC: %s (CIK %s) facts 미존재", ticker, cik)
            return None
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.exception("SEC company facts fetch 실패: %s", ticker)
        return None

    facts = data.get("facts") or {}
    return CompanyFinancials(
        ticker=ticker.upper(),
        cik=cik,
        company_name=str(data.get("entityName") or ticker.upper()),
        capex=_extract_annual_units(facts, CAPEX_TAGS, years=years),
        ocf=_extract_annual_units(facts, OCF_TAGS, years=years),
        revenue=_extract_annual_units(facts, REV_TAGS, years=years),
        rnd=_extract_annual_units(facts, RND_TAGS, years=years),
        ni=_extract_annual_units(facts, NI_TAGS, years=years),
        q_revenue=_extract_quarterly_units(facts, REV_TAGS),
        q_capex=_extract_quarterly_units(facts, CAPEX_TAGS),
    )


# ------------------------------------------------------------------
# 헬퍼 — 화면 표시용 포맷
# ------------------------------------------------------------------
def fmt_usd(val: float) -> str:
    """USD를 단위 자동 ($1.2B, $456M)."""
    av = abs(val)
    if av >= 1e9:
        s = f"${val / 1e9:.2f}B"
    elif av >= 1e6:
        s = f"${val / 1e6:.1f}M"
    elif av >= 1e3:
        s = f"${val / 1e3:.1f}K"
    else:
        s = f"${val:.0f}"
    return s


def sanitize_tickers(raw: str) -> list[str]:
    """문자열에서 티커 후보 추출 (대문자 1-5자, 흔한 영단어 제외)."""
    if not raw:
        return []
    candidates = re.findall(r"\b[A-Z][A-Z\.]{0,4}\b", raw)
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out
