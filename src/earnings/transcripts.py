"""미국 기업 어닝콜 전문 fetch + 요약.

데이터 소스:
  - perplexity/sonar-pro (research tier, 웹검색) — 1차: 가장 최근 어닝콜 전문 검색·요약
  - 폴백: kimi (summary tier) — 입력된 URL 추출 텍스트만 요약

format:
  1) 어닝콜 메타데이터: ticker, 분기, 일자, 참석자
  2) Management Discussion (CEO/CFO 핵심 발언 5-10개 quote)
  3) Q&A 핵심 질문 5-7개 + 답변
  4) 가이던스 (다음 분기/연간)
  5) 주요 숫자 (revenue beat/miss, EPS, margin, capex guidance)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

RESEARCH_MODEL = os.getenv("IDEA_RESEARCH_MODEL", "perplexity/sonar-pro")
SUMMARY_MODEL_FALLBACK = os.getenv("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")


TRANSCRIPT_SYSTEM = """You are a senior US equity research analyst specializing in earnings call transcripts.

Given a US-listed company ticker and target fiscal quarter, find the company's most recent earnings call transcript matching the quarter (search web sources: company IR site, Seeking Alpha, Motley Fool, transcript aggregators, SEC 8-K filings).

Return a STRUCTURED JSON response. Output JSON only, no prose.

Schema:
{
  "ticker": "AAPL",
  "company_name": "Apple Inc.",
  "fiscal_period": "Q1 FY2026",  // as reported by the company
  "call_date": "2026-02-01",
  "found": true,
  "source_urls": ["https://...", "..."],
  "participants": {
    "company": ["Tim Cook - CEO", "Luca Maestri - CFO"],
    "analysts": ["Amit Daryanani - Evercore", "..."]
  },
  "headline_numbers": {
    "revenue_actual": "$124.3B",
    "revenue_yoy": "+4%",
    "revenue_consensus": "$121.7B",
    "eps_actual": "$2.40",
    "eps_consensus": "$2.35",
    "operating_margin": "30.5%",
    "fcf_quarter": "$28B",
    "buyback_quarter": "$25B"
  },
  "management_discussion": [
    {"speaker": "Tim Cook", "topic": "AI/Services", "quote": "verbatim or close-paraphrase 2-4 sentence quote"},
    ...  // 6-10 highlights covering: top line drivers, margin commentary, capex/AI spend, segments, geographies
  ],
  "guidance": {
    "next_quarter_revenue": "low double-digit growth",
    "fy_revenue": "...",
    "capex_fy": "$130B+ (raised from $120B)",
    "opex": "...",
    "tax_rate": "...",
    "fx_impact": "..."
  },
  "qa_highlights": [
    {"analyst": "Amit Daryanani", "question": "Can you elaborate on AI infrastructure capex...", "answer_summary": "Cook responded that capex is being raised again to support generative AI demand..."},
    ...  // 5-7 most consequential Q&A
  ],
  "surprises_and_risks": [
    "Capex guidance raised 25% — heaviest signal so far for AI build-out",
    "China revenue declined 8% YoY — first contraction in 3 quarters",
    ...  // 3-5 items flagged as market-moving or non-consensus
  ],
  "analyst_sentiment": "Mixed — beat on top line but capex hike spooked some PMs",
  "stock_reaction_postcall": "Up 3% in after-hours / Down 5% next day"
}

Rules:
- If no transcript found for the requested quarter, set "found": false and explain in "source_urls" what was searched.
- Use verbatim or close-paraphrase quotes — do NOT fabricate numbers.
- Quotes should reflect actual emphasis from the call (capex, AI, margin, guidance).
- Output JSON only. No markdown fences.
"""


CRITERIA_TO_TICKERS_SYSTEM = """You are a US equity research assistant. Given a user's natural-language criteria
(e.g. "big tech Q1 2026 earnings", "top 6 hyperscalers by net capex", "AI semiconductor megacaps that already reported"),
return a JSON list of US-listed company tickers that best match.

Schema:
{
  "interpreted_criteria": "Top 6 hyperscalers by net capex over the last 4 quarters",
  "target_fiscal_period": "Q1 FY2026",  // best guess from the user's wording; can be null
  "tickers": [
    {"ticker": "MSFT", "company_name": "Microsoft Corporation", "reason_for_inclusion": "Hyperscaler with largest LTM capex ($X)"},
    {"ticker": "AMZN", "company_name": "Amazon.com Inc.", "reason_for_inclusion": "AWS capex among top 2"},
    ...
  ],
  "exclusion_notes": "TSLA excluded — capex profile is auto-manufacturing, not data center"
}

Rules:
- Up to 8 tickers max. Prefer 4-6 for cleaner comparison.
- Use real, current US-listed tickers (use ADR for non-US e.g. TSM, BABA only if explicitly relevant).
- For "Korean" or other non-US criteria, return empty tickers and note that earnings call bot covers US only.
- Output JSON only. No prose.
"""


def fetch_transcript(client, ticker: str, fiscal_period: str | None) -> dict[str, Any] | None:
    """단일 종목 어닝콜 전문 fetch (perplexity 웹검색).

    fiscal_period: "Q1 FY2026", "Q4 CY2025" 등 자연어. None이면 "the most recent reported quarter".
    실패 시 None.
    """
    target = fiscal_period or "the most recent reported quarter"
    user_msg = (
        f"Find {ticker}'s earnings call transcript for {target}. "
        f"Return the structured JSON with all sections filled. "
        f"If no transcript matches that period, return the latest available with 'found': true."
    )
    try:
        resp = client.chat.completions.create(
            model=RESEARCH_MODEL,
            messages=[
                {"role": "system", "content": TRANSCRIPT_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=6000,
        )
    except Exception:
        log.exception("transcript fetch 실패 (%s)", ticker)
        return None

    content = ""
    try:
        content = (resp.choices[0].message.content or "").strip()
    except (IndexError, AttributeError):
        log.warning("transcript LLM 응답 구조 비정상 (%s)", ticker)
        return None

    if not content:
        log.warning("transcript LLM 빈 응답 (%s)", ticker)
        return None

    from src.idea_bot import _parse_json  # 재사용: tolerant JSON parser
    parsed = _parse_json(content)
    if parsed is None:
        log.warning("transcript JSON 파싱 실패 (%s) — raw head: %r", ticker, content[:300])
        return None
    parsed.setdefault("ticker", ticker.upper())
    return parsed


def expand_criteria_to_tickers(client, criteria: str) -> dict[str, Any] | None:
    """자연어 조건 → 미국 티커 리스트. 실패 시 None."""
    if not criteria:
        return None
    try:
        resp = client.chat.completions.create(
            model=RESEARCH_MODEL,
            messages=[
                {"role": "system", "content": CRITERIA_TO_TICKERS_SYSTEM},
                {"role": "user", "content": criteria},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
    except Exception:
        log.exception("criteria→tickers 호출 실패")
        return None
    content = ""
    try:
        content = (resp.choices[0].message.content or "").strip()
    except (IndexError, AttributeError):
        return None
    if not content:
        return None
    from src.idea_bot import _parse_json
    return _parse_json(content)


# ------------------------------------------------------------------
# 텔레그램 요약용 (transcript dict → 사람이 읽기 쉬운 메시지)
# ------------------------------------------------------------------
def format_transcript_text(tr: dict[str, Any]) -> str:
    """transcript JSON → 텔레그램 발송용 한국어 마크다운.

    너무 길지 않게 4000자 안쪽 1청크에 들어가는 것을 목표 (긴 경우는 send_text_chunked가 알아서 분할).
    """
    if not tr:
        return "어닝콜 데이터를 가져오지 못했습니다."

    if not tr.get("found", True):
        return (
            f"⚠️ {tr.get('ticker', '?')} — 어닝콜 전문을 찾지 못함\n"
            f"검색 시도: {tr.get('source_urls', [])}\n"
        )

    lines: list[str] = []
    ticker = tr.get("ticker", "?")
    name = tr.get("company_name", "")
    period = tr.get("fiscal_period", "?")
    call_date = tr.get("call_date", "?")

    lines.append(f"📞 {ticker} — {name}")
    lines.append(f"  {period} · 콜 일자 {call_date}")
    lines.append("")

    hn = tr.get("headline_numbers") or {}
    if hn:
        lines.append("📊 헤드라인 숫자")
        for k, v in hn.items():
            if v:
                lines.append(f"  · {k}: {v}")
        lines.append("")

    md = tr.get("management_discussion") or []
    if md:
        lines.append("🎤 경영진 발언 (highlights)")
        for item in md[:10]:
            sp = (item.get("speaker") or "?").strip()
            tp = (item.get("topic") or "").strip()
            qt = (item.get("quote") or "").strip()
            head = f"[{sp}" + (f" · {tp}" if tp else "") + "]"
            lines.append(f"  {head}")
            lines.append(f"  \"{qt}\"")
        lines.append("")

    g = tr.get("guidance") or {}
    if g:
        lines.append("🔮 가이던스")
        for k, v in g.items():
            if v:
                lines.append(f"  · {k}: {v}")
        lines.append("")

    qa = tr.get("qa_highlights") or []
    if qa:
        lines.append("❓ Q&A 핵심")
        for item in qa[:7]:
            a = (item.get("analyst") or "?").strip()
            q = (item.get("question") or "").strip()
            ans = (item.get("answer_summary") or "").strip()
            lines.append(f"  Q ({a}): {q}")
            lines.append(f"  A: {ans}")
        lines.append("")

    sr = tr.get("surprises_and_risks") or []
    if sr:
        lines.append("⚡ 서프라이즈 / 리스크")
        for item in sr:
            lines.append(f"  · {item}")
        lines.append("")

    sent = tr.get("analyst_sentiment")
    if sent:
        lines.append(f"💬 애널리스트 톤: {sent}")
    react = tr.get("stock_reaction_postcall")
    if react:
        lines.append(f"📈 주가 반응: {react}")

    return "\n".join(lines)
