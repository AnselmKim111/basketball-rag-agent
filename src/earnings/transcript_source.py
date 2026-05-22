"""진짜 어닝콜 전문(full transcript) 확보 — grounding 레이어.

품질의 핵심: LLM에게 "찾아서 요약" 시키지 않고, 실제 콜 전문 텍스트를 확보한 뒤
그 위에서 심층 분석한다. 환각 제거 + 딥리서치급 깊이의 전제.

프로바이더 체인 (순서대로 시도, 첫 성공 채택):
  1) Financial Modeling Prep (FMP_API_KEY)        — 1순위, 안정적 full text
  2) API Ninjas (API_NINJAS_KEY)                  — 대안 full text
  3) 웹 스크레이프 (perplexity가 URL → httpx+BS4) — 키 없을 때, fragile
  4) perplexity 요약 (현행)                        — 최후 폴백, grounded=False 명시

모든 함수 graceful — 실패 시 None 반환, 예외를 봇으로 전파하지 않음.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com"
API_NINJAS_URL = "https://api.api-ninjas.com/v1/earningstranscript"

DEFAULT_TIMEOUT = httpx.Timeout(40.0, connect=10.0)

# 전문 텍스트 상한 (모델 컨텍스트 보호). 심층추출 단계에서 이 길이까지 읽음.
MAX_TRANSCRIPT_CHARS = 60_000
# grounded 판정 최소 길이 — 이보다 짧으면 "요약"으로 간주
MIN_GROUNDED_CHARS = 3_000


@dataclass
class TranscriptDoc:
    ticker: str
    company_name: str = ""
    fiscal_period: str = ""
    year: int | None = None
    quarter: int | None = None
    call_date: str = ""
    full_text: str = ""
    participants: list[str] = field(default_factory=list)
    source: str = ""          # "FMP" | "API Ninjas" | "scrape:<host>" | "perplexity"
    source_urls: list[str] = field(default_factory=list)
    grounded: bool = False    # True = 실제 전문 텍스트 / False = 요약 (환각 위험)

    def trimmed_text(self) -> str:
        if len(self.full_text) <= MAX_TRANSCRIPT_CHARS:
            return self.full_text
        return self.full_text[:MAX_TRANSCRIPT_CHARS] + "\n\n...(전문 길이 초과로 절단)"


# ------------------------------------------------------------------
# fiscal_period 문자열 → (year, quarter)
# ------------------------------------------------------------------
def resolve_year_quarter(fiscal_period: str | None) -> tuple[int | None, int | None]:
    """'Q1 FY2026', 'Q1 CY2026', '1Q26', '2026 1분기' 등 → (2026, 1).

    분기/연도 못 찾으면 (None, None) — 호출자가 'most recent'로 처리.
    """
    if not fiscal_period:
        return (None, None)
    s = fiscal_period.strip()

    def _y(raw: str) -> int:
        v = int(raw)
        return v + 2000 if v < 100 else v

    # compact: "1Q26", "1Q2026", "2Q '25"
    m = re.search(r"\b([1-4])\s*Q\s*'?(\d{2,4})\b", s, re.IGNORECASE)
    if m:
        return (_y(m.group(2)), int(m.group(1)))
    # compact: "Q1 FY2026", "Q1 2026", "Q1'26", "Q4 CY2025"
    m = re.search(r"\bQ\s*([1-4])\s*(?:FY|CY)?\s*'?(\d{2,4})\b", s, re.IGNORECASE)
    if m:
        return (_y(m.group(2)), int(m.group(1)))

    # 분리형: 분기와 연도 따로
    q = None
    for pat in (r"Q\s*([1-4])", r"([1-4])\s*Q", r"([1-4])\s*분기"):
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            q = int(m.group(1))
            break
    y = None
    m = re.search(r"(20\d{2})", s)
    if m:
        y = int(m.group(1))
    return (y, q)


# ------------------------------------------------------------------
# 프로바이더 1: Financial Modeling Prep
# ------------------------------------------------------------------
def _fmp(ticker: str, year: int | None, quarter: int | None) -> TranscriptDoc | None:
    key = os.getenv("FMP_API_KEY")
    if not key:
        return None
    params: dict[str, Any] = {"apikey": key}
    if year:
        params["year"] = year
    if quarter:
        params["quarter"] = quarter
    url = f"{FMP_BASE}/api/v3/earning_call_transcript/{ticker.upper()}"
    try:
        r = httpx.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if r.status_code in (401, 403):
            log.warning("FMP 인증 실패 (%s) — 키 확인", r.status_code)
            return None
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.exception("FMP transcript fetch 실패 (%s)", ticker)
        return None

    if not isinstance(data, list) or not data:
        return None
    entry = data[0]
    content = (entry.get("content") or "").strip()
    if len(content) < MIN_GROUNDED_CHARS:
        return None
    return TranscriptDoc(
        ticker=ticker.upper(),
        fiscal_period=f"Q{entry.get('quarter')} {entry.get('year')}",
        year=int(entry.get("year")) if entry.get("year") else year,
        quarter=int(entry.get("quarter")) if entry.get("quarter") else quarter,
        call_date=str(entry.get("date") or "")[:10],
        full_text=content,
        source="FMP",
        grounded=True,
    )


# ------------------------------------------------------------------
# 프로바이더 2: API Ninjas
# ------------------------------------------------------------------
def _api_ninjas(ticker: str, year: int | None, quarter: int | None) -> TranscriptDoc | None:
    key = os.getenv("API_NINJAS_KEY")
    if not key or not year or not quarter:
        # API Ninjas는 year+quarter 필수
        return None
    try:
        r = httpx.get(
            API_NINJAS_URL,
            params={"ticker": ticker.upper(), "year": year, "quarter": quarter},
            headers={"X-Api-Key": key},
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code in (401, 403):
            log.warning("API Ninjas 인증 실패 (%s)", r.status_code)
            return None
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.exception("API Ninjas transcript fetch 실패 (%s)", ticker)
        return None

    content = (data.get("transcript") or "").strip() if isinstance(data, dict) else ""
    if len(content) < MIN_GROUNDED_CHARS:
        return None
    return TranscriptDoc(
        ticker=ticker.upper(),
        fiscal_period=f"Q{quarter} {year}",
        year=year,
        quarter=quarter,
        call_date=str(data.get("date") or "")[:10],
        full_text=content,
        source="API Ninjas",
        grounded=True,
    )


# ------------------------------------------------------------------
# 프로바이더 3: 웹 스크레이프 (perplexity가 URL 제공 → httpx+BS4 본문 추출)
# ------------------------------------------------------------------
def _find_source_urls(client, ticker: str, year: int | None, quarter: int | None) -> list[str]:
    """perplexity로 전문 페이지 URL 후보를 찾는다."""
    from src import summarizer
    period = f"Q{quarter} {year}" if (year and quarter) else "most recent quarter"
    prompt = (
        f"Find direct URLs to the FULL earnings call transcript for {ticker} ({period}). "
        f"Prefer fool.com (Motley Fool), seekingalpha.com, or the company IR page. "
        f"Return ONLY a JSON array of URL strings, most-likely-full-text first. No prose."
    )
    try:
        content = summarizer.chat_with_retry(
            client,
            model=os.getenv("IDEA_RESEARCH_MODEL", "perplexity/sonar-pro"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.1,
            context=f"transcript-url:{ticker}",
        )
    except Exception:
        log.exception("source URL 검색 실패 (%s)", ticker)
        return []
    urls = re.findall(r'https?://[^\s"\'\]]+', content or "")
    # 중복 제거, 흔한 비전문 도메인 후순위
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = u.rstrip(".,)")
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out[:5]


def _scrape_url(url: str) -> str:
    """단일 URL 본문 텍스트 추출 (httpx + BeautifulSoup)."""
    try:
        r = httpx.get(
            url,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                )
            },
        )
        r.raise_for_status()
        html = r.text
    except Exception:
        log.warning("scrape GET 실패: %s", url)
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        # 본문 후보: article > main > body
        node = soup.find("article") or soup.find("main") or soup.body or soup
        text = node.get_text(separator="\n", strip=True)
        # 과도한 빈 줄 정리
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text
    except Exception:
        log.exception("BS4 파싱 실패: %s", url)
        return ""


def _scrape(client, ticker: str, year: int | None, quarter: int | None) -> TranscriptDoc | None:
    urls = _find_source_urls(client, ticker, year, quarter)
    for u in urls:
        text = _scrape_url(u)
        if len(text) >= MIN_GROUNDED_CHARS and _looks_like_transcript(text):
            host = re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
            return TranscriptDoc(
                ticker=ticker.upper(),
                fiscal_period=f"Q{quarter} {year}" if (year and quarter) else "",
                year=year,
                quarter=quarter,
                full_text=text[:MAX_TRANSCRIPT_CHARS],
                source=f"scrape:{host}",
                source_urls=[u],
                grounded=True,
            )
    return None


def _looks_like_transcript(text: str) -> bool:
    """전문 페이지 휴리스틱 — 발언자 패턴/콜 키워드 존재 여부."""
    low = text.lower()
    signals = ("earnings call", "conference call", "q&a", "operator", "prepared remarks", "chief executive")
    hits = sum(1 for s in signals if s in low)
    return hits >= 2


# ------------------------------------------------------------------
# 프로바이더 4: perplexity 요약 (최후 폴백, grounded=False)
# ------------------------------------------------------------------
def _perplexity_summary(client, ticker: str, year: int | None, quarter: int | None) -> TranscriptDoc | None:
    from src import summarizer
    period = f"Q{quarter} FY{year}" if (year and quarter) else "the most recent reported quarter"
    prompt = (
        f"Provide the most detailed possible reconstruction of {ticker}'s {period} earnings call. "
        f"Include: management prepared remarks (CEO/CFO) with as much verbatim detail as available, "
        f"full guidance, all material analyst Q&A, headline numbers (revenue/EPS/margins/capex), "
        f"segment performance. Write it as a long structured document, attributing speakers. "
        f"Cite source URLs at the end. Be comprehensive — do not over-summarize."
    )
    try:
        content = summarizer.chat_with_retry(
            client,
            model=os.getenv("IDEA_RESEARCH_MODEL", "perplexity/sonar-pro"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000,
            temperature=0.2,
            context=f"transcript-fallback:{ticker}",
        )
    except Exception:
        log.exception("perplexity 요약 폴백 실패 (%s)", ticker)
        return None
    if not content or len(content) < 500:
        return None
    urls = re.findall(r'https?://[^\s"\'\]]+', content)
    return TranscriptDoc(
        ticker=ticker.upper(),
        fiscal_period=f"Q{quarter} FY{year}" if (year and quarter) else "",
        year=year,
        quarter=quarter,
        full_text=content,
        source="perplexity",
        source_urls=urls[:5],
        grounded=False,
    )


# ------------------------------------------------------------------
# 공개 진입점
# ------------------------------------------------------------------
def fetch_full_transcript(
    client, ticker: str, year: int | None, quarter: int | None
) -> TranscriptDoc | None:
    """프로바이더 체인을 순서대로 시도. 첫 성공 채택. 모두 실패 시 None.

    client: OpenRouter OpenAI client (스크레이프 URL 검색·perplexity 폴백용).
    """
    # 1) FMP
    doc = _fmp(ticker, year, quarter)
    if doc:
        log.info("[transcript] %s — FMP grounded (%d자)", ticker, len(doc.full_text))
        return doc
    # 2) API Ninjas
    doc = _api_ninjas(ticker, year, quarter)
    if doc:
        log.info("[transcript] %s — API Ninjas grounded (%d자)", ticker, len(doc.full_text))
        return doc
    # 3) 스크레이프
    try:
        doc = _scrape(client, ticker, year, quarter)
    except Exception:
        log.exception("scrape 단계 실패 (%s)", ticker)
        doc = None
    if doc:
        log.info("[transcript] %s — %s grounded (%d자)", ticker, doc.source, len(doc.full_text))
        return doc
    # 4) perplexity 요약 (비grounding)
    doc = _perplexity_summary(client, ticker, year, quarter)
    if doc:
        log.warning("[transcript] %s — perplexity 요약 폴백 (비grounding, %d자)", ticker, len(doc.full_text))
        return doc
    log.warning("[transcript] %s — 모든 프로바이더 실패", ticker)
    return None
