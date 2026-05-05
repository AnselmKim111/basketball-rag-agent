"""Naver Finance에서 한국 종목의 현재가·전일대비·YTD·시총을 가져오는 fetcher.

격리: httpx 한 번 호출당 timeout 5초. 실패 시 None — 호출자가 graceful 처리.
서비스: 공개 finance.naver.com 페이지의 종목 모듈 JSON (인증 불필요).

함수:
  - fetch_quote(ticker6) → dict | None
    {price, change_pct_today, ytd_pct, market_cap_eok}
  - fetch_quotes_batch(tickers) → dict[ticker, dict|None]
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
TIMEOUT = httpx.Timeout(5.0, connect=3.0)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def fetch_quote(ticker6: str) -> dict | None:
    """한 종목 quote — 현재가, YTD 수익률, 시총 등.

    Naver Finance의 종목 메인 페이지 HTML에서 핵심 수치 정규식 추출.
    YTD는 거래소 연초가 데이터를 별도 API로 fetch.

    실패 시 None.
    """
    if not re.match(r"^\d{6}$", ticker6):
        return None
    try:
        return _fetch_inner(ticker6)
    except Exception:
        log.exception("Naver quote fetch 실패: %s", ticker6)
        return None


def fetch_quotes_batch(tickers: Iterable[str]) -> dict[str, dict | None]:
    """여러 종목 quote 동시 fetch (sync, 짧은 timeout이라 ~5종목 ≤2초)."""
    out: dict[str, dict | None] = {}
    for t in tickers:
        out[t] = fetch_quote(t)
    return out


def _fetch_inner(ticker6: str) -> dict | None:
    url = f"https://finance.naver.com/item/main.naver?code={ticker6}"
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=TIMEOUT, headers=headers, follow_redirects=True) as cli:
        r = cli.get(url)
        if r.status_code != 200:
            log.info("Naver finance 응답 %s: %s", r.status_code, ticker6)
            return None
        html = r.text

    # ── 현재가 추출
    # <p class="no_today"><em ...><span class="blind">12,345</span></em></p>
    m_price = re.search(
        r'class="no_today"[^>]*>\s*<em[^>]*>\s*<span class="blind">([\d,]+)</span>',
        html,
    )
    if not m_price:
        # 코스닥 종목은 약간 구조 다름
        m_price = re.search(r'<span class="blind">([\d,]+)</span>', html)
    if not m_price:
        return None
    try:
        price = int(m_price.group(1).replace(",", ""))
    except ValueError:
        return None

    # ── 전일대비 등락률
    # <p class="no_exday"><em class="...">...<span class="blind">±X.XX</span></em></p>
    change_pct = None
    m_change = re.search(
        r'no_exday[^"]*"[^>]*>.*?<span[^>]*class="blind">([\d.]+)</span>\s*</em>\s*<em[^>]*>\s*<span[^>]*class="blind">([\d.]+)</span>',
        html, re.S,
    )
    if m_change:
        try:
            change_pct = float(m_change.group(2))
            # 부호 — 'no_exday' 영역에서 ico_minus / ico_plus 클래스로 판별
            ctx = html[m_change.start():m_change.end() + 200]
            if "ico_down" in ctx or "minus" in ctx or "ico_minus" in ctx:
                change_pct = -change_pct
        except ValueError:
            pass

    # ── 시총 (단위: 억원) — "시가총액" 라벨 다음 숫자 추출
    market_cap_eok = None
    m_cap = re.search(
        r'시가총액[^<]*</[^>]+>\s*<em[^>]*>([\d,]+)</em>\s*<span[^>]*>억원',
        html,
    )
    if not m_cap:
        # 조 단위
        m_cap = re.search(
            r'시가총액[^<]*</[^>]+>\s*<em[^>]*>([\d,]+)</em>\s*<span[^>]*>조원',
            html,
        )
        if m_cap:
            try:
                market_cap_eok = int(m_cap.group(1).replace(",", "")) * 10_000
            except ValueError:
                pass
    elif m_cap:
        try:
            market_cap_eok = int(m_cap.group(1).replace(",", ""))
        except ValueError:
            pass

    # ── YTD 수익률 — 별도 API로 연초가 fetch
    ytd_pct = _fetch_ytd_pct(ticker6, current_price=price)

    return {
        "price": price,
        "change_pct_today": change_pct,
        "ytd_pct": ytd_pct,
        "market_cap_eok": market_cap_eok,
    }


def _fetch_ytd_pct(ticker6: str, current_price: int) -> float | None:
    """YTD 수익률 — 올해 첫 거래일 종가 대비 % 변화.

    Naver Finance sise_day API 사용 (페이지네이션 형식).
    """
    try:
        year = datetime.now(KST).year
        # sise_day API — 일봉 데이터 (최근 → 과거 순)
        # 페이지 늘리면서 1월 1일~ 첫 거래일 찾기
        url = f"https://finance.naver.com/item/sise_day.naver?code={ticker6}"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://finance.naver.com/item/sise.naver?code={ticker6}",
        }
        # 매 페이지 10건. 보통 5월 첫주면 1년 약 90거래일 → 9~10페이지면 충분
        target_year_start = f"{year}.01.01"
        target_year_end = f"{year}.12.31"
        first_close: int | None = None
        with httpx.Client(timeout=TIMEOUT, headers=headers, follow_redirects=True) as cli:
            for page in range(1, 25):
                r = cli.get(f"{url}&page={page}")
                if r.status_code != 200:
                    break
                # tr 행마다 날짜 + 종가
                rows = re.findall(
                    r'<td[^>]*class="date"[^>]*>([\d.]+)</td>\s*<td[^>]*class="num"[^>]*>'
                    r'<span[^>]*>([\d,]+)</span>',
                    r.text,
                )
                if not rows:
                    break
                for date_str, close_str in rows:
                    if date_str < target_year_start:
                        # 이미 작년 진입 — 직전(가장 가까운 올해 첫 거래일)이 있어야 함
                        return _calc_pct(first_close, current_price)
                    if date_str <= target_year_end:
                        # 올해 거래일 — 시간 역순이라 매번 갱신해도 결국 첫 거래일이 마지막에 잡힘
                        try:
                            first_close = int(close_str.replace(",", ""))
                        except ValueError:
                            continue
                # 모든 페이지 다 봐도 작년 진입 안 함 (아직 행 더 있음)
        return _calc_pct(first_close, current_price)
    except Exception:
        log.exception("YTD fetch 실패: %s", ticker6)
        return None


def _calc_pct(base: int | None, current: int) -> float | None:
    if base is None or base <= 0:
        return None
    return round((current - base) / base * 100, 1)


def format_quote_brief(q: dict | None) -> str:
    """Top 5 헤더용 짧은 가격 문자열. q가 None이면 ''."""
    if not q:
        return ""
    parts: list[str] = []
    price = q.get("price")
    if price is not None:
        parts.append(f"₩{price:,}")
    today = q.get("change_pct_today")
    if today is not None:
        sign = "+" if today >= 0 else ""
        parts.append(f"{sign}{today:.1f}%(D)")
    ytd = q.get("ytd_pct")
    if ytd is not None:
        sign = "+" if ytd >= 0 else ""
        parts.append(f"{sign}{ytd:.1f}%(YTD)")
    cap = q.get("market_cap_eok")
    if cap is not None:
        if cap >= 10_000:
            parts.append(f"시총 {cap / 10_000:.1f}조")
        else:
            parts.append(f"시총 {cap:,}억")
    return " · ".join(parts)
