"""티커별 생태계 4-bucket 매핑 — customers / inputs / peers / enablers.

각 미국 ticker가 누구를 통해 매출을 일으키고, 누구로부터 부품·서비스를 사고, 누구와 직접 경쟁하고,
어떤 인프라(전력·장비·금융)에 노출되는지를 4 그룹으로 정리한 JSON. synthesis가 §3.5 "생태계
정합성"을 쓸 때 강제 근거로 사용.

스토리지:
  {STATE_ROOT}/earnings_ecosystem/{TICKER}.json
  payload: {
    "ticker": "DELL", "generated_at": ISO, "source": "perplexity|manual",
    "customers": ["MSFT","AMZN","GOOG","META", ...],
    "inputs":    ["NVDA","AMD","AVGO","MU", ...],
    "peers":     ["SMCI","HPE","LNVGY","CSCO", ...],
    "enablers":  ["VRT","ETN","GEV", ...],
    "notes": "1-2 sentences on the company's positioning in this ecosystem"
  }

첫 호출 시 perplexity로 자동 시드(LLM이 ticker만 받아 4 그룹 JSON 생성).
사용자 갱신: history와 동일하게 STATE_DIR 변경 또는 파일 직접 수정.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_LOCK = threading.RLock()


# ------------------------------------------------------------------
# 경로
# ------------------------------------------------------------------
def _root() -> Path:
    for env_key in ("RAILWAY_VOLUME_MOUNT_PATH", "STATE_DIR"):
        v = os.environ.get(env_key)
        if v:
            p = Path(v) / "earnings_ecosystem"
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                continue
    for candidate in ("/data", "/app/data", "/tmp"):
        try:
            p = Path(candidate) / "earnings_ecosystem"
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return Path("/tmp")


def _path(ticker: str) -> Path:
    return _root() / f"{ticker.upper().strip()}.json"


# ------------------------------------------------------------------
# IO
# ------------------------------------------------------------------
def load(ticker: str) -> dict | None:
    p = _path(ticker)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.exception("[earnings/ecosystem] load 실패: %s", p)
        return None


def save(ecosystem: dict) -> Path | None:
    t = (ecosystem or {}).get("ticker", "").upper().strip()
    if not t:
        return None
    p = _path(t)
    try:
        with _LOCK:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(ecosystem, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
            log.info("[earnings/ecosystem] saved %s", p)
            return p
    except Exception:
        log.exception("[earnings/ecosystem] save 실패: %s", p)
        return None


# ------------------------------------------------------------------
# LLM 자동 시드 (perplexity)
# ------------------------------------------------------------------
def _generate_via_llm(ticker: str) -> dict | None:
    """perplexity sonar로 4-bucket 자동 생성. 실패 시 None."""
    try:
        from src import summarizer
    except Exception:
        return None
    client = summarizer.get_client()
    model = (
        os.getenv("IDEA_RESEARCH_MODEL")
        or os.getenv("EARNINGS_RESEARCH_MODEL")
        or "perplexity/sonar-pro"
    )
    system = (
        "You map US-listed company ecosystems. Given a ticker, return ONLY a JSON with 4 buckets of "
        "the most relevant US-listed tickers (avoid foreign tickers, private companies, ETFs). "
        "Prioritize businesses where there is a clear, named commercial relationship. No prose."
    )
    user_msg = (
        f"Ticker: {ticker}\n\n"
        "Return JSON in this exact shape (use up to 8 tickers per bucket, fewer is fine. "
        "Use null/empty array if a bucket is not applicable):\n"
        "{\n"
        '  "customers": ["TICKER", ...],   // who pays this company (downstream)\n'
        '  "inputs": ["TICKER", ...],      // who this company pays for components/services (upstream)\n'
        '  "peers": ["TICKER", ...],       // direct competitors in the same product/market\n'
        '  "enablers": ["TICKER", ...],    // adjacent infra (power, equipment, finance, real estate) materially exposed\n'
        '  "notes": "1-2 sentences on the company\'s positioning in this ecosystem"\n'
        "}"
    )
    try:
        content = summarizer.chat_with_retry(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=900,
            context="earnings-ecosystem-seed",
        )
    except Exception as e:
        log.warning("ecosystem 자동 시드 실패 %s: %s", ticker, e)
        return None
    if not content:
        return None
    try:
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            return None
        obj = json.loads(m.group(0))
    except Exception:
        return None

    out = {
        "ticker": ticker.upper().strip(),
        "generated_at": datetime.now(KST).isoformat(),
        "source": "perplexity",
        "customers": _clean_tickers(obj.get("customers")),
        "inputs": _clean_tickers(obj.get("inputs")),
        "peers": _clean_tickers(obj.get("peers")),
        "enablers": _clean_tickers(obj.get("enablers")),
        "notes": (obj.get("notes") or "").strip(),
    }
    return out


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _clean_tickers(arr: Any) -> list[str]:
    if not isinstance(arr, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in arr:
        if not isinstance(x, str):
            continue
        t = x.strip().upper()
        if not t or t in seen:
            continue
        if not _TICKER_RE.match(t):
            continue
        seen.add(t)
        out.append(t)
    return out[:8]


# ------------------------------------------------------------------
# 공개 API
# ------------------------------------------------------------------
def get_ecosystem(ticker: str, auto_seed: bool = True) -> dict | None:
    """티커의 4-bucket 맵. 캐시 hit → 그대로 반환. miss + auto_seed → perplexity 시드 후 저장."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return None
    cached = load(ticker)
    if cached:
        return cached
    if not auto_seed:
        return None
    gen = _generate_via_llm(ticker)
    if gen:
        save(gen)
        return gen
    return None


def ecosystem_payload_block(ticker: str, eco: dict, history_excerpt: dict | None = None) -> str:
    """synthesis LLM에 넘길 단일 블록 (영문). history_excerpt: {related_ticker: latest_extract dict}."""
    lines: list[str] = []
    lines.append(f"### {ticker} ecosystem map (source: {eco.get('source','?')})")
    lines.append(f"customers: {', '.join(eco.get('customers') or []) or '(none)'}")
    lines.append(f"inputs:    {', '.join(eco.get('inputs') or []) or '(none)'}")
    lines.append(f"peers:     {', '.join(eco.get('peers') or []) or '(none)'}")
    lines.append(f"enablers:  {', '.join(eco.get('enablers') or []) or '(none)'}")
    note = (eco.get("notes") or "").strip()
    if note:
        lines.append(f"positioning_note: {note}")
    if history_excerpt:
        lines.append("recent ecosystem-member calls (from earnings_history cache):")
        for rt, excerpt in history_excerpt.items():
            label = excerpt.get("fiscal_label") or "?"
            tone = (excerpt.get("management_tone") or "").strip()
            gd = excerpt.get("guidance") or {}
            gd_short = ", ".join(f"{k}={v}" for k, v in list(gd.items())[:3] if v)
            lines.append(f"  - {rt} [{label}] tone='{tone[:80]}' guidance: {gd_short}")
    return "\n".join(lines)


def fmt_ecosystem_text(ticker: str, eco: dict) -> str:
    """텔레그램용 한국어 요약."""
    lines: list[str] = []
    lines.append(f"🌐 {ticker} 생태계 맵 (출처 {eco.get('source','?')})")
    for bucket, label in (("customers", "고객사"), ("inputs", "공급사"), ("peers", "경쟁사"), ("enablers", "인프라")):
        ts = eco.get(bucket) or []
        if ts:
            lines.append(f"  · {label}: {', '.join(ts)}")
    note = (eco.get("notes") or "").strip()
    if note:
        lines.append(f"  · 노트: {note}")
    return "\n".join(lines)
