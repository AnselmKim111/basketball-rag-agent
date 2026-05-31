"""어닝콜 심층추출 + 조건→티커 확장.

품질 핵심 (딥리서치급):
  1) transcript_source.fetch_full_transcript 로 진짜 전문 텍스트 확보 (grounding)
  2) 그 전문 전체를 sonnet이 읽어 심층 구조화 노트 추출 (prompts/earnings_extract.txt)
     — 얕은 단일 perplexity 요약 대신 verbatim 인용 + 전체 Q&A + segment까지.

폴백: 전문 확보 실패 시 transcript_source가 perplexity 요약(grounded=False)을 주고,
deep extract는 그 위에서라도 동작 (보고서에 "비grounding" 경고 표기).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RESEARCH_MODEL = os.getenv("IDEA_RESEARCH_MODEL", "perplexity/sonar-pro")


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

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompt(name: str, default: str = "Output JSON only.") -> str:
    try:
        return (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()
    except Exception:
        log.warning("프롬프트 로드 실패: %s", name)
        return default


# ------------------------------------------------------------------
# 1) 조건 → 티커 확장 (research tier)
# ------------------------------------------------------------------
def expand_criteria_to_tickers(client, criteria: str) -> dict[str, Any] | None:
    """자연어 조건 → 미국 티커 리스트. 실패 시 None."""
    if not criteria:
        return None
    from src import summarizer
    from src.idea_bot import _parse_json
    try:
        content = summarizer.chat_with_retry(
            client,
            model=RESEARCH_MODEL,
            messages=[
                {"role": "system", "content": CRITERIA_TO_TICKERS_SYSTEM},
                {"role": "user", "content": criteria},
            ],
            temperature=0.3,
            max_tokens=2000,
            context="earnings-criteria",
        )
    except Exception:
        log.exception("criteria→tickers 호출 실패")
        return None
    if not content:
        return None
    return _parse_json(content)


# ------------------------------------------------------------------
# 2) grounded 전문 확보 → 3) 심층추출
# ------------------------------------------------------------------
def fetch_and_extract(
    client,
    ticker: str,
    year: int | None,
    quarter: int | None,
    *,
    extract_model: str,
    fiscal_label: str = "",
) -> dict[str, Any] | None:
    """진짜 전문 확보 후 sonnet으로 심층추출. 실패 시 None.

    반환 dict: earnings_extract 스키마 + grounding 메타
      (grounded, source, source_urls, transcript_chars).
    """
    from src import summarizer
    from src.idea_bot import _parse_json
    from src.earnings import transcript_source as ts

    doc = ts.fetch_full_transcript(client, ticker, year, quarter)
    if doc is None:
        return None

    system = _load_prompt("earnings_extract")
    user_msg = (
        f"Company ticker: {ticker.upper()}\n"
        f"Requested period: {fiscal_label or doc.fiscal_period or 'most recent'}\n"
        f"grounded (real transcript text): {doc.grounded}\n"
        f"source: {doc.source}\n\n"
        f"=== TRANSCRIPT TEXT START ===\n{doc.trimmed_text()}\n=== TRANSCRIPT TEXT END ==="
    )

    try:
        content = summarizer.chat_with_retry(
            client,
            model=extract_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=8000,
            context=f"earnings-extract:{ticker}",
        )
    except Exception:
        log.exception("심층추출 실패 (%s)", ticker)
        content = ""

    parsed = _parse_json(content) if content else None
    if parsed is None:
        # 추출 실패해도 최소한의 grounding 메타는 보존 — 합성 단계가 빈손이 안 되게
        log.warning("심층추출 JSON 파싱 실패 (%s) — 메타만 반환", ticker)
        parsed = {"ticker": ticker.upper(), "extract_failed": True}

    parsed.setdefault("ticker", ticker.upper())
    parsed.setdefault("fiscal_period", doc.fiscal_period or fiscal_label)
    parsed.setdefault("call_date", doc.call_date)
    # grounding 메타 주입 (합성·검증·PDF에서 사용)
    parsed["grounded"] = doc.grounded
    parsed["source"] = doc.source
    parsed["source_urls"] = doc.source_urls
    parsed["transcript_chars"] = len(doc.full_text)
    # Track K — call-internal NLP signals (정규식, LLM 0회)
    try:
        from src.earnings import nlp_signals
        parsed["_signals"] = nlp_signals.compute_signals(doc.full_text, parsed)
    except Exception:
        log.exception("[earnings/nlp_signals] compute_signals 실패 (%s)", ticker)
    # Phase 7B — 환각 게이트: extract의 verbatim 인용이 raw_text에 실재하는지 검증.
    # grounded transcript이고 충분히 길 때만(짧으면 검증 fragment가 우연 매치 불가).
    if doc.grounded and len(doc.full_text) >= 5000 and not parsed.get("extract_failed"):
        try:
            from src.earnings import quality_gate as qg
            gate = qg.verify_extract_verbatim(parsed, doc.full_text, threshold=0.30)
            parsed["_gate_extract"] = {
                "total": gate.total_checked,
                "violations": len(gate.violations),
                "ratio": gate.violation_ratio,
                "passed": gate.passed,
                "summary": gate.summary(),
            }
            if gate.violations:
                # 위반 항목을 in-place 제거 — 합성·텔레그램 보고서에 깨끗한 데이터만
                qg.strip_violating_items(parsed, gate.violations)
                log.info("[quality_gate/%s] extract 위반 %d건 제거 (ratio=%.1f%%)",
                         ticker, len(gate.violations), gate.violation_ratio*100)
        except Exception:
            log.exception("[quality_gate] extract 검증 실패 (%s)", ticker)
    return parsed


# ------------------------------------------------------------------
# 텔레그램 발송용 포맷
# ------------------------------------------------------------------
def format_transcript_text(tr: dict[str, Any]) -> str:
    """심층추출 dict → 텔레그램 발송용 한국어 텍스트 (긴 경우 send_text_chunked가 분할)."""
    if not tr:
        return "어닝콜 데이터를 가져오지 못했습니다."

    lines: list[str] = []
    ticker = tr.get("ticker", "?")
    name = tr.get("company_name", "")
    period = tr.get("fiscal_period", "?")
    call_date = tr.get("call_date", "?")
    grounded = tr.get("grounded", False)
    source = tr.get("source", "?")

    badge = "✅ 전문 기반" if grounded else "⚠️ 전문 비근거(요약)"
    lines.append(f"📞 {ticker} — {name}")
    lines.append(f"  {period} · 콜 {call_date} · {badge} · 출처 {source}")
    lines.append("")

    if tr.get("extract_failed"):
        lines.append("⚠️ 심층추출 실패 — 원문 확보됐으나 구조화 실패. 재시도 권장.")
        return "\n".join(lines)

    hn = tr.get("headline_numbers") or {}
    if hn:
        lines.append("📊 헤드라인")
        for k, v in hn.items():
            if v:
                lines.append(f"  · {k}: {v}")
        lines.append("")

    seg = tr.get("segment_performance") or []
    if seg:
        lines.append("🧩 세그먼트")
        for item in seg[:8]:
            s = (item.get("segment") or "?").strip()
            mtr = (item.get("metric") or "").strip()
            det = (item.get("detail") or "").strip()
            lines.append(f"  · {s}: {mtr} — {det}")
        lines.append("")

    md = tr.get("management_discussion") or []
    if md:
        lines.append("🎤 경영진 발언")
        for item in md[:12]:
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
        lines.append("❓ Q&A")
        for item in qa[:10]:
            a = (item.get("analyst") or "?").strip()
            q = (item.get("question") or "").strip()
            ans = (item.get("answer_summary") or "").strip()
            lines.append(f"  Q ({a}): {q}")
            lines.append(f"  A: {ans}")
        lines.append("")

    cap = tr.get("capital_allocation")
    if cap:
        lines.append(f"💰 자본배분: {cap}")
    tone = tr.get("management_tone")
    if tone:
        lines.append(f"🎚️ 톤: {tone}")

    sr = tr.get("surprises_and_risks") or []
    if sr:
        lines.append("")
        lines.append("⚡ 서프라이즈/리스크")
        for item in sr:
            lines.append(f"  · {item}")

    deals = tr.get("named_deals") or []
    if deals:
        lines.append("")
        lines.append("🤝 명명된 계약·파트너십")
        for d in deals:
            cp = (d.get("counterparty") or "?").strip()
            mag = (d.get("magnitude") or "—").strip()
            ten = (d.get("tenure") or "—").strip()
            stat = (d.get("status") or "?").strip()
            prior = d.get("prior_quarter_mentioned")
            prior_tag = " [신규]" if prior is False else (" [기존 연장]" if prior is True else "")
            lines.append(f"  · {cp} — {mag} · {ten} · {stat}{prior_tag}")
            qv = (d.get("verbatim") or "").strip()
            if qv:
                lines.append(f"    \"{qv}\"")

    sc = tr.get("supply_chain_signals") or []
    if sc:
        lines.append("")
        lines.append("⛓️ 공급망 시그널")
        for s in sc:
            vc = (s.get("vendor_or_component") or "?").strip()
            st = (s.get("signal_type") or "?").strip()
            qv = (s.get("verbatim") or "").strip()
            lines.append(f"  · {vc} — {st}")
            if qv:
                lines.append(f"    \"{qv}\"")

    cc = tr.get("competitor_callouts") or []
    if cc:
        lines.append("")
        lines.append("⚔️ 경쟁사 직접 언급")
        for c in cc:
            comp = (c.get("competitor") or "?").strip()
            dim = (c.get("dimension") or "?").strip()
            direction = (c.get("implied_direction") or "?").strip()
            qv = (c.get("verbatim") or "").strip()
            lines.append(f"  · {comp} ({dim}, {direction})")
            if qv:
                lines.append(f"    \"{qv}\"")

    mp = tr.get("macro_policy_mentions") or []
    if mp:
        lines.append("")
        lines.append("🌐 매크로·정책 멘션")
        for m in mp:
            th = (m.get("theme") or "?").strip()
            rg = (m.get("region") or "?").strip()
            di = (m.get("impact_direction") or "?").strip()
            qv = (m.get("verbatim") or "").strip()
            lines.append(f"  · {th} ({rg}, {di})")
            if qv:
                lines.append(f"    \"{qv}\"")

    sg = tr.get("segment_geo_mix") or []
    if sg:
        lines.append("")
        lines.append("📍 세그먼트·지역 mix")
        for item in sg:
            seg_r = (item.get("segment_or_region") or "?").strip()
            share = item.get("share_pct")
            yoy = item.get("yoy")
            call = (item.get("callout") or "").strip()
            bits = []
            if share: bits.append(f"share {share}")
            if yoy: bits.append(f"yoy {yoy}")
            tail = f" — {call}" if call else ""
            lines.append(f"  · {seg_r}: {', '.join(bits) or '—'}{tail}")

    nv = tr.get("notable_verbatim") or []
    if nv:
        lines.append("")
        lines.append("🗣️ 핵심 verbatim")
        for item in nv[:5]:
            lines.append(f"  \"{item}\"")

    return "\n".join(lines)
