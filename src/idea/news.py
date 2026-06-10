"""종목별 최근 30일 뉴스·공시·실적 fetch — perplexity sonar.

wisereport 분석가 리포트는 회장 발언·capex 가이던스 같은 이벤트성 뉴스가
**리포트화되기 전 1~4주 갭** 동안 못 잡힘. 그 사이 공백을 메우려고
top10 종목별로 perplexity 라이브 웹검색을 1회씩 호출해 최근 N일 뉴스를 수집.

원칙:
  - 모델 prior로 추측 금지 — 반드시 web search로 직접 발견.
  - 30일 이내 자료만 (오늘 기준).
  - 실패·credit 부족·sonar 미가용 → 빈 리스트 (graceful).
  - 10 종목 병렬 (Semaphore=5 — 동시 5개로 perplexity rate limit 대비).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.llm_json import parse_json_object

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 모델: news는 무거운 추론 불필요 — sonar(저렴) 1순위, sonar-pro 폴백
_NEWS_MODEL_ENV = "IDEA_NEWS_MODEL"
_DEFAULT_NEWS_MODEL = "perplexity/sonar"
_FALLBACK_NEWS_MODEL = "perplexity/sonar-pro"

# 동시 호출 상한 — perplexity rate limit 회피
_CONCURRENCY = int(os.getenv("IDEA_NEWS_CONCURRENCY", "5"))

# 1 종목당 max_tokens — JSON 5건이면 충분
_NEWS_MAX_TOKENS = 1500


_SYSTEM_PROMPT = (
    "당신은 한국 주식 종목 뉴스 리서치 어시스턴트입니다.\n"
    "특정 종목의 **최근 N일 이내** 뉴스·공시·실적·회장발언·M&A·공급계약·capex "
    "가이던스를 web search로 찾아 JSON으로 출력합니다.\n\n"
    "# 절대 원칙\n"
    "- 사용자 메시지의 `현재 기준일`이 absolute anchor. 그 날짜 - N일 이내 자료만.\n"
    "- 본인 LLM training prior로 추측 절대 금지 — 반드시 web search 출처(URL/매체명).\n"
    "- 발견 자료가 없으면 `\"news\": []`로 빈 배열 반환.\n"
    "- 광고성·전망 기사 X, 실제 발생 이벤트만.\n\n"
    "# 출력 형식 — JSON만\n"
    "```json\n"
    "{\n"
    '  "news": [\n'
    '    {\n'
    '      "date": "YYYY-MM-DD",\n'
    '      "headline": "한 줄 요약",\n'
    '      "source": "매체명 또는 URL",\n'
    '      "impact_hint": "사용자 thesis 관점 1줄 시사점 (수혜 가속/감속/중립)"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "```\n"
    "최대 5건. 가장 영향력 큰 순서.\n"
)


def _today_anchor() -> str:
    now = datetime.now(KST)
    return f"**현재 기준일: {now.strftime('%Y년 %m월 %d일')} KST**\n\n"


def _build_user_msg(name: str, ticker6: str, days: int) -> str:
    return (
        _today_anchor()
        + f"종목: {name} ({ticker6})\n"
        f"기간: 현재 기준일으로부터 **최근 {days}일 이내** 자료만.\n\n"
        "위 종목의 최근 뉴스·공시·실적·회장발언·capex/M&A를 web search로 찾아 "
        "시스템 프롬프트 형식대로 JSON 출력."
    )


def _normalize_news_item(raw: Any) -> dict | None:
    """LLM 응답 1건을 안전 dict로. 필수 필드 없으면 None."""
    if not isinstance(raw, dict):
        return None
    headline = (raw.get("headline") or "").strip()
    if not headline:
        return None
    date_s = (raw.get("date") or "").strip()
    # YYYY-MM-DD 또는 YYYY.MM.DD 형식만 받음
    if date_s and not re.match(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$", date_s):
        date_s = ""
    return {
        "date": date_s,
        "headline": headline[:200],
        "source": (raw.get("source") or "").strip()[:200],
        "impact_hint": (raw.get("impact_hint") or "").strip()[:200],
    }


def _filter_by_age(items: list[dict], days: int) -> list[dict]:
    """date가 today - days보다 오래된 항목 제거. date 비어있으면 통과(LLM 응답 신뢰)."""
    cutoff = (datetime.now(KST) - timedelta(days=days)).date()
    out = []
    for it in items:
        d = it.get("date") or ""
        if not d:
            out.append(it)
            continue
        try:
            d_norm = re.sub(r"[./]", "-", d)
            parsed = datetime.strptime(d_norm, "%Y-%m-%d").date()
        except ValueError:
            out.append(it)
            continue
        if parsed >= cutoff:
            out.append(it)
    return out


def _fetch_one_sync(name: str, ticker6: str, days: int) -> list[dict]:
    """단일 종목 perplexity 호출 (blocking). 실패 시 빈 리스트."""
    try:
        from src import summarizer
        client = summarizer.get_client()
    except Exception:
        log.info("[news] OpenRouter client 미가용 — %s skip", name)
        return []

    model = os.getenv(_NEWS_MODEL_ENV) or _DEFAULT_NEWS_MODEL
    user_msg = _build_user_msg(name, ticker6, days)
    content = ""
    for attempt, m in enumerate((model, _FALLBACK_NEWS_MODEL), start=1):
        try:
            resp = client.chat.completions.create(
                model=m,
                max_tokens=_NEWS_MAX_TOKENS,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            content = (resp.choices[0].message.content or "").strip()
            if content:
                break
        except Exception as e:
            try:
                from src import summarizer as _sm
                if _sm._is_credit_error(e):
                    log.warning("[news] credit 부족 — %s 즉시 빈 응답", name)
                    return []
            except Exception:
                pass
            log.info("[news] %s 호출 실패 (attempt %d, model=%s): %s", name, attempt, m, e)
            content = ""
    if not content:
        return []
    obj = parse_json_object(content) or {}
    raw_list = obj.get("news") or []
    if not isinstance(raw_list, list):
        return []
    items = [x for x in (_normalize_news_item(r) for r in raw_list) if x]
    items = _filter_by_age(items, days)
    return items[:5]


async def fetch_recent_news(name: str, ticker6: str, days: int = 30) -> list[dict]:
    """단일 종목 최근 N일 뉴스 (async wrapper)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_one_sync, name, ticker6, days)


async def fetch_recent_news_batch(
    top: list[dict], days: int = 30,
) -> dict[str, list[dict]]:
    """top10 종목 병렬 fetch. Semaphore로 동시 호출 제한.

    반환: {ticker6: [news items]}. ticker6 빈 종목은 fetch skip — 빈 결과.
    """
    sem = asyncio.Semaphore(max(1, _CONCURRENCY))

    async def _one(c: dict) -> tuple[str, list[dict]]:
        ticker = (c.get("ticker6") or "").strip()
        name = (c.get("name") or "").strip()
        if not name or not re.match(r"^\d{6}$", ticker):
            return ticker, []
        async with sem:
            try:
                items = await fetch_recent_news(name, ticker, days=days)
            except Exception:
                log.exception("[news] %s fetch 예외", name)
                items = []
        return ticker, items

    results = await asyncio.gather(*[_one(c) for c in top], return_exceptions=False)
    out: dict[str, list[dict]] = {}
    for ticker, items in results:
        if ticker:
            out[ticker] = items
    total = sum(len(v) for v in out.values())
    hit_tickers = sum(1 for v in out.values() if v)
    log.info(
        "[news] 종목별 최근 %d일 뉴스 수집 완료 — 총 %d건, 신규 hit %d/%d종목",
        days, total, hit_tickers, len(top),
    )
    return out
