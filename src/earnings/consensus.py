"""Sell-side consensus fetch — Yahoo Finance quoteSummary 우선, perplexity 폴백.

PM-grade beat/miss/surprise 정량화를 위해 컨센서스(revenue/EPS 추정치, 추천 분포,
목표주가 분포)를 외부에서 수집한다. 자체 API 키 불필요.

소스 우선 순위:
1. Yahoo Finance quoteSummary (query2.finance.yahoo.com) — 키 없이 cookie+crumb dance.
2. perplexity 폴백 — `IDEA_RESEARCH_MODEL`로 자연어 쿼리해 정량 답변 정규화.

실패는 graceful — None 반환. 호출자가 verify·synthesis 단계에서 누락 처리.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

YAHOO_BASE = "https://query2.finance.yahoo.com"
YAHOO_COOKIE_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = f"{YAHOO_BASE}/v1/test/getcrumb"
YAHOO_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# 일반 브라우저 User-Agent — Yahoo가 bot UA 차단 (실측).
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# cookie/crumb를 프로세스 단위로 캐시 (Yahoo는 cookie 만료 약 1시간)
_AUTH_LOCK = threading.Lock()
_AUTH_CACHE: dict[str, Any] = {"cookies": None, "crumb": None, "ts": 0.0}
_AUTH_TTL_S = 55 * 60  # 55분 — 충분한 마진


# ------------------------------------------------------------------
# 데이터 클래스
# ------------------------------------------------------------------
@dataclass
class ConsensusSnapshot:
    """sell-side 컨센서스 + analyst sentiment 한 stamp.

    필드 단위는 모두 raw — usd/percent/count 그대로. 포맷팅은 호출자가."""
    ticker: str
    fetched_at: float
    source: str                                  # 'yahoo' | 'perplexity' | 'mixed'

    # earnings (current quarter / next quarter / current FY / next FY)
    eps_est_current_q: float | None = None
    eps_est_next_q: float | None = None
    eps_est_current_y: float | None = None
    revenue_est_current_q: float | None = None   # USD raw (e.g., 25_000_000_000)
    revenue_est_next_q: float | None = None
    revenue_est_current_y: float | None = None
    num_analysts: int | None = None

    # 분기 surprise 히스토리 (이전 4분기) — actual vs estimate
    earnings_history: list[dict[str, Any]] = field(default_factory=list)

    # recommendation distribution (latest)
    rec_strongbuy: int | None = None
    rec_buy: int | None = None
    rec_hold: int | None = None
    rec_sell: int | None = None
    rec_strongsell: int | None = None
    rec_mean: float | None = None                # 1=Strong Buy ~ 5=Strong Sell

    # price targets
    target_mean: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    current_price: float | None = None

    # 최근 변경
    recent_upgrades_downgrades: list[dict[str, Any]] = field(default_factory=list)

    # 폴백 시: 자연어 노트 (perplexity가 줄 수 있음)
    notes: str = ""


# ------------------------------------------------------------------
# Yahoo cookie + crumb (1회 dance, 캐시)
# ------------------------------------------------------------------
def _ensure_yahoo_auth(client: httpx.Client) -> tuple[httpx.Cookies, str] | None:
    """fc.yahoo.com에서 cookie 받고 /v1/test/getcrumb으로 crumb 발급. 55분 캐시."""
    with _AUTH_LOCK:
        now = time.monotonic()
        if (
            _AUTH_CACHE["cookies"] is not None
            and _AUTH_CACHE["crumb"]
            and (now - _AUTH_CACHE["ts"]) < _AUTH_TTL_S
        ):
            return _AUTH_CACHE["cookies"], _AUTH_CACHE["crumb"]
        try:
            r1 = client.get(YAHOO_COOKIE_URL, headers={"User-Agent": UA})
            cookies = r1.cookies
            r2 = client.get(
                YAHOO_CRUMB_URL,
                headers={"User-Agent": UA, "Accept": "text/plain"},
                cookies=cookies,
            )
            crumb = r2.text.strip()
            if not crumb or "<" in crumb or len(crumb) > 50:
                log.warning("Yahoo crumb 비정상: %r", crumb[:80])
                return None
            _AUTH_CACHE["cookies"] = cookies
            _AUTH_CACHE["crumb"] = crumb
            _AUTH_CACHE["ts"] = now
            return cookies, crumb
        except Exception as e:
            log.warning("Yahoo auth 실패: %s", e)
            return None


# ------------------------------------------------------------------
# Yahoo quoteSummary 호출
# ------------------------------------------------------------------
def _yahoo_fetch(ticker: str) -> dict[str, Any] | None:
    """quoteSummary로 6개 module 동시 fetch. 실패 시 None."""
    modules = ",".join([
        "earningsEstimate",
        "revenueEstimate",
        "earningsHistory",
        "recommendationTrend",
        "upgradeDowngradeHistory",
        "financialData",
        "price",
        "defaultKeyStatistics",
    ])
    url = f"{YAHOO_BASE}/v10/finance/quoteSummary/{ticker}"
    with httpx.Client(timeout=YAHOO_TIMEOUT, follow_redirects=True) as client:
        auth = _ensure_yahoo_auth(client)
        if auth is None:
            return None
        cookies, crumb = auth
        try:
            r = client.get(
                url,
                params={"modules": modules, "crumb": crumb},
                headers={"User-Agent": UA, "Accept": "application/json"},
                cookies=cookies,
            )
            if r.status_code != 200:
                log.warning("Yahoo quoteSummary %s → %s", ticker, r.status_code)
                return None
            j = r.json()
            results = (((j or {}).get("quoteSummary") or {}).get("result") or [])
            if not results:
                return None
            return results[0]
        except Exception as e:
            log.warning("Yahoo quoteSummary %s 예외: %s", ticker, e)
            return None


def _raw(node: Any, key: str = "raw") -> Any:
    """Yahoo 응답이 {raw, fmt, longFmt} 패턴이라 raw만 추출."""
    if isinstance(node, dict):
        return node.get(key)
    return node


def _yahoo_to_snapshot(ticker: str, data: dict[str, Any]) -> ConsensusSnapshot:
    """quoteSummary result → ConsensusSnapshot. 누락 필드는 None."""
    snap = ConsensusSnapshot(ticker=ticker.upper(), fetched_at=time.time(), source="yahoo")

    ee = data.get("earningsEstimate") or {}
    # 각 module의 첫 번째 entry는 current quarter, 이후 next q, current y, next y
    if isinstance(ee.get("estimate"), list) and ee["estimate"]:
        try:
            snap.eps_est_current_q = _raw(ee["estimate"][0].get("avg"))
            if len(ee["estimate"]) > 1:
                snap.eps_est_next_q = _raw(ee["estimate"][1].get("avg"))
            if len(ee["estimate"]) > 2:
                snap.eps_est_current_y = _raw(ee["estimate"][2].get("avg"))
            snap.num_analysts = _raw(ee["estimate"][0].get("numberOfAnalysts"))
        except Exception:
            pass

    re_mod = data.get("revenueEstimate") or {}
    if isinstance(re_mod.get("estimate"), list) and re_mod["estimate"]:
        try:
            snap.revenue_est_current_q = _raw(re_mod["estimate"][0].get("avg"))
            if len(re_mod["estimate"]) > 1:
                snap.revenue_est_next_q = _raw(re_mod["estimate"][1].get("avg"))
            if len(re_mod["estimate"]) > 2:
                snap.revenue_est_current_y = _raw(re_mod["estimate"][2].get("avg"))
        except Exception:
            pass

    eh = data.get("earningsHistory") or {}
    if isinstance(eh.get("history"), list):
        hist = []
        for item in eh["history"]:
            try:
                hist.append({
                    "quarter": _raw(item.get("quarter")),
                    "actual_eps": _raw(item.get("epsActual")),
                    "estimate_eps": _raw(item.get("epsEstimate")),
                    "surprise_pct": _raw(item.get("surprisePercent")),
                })
            except Exception:
                continue
        snap.earnings_history = hist

    rt = data.get("recommendationTrend") or {}
    if isinstance(rt.get("trend"), list) and rt["trend"]:
        latest = rt["trend"][0]
        snap.rec_strongbuy = latest.get("strongBuy")
        snap.rec_buy = latest.get("buy")
        snap.rec_hold = latest.get("hold")
        snap.rec_sell = latest.get("sell")
        snap.rec_strongsell = latest.get("strongSell")

    fd = data.get("financialData") or {}
    snap.rec_mean = _raw(fd.get("recommendationMean"))
    snap.target_mean = _raw(fd.get("targetMeanPrice"))
    snap.target_high = _raw(fd.get("targetHighPrice"))
    snap.target_low = _raw(fd.get("targetLowPrice"))
    snap.current_price = _raw(fd.get("currentPrice"))

    udh = data.get("upgradeDowngradeHistory") or {}
    if isinstance(udh.get("history"), list):
        # 최근 10개만
        recent = []
        for item in udh["history"][:10]:
            try:
                recent.append({
                    "epoch": item.get("epochGradeDate"),
                    "firm": item.get("firm"),
                    "to_grade": item.get("toGrade"),
                    "from_grade": item.get("fromGrade"),
                    "action": item.get("action"),
                })
            except Exception:
                continue
        snap.recent_upgrades_downgrades = recent

    return snap


# ------------------------------------------------------------------
# Perplexity 폴백 (Yahoo 실패 시)
# ------------------------------------------------------------------
def _perplexity_fetch(ticker: str, fiscal_period: str | None = None) -> ConsensusSnapshot | None:
    """perplexity sonar로 자연어 fetch. JSON-ish 출력 강제 후 정규화."""
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
    period_hint = f" for the {fiscal_period} reporting period" if fiscal_period else ""
    system = (
        "You are a financial data extractor. Given a US stock ticker, return ONLY a JSON object "
        "with sell-side consensus estimates (latest available). No prose. No code fences."
    )
    user_msg = (
        f"Ticker: {ticker}{period_hint}\n\n"
        "Return JSON with this exact shape (use null if data not found, do not invent):\n"
        "{\n"
        '  "eps_est_current_q": float|null,\n'
        '  "revenue_est_current_q_usd": float|null,\n'
        '  "eps_est_next_q": float|null,\n'
        '  "revenue_est_next_q_usd": float|null,\n'
        '  "num_analysts": int|null,\n'
        '  "target_mean": float|null,\n'
        '  "target_high": float|null,\n'
        '  "target_low": float|null,\n'
        '  "rec_mean": float|null,\n'
        '  "current_price": float|null,\n'
        '  "notes": "1-2 sentences on source/date"\n'
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
            temperature=0.0,
            max_tokens=900,
            context="earnings-consensus-perplexity",
        )
    except Exception as e:
        log.warning("perplexity consensus 실패 %s: %s", ticker, e)
        return None
    if not content:
        return None
    # JSON 추출 (perplexity가 가끔 prose 섞는 경우 대비)
    try:
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            return None
        obj = json.loads(m.group(0))
    except Exception:
        return None

    snap = ConsensusSnapshot(ticker=ticker.upper(), fetched_at=time.time(), source="perplexity")
    snap.eps_est_current_q = obj.get("eps_est_current_q")
    snap.revenue_est_current_q = obj.get("revenue_est_current_q_usd")
    snap.eps_est_next_q = obj.get("eps_est_next_q")
    snap.revenue_est_next_q = obj.get("revenue_est_next_q_usd")
    snap.num_analysts = obj.get("num_analysts")
    snap.target_mean = obj.get("target_mean")
    snap.target_high = obj.get("target_high")
    snap.target_low = obj.get("target_low")
    snap.rec_mean = obj.get("rec_mean")
    snap.current_price = obj.get("current_price")
    snap.notes = obj.get("notes") or ""
    return snap


# ------------------------------------------------------------------
# 공개 API
# ------------------------------------------------------------------
def fetch_consensus(ticker: str, fiscal_period: str | None = None) -> ConsensusSnapshot | None:
    """ticker의 sell-side 컨센서스 1 stamp. Yahoo → perplexity 체인. 실패 시 None."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return None
    data = _yahoo_fetch(ticker)
    if data:
        snap = _yahoo_to_snapshot(ticker, data)
        # Yahoo가 응답했어도 추정치가 비면 perplexity로 보충
        if snap.revenue_est_current_q is None and snap.eps_est_current_q is None:
            ext = _perplexity_fetch(ticker, fiscal_period)
            if ext:
                snap.eps_est_current_q = ext.eps_est_current_q or snap.eps_est_current_q
                snap.revenue_est_current_q = ext.revenue_est_current_q or snap.revenue_est_current_q
                snap.num_analysts = ext.num_analysts or snap.num_analysts
                snap.notes = ext.notes
                snap.source = "mixed"
        return snap
    return _perplexity_fetch(ticker, fiscal_period)


# ------------------------------------------------------------------
# 포매팅 헬퍼 (텔레그램·payload 공용)
# ------------------------------------------------------------------
def fmt_currency(v: float | None) -> str:
    if v is None:
        return "—"
    av = abs(v)
    if av >= 1e9:
        return f"${v/1e9:.2f}B"
    if av >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v:,.2f}"


def fmt_consensus_text(snap: ConsensusSnapshot) -> str:
    """ConsensusSnapshot → 텔레그램용 한국어 텍스트."""
    lines: list[str] = []
    lines.append(f"📐 컨센서스 ({snap.ticker}) — 출처 {snap.source}")
    if snap.num_analysts:
        lines.append(f"  분석가 수: {snap.num_analysts}명")
    if snap.eps_est_current_q is not None or snap.revenue_est_current_q is not None:
        lines.append(
            f"  당분기 추정: EPS {snap.eps_est_current_q if snap.eps_est_current_q is not None else '—'} · "
            f"Revenue {fmt_currency(snap.revenue_est_current_q)}"
        )
    if snap.eps_est_next_q is not None or snap.revenue_est_next_q is not None:
        lines.append(
            f"  차분기 추정: EPS {snap.eps_est_next_q if snap.eps_est_next_q is not None else '—'} · "
            f"Revenue {fmt_currency(snap.revenue_est_next_q)}"
        )
    if snap.target_mean is not None:
        lines.append(
            f"  목표주가: 평균 ${snap.target_mean:.2f} (low ${snap.target_low or 0:.2f} / high ${snap.target_high or 0:.2f})"
            + (f" · 현재 ${snap.current_price:.2f}" if snap.current_price else "")
        )
    if any(v is not None for v in (snap.rec_strongbuy, snap.rec_buy, snap.rec_hold, snap.rec_sell, snap.rec_strongsell)):
        lines.append(
            f"  추천 분포: SB {snap.rec_strongbuy or 0} / B {snap.rec_buy or 0} / H {snap.rec_hold or 0} / "
            f"S {snap.rec_sell or 0} / SS {snap.rec_strongsell or 0}"
            + (f" · mean {snap.rec_mean:.2f}" if snap.rec_mean is not None else "")
        )
    if snap.earnings_history:
        lines.append("  최근 4Q surprise:")
        for h in snap.earnings_history[:4]:
            q = h.get("quarter") or "?"
            a = h.get("actual_eps")
            e = h.get("estimate_eps")
            sp = h.get("surprise_pct")
            a_s = f"{a}" if a is not None else "—"
            e_s = f"{e}" if e is not None else "—"
            sp_s = f"{sp:+.1f}%" if isinstance(sp, (int, float)) else "—"
            lines.append(f"    · {q} EPS actual={a_s} est={e_s} surprise={sp_s}")
    if snap.recent_upgrades_downgrades:
        lines.append("  최근 analyst actions:")
        for u in snap.recent_upgrades_downgrades[:5]:
            firm = u.get("firm") or "?"
            act = u.get("action") or "?"
            to_g = u.get("to_grade") or "?"
            fr_g = u.get("from_grade") or ""
            arrow = f"{fr_g} → {to_g}" if fr_g else to_g
            lines.append(f"    · {firm}: {act} ({arrow})")
    if snap.notes:
        lines.append(f"  메모: {snap.notes}")
    return "\n".join(lines)


def consensus_payload_block(snap: ConsensusSnapshot) -> str:
    """synthesis LLM에 넘길 단일 블록 (영문, 모델이 인용하기 쉽도록 라벨링)."""
    lines: list[str] = []
    lines.append(f"### {snap.ticker} consensus snapshot (source: {snap.source})")
    lines.append(f"num_analysts: {snap.num_analysts}")
    lines.append(f"eps_est_current_q: {snap.eps_est_current_q}")
    lines.append(f"eps_est_next_q: {snap.eps_est_next_q}")
    lines.append(f"eps_est_current_y: {snap.eps_est_current_y}")
    lines.append(f"revenue_est_current_q: {snap.revenue_est_current_q}")
    lines.append(f"revenue_est_next_q: {snap.revenue_est_next_q}")
    lines.append(f"revenue_est_current_y: {snap.revenue_est_current_y}")
    lines.append(f"target_mean / high / low: {snap.target_mean} / {snap.target_high} / {snap.target_low}")
    lines.append(f"current_price: {snap.current_price}")
    lines.append(
        f"rec dist: SB={snap.rec_strongbuy} B={snap.rec_buy} H={snap.rec_hold} "
        f"S={snap.rec_sell} SS={snap.rec_strongsell} mean={snap.rec_mean}"
    )
    if snap.earnings_history:
        lines.append("earnings_history (last 4Q):")
        for h in snap.earnings_history[:4]:
            lines.append(
                f"  - {h.get('quarter')} actual={h.get('actual_eps')} est={h.get('estimate_eps')} surprise%={h.get('surprise_pct')}"
            )
    if snap.recent_upgrades_downgrades:
        lines.append("recent_upgrades_downgrades:")
        for u in snap.recent_upgrades_downgrades[:5]:
            lines.append(
                f"  - {u.get('firm')}: {u.get('action')} ({u.get('from_grade')}→{u.get('to_grade')})"
            )
    if snap.notes:
        lines.append(f"notes: {snap.notes}")
    return "\n".join(lines)
