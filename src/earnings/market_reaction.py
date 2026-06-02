"""Market reaction fetcher (Phase 8 Track M).

콜 발표 이후의 시장 반응 데이터 — 주가 reaction (T-1, T+1, T+5, T+30),
^GSPC 대비 alpha, 옵션 implied move (best-effort), sell-side target revision.

소스:
1. Yahoo Finance chart API (`/v8/finance/chart/{ticker}`) — `consensus._ensure_yahoo_auth` 재사용.
2. ^GSPC 동시 fetch로 relative return 계산.
3. options chain → ATM straddle implied move (실패 시 None).
4. target revisions — `ConsensusSnapshot.recent_upgrades_downgrades`에서 call_date 이후 필터링.
5. perplexity 폴백 (1회) — sell-side 노트 코멘트 정성 보강.

실패는 graceful — None 반환. 호출자는 payload에서 "시장 반응 데이터 미확보"로 처리.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

YAHOO_BASE = "https://query2.finance.yahoo.com"
YAHOO_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


# ------------------------------------------------------------------
# Dataclass
# ------------------------------------------------------------------
@dataclass
class MarketReaction:
    """콜 발표 전후 시장 반응 한 stamp."""
    ticker: str
    call_date: str | None  # YYYY-MM-DD
    fetched_at: float = field(default_factory=lambda: time.time())
    source: str = "yahoo"

    # 가격 reaction (USD)
    price_t_minus_1: float | None = None  # 콜 전일 close
    close_t: float | None = None           # 콜 당일 close
    close_t_plus_1d: float | None = None
    close_t_plus_5d: float | None = None
    close_t_plus_30d: float | None = None

    # returns (%)
    ret_1d: float | None = None
    ret_5d: float | None = None
    ret_30d: float | None = None

    # SPX alpha (종목 ret - SPX ret, %)
    ret_spx_1d: float | None = None
    ret_spx_5d: float | None = None
    alpha_1d: float | None = None
    alpha_5d: float | None = None

    # 옵션 implied move (%, ATM straddle / spot — best-effort)
    implied_move_pct: float | None = None

    # 최근 sell-side target/rating revisions (call_date 이후만)
    target_revisions: list[dict[str, Any]] = field(default_factory=list)

    # current price 컨센서스에서 가져온 것 (정합성 cross-check)
    current_price: float | None = None

    notes: str = ""


# ------------------------------------------------------------------
# Yahoo chart fetch
# ------------------------------------------------------------------
def _yahoo_chart(client: httpx.Client, ticker: str, period1: int, period2: int) -> dict[str, Any] | None:
    """`/v8/finance/chart/{ticker}` 일봉 fetch. Yahoo cookie/crumb 불필요 (chart는 public)."""
    url = f"{YAHOO_BASE}/v8/finance/chart/{ticker}"
    try:
        r = client.get(
            url,
            params={
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "includePrePost": "false",
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
        )
        if r.status_code != 200:
            log.warning("Yahoo chart %s → %s", ticker, r.status_code)
            return None
        j = r.json()
        results = (((j or {}).get("chart") or {}).get("result") or [])
        if not results:
            return None
        return results[0]
    except Exception as e:
        log.warning("Yahoo chart %s 예외: %s", ticker, e)
        return None


def _parse_closes(chart_result: dict[str, Any]) -> list[tuple[datetime, float]]:
    """chart result → [(date, close), ...] sorted by date."""
    ts = chart_result.get("timestamp") or []
    ind = ((chart_result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = ind.get("close") or []
    out: list[tuple[datetime, float]] = []
    for t, c in zip(ts, closes):
        if c is None or not isinstance(c, (int, float)) or math.isnan(float(c)):
            continue
        out.append((datetime.fromtimestamp(int(t), tz=timezone.utc), float(c)))
    out.sort(key=lambda x: x[0])
    return out


def _pick(closes: list[tuple[datetime, float]], target_dt: datetime, side: str = "after") -> tuple[datetime, float] | None:
    """target_dt 직전/직후의 첫 영업일 close."""
    if not closes:
        return None
    if side == "before":
        cand = [c for c in closes if c[0].date() < target_dt.date()]
        return cand[-1] if cand else None
    if side == "on_or_after":
        cand = [c for c in closes if c[0].date() >= target_dt.date()]
        return cand[0] if cand else None
    cand = [c for c in closes if c[0].date() > target_dt.date()]
    return cand[0] if cand else None


def _nth_business(closes: list[tuple[datetime, float]], anchor_dt: datetime, n: int) -> tuple[datetime, float] | None:
    """anchor 이후 n번째 영업일 close. n=1 → 다음 영업일."""
    if not closes:
        return None
    after = [c for c in closes if c[0].date() > anchor_dt.date()]
    if len(after) < n:
        return None
    return after[n - 1]


def _ret_pct(prev: float | None, curr: float | None) -> float | None:
    if prev is None or curr is None or prev <= 0:
        return None
    return (curr / prev - 1.0) * 100.0


# ------------------------------------------------------------------
# Options chain → implied move (ATM straddle / spot)
# ------------------------------------------------------------------
def _yahoo_options_implied_move(client: httpx.Client, ticker: str) -> float | None:
    """`/v7/finance/options/{ticker}` 첫 만기 ATM 콜+풋 가격 / spot.

    가장 가까운 만기의 ATM 콜+풋 mid 합 / underlying price → implied 1-month move 근사.
    실패 None.
    """
    url = f"{YAHOO_BASE}/v7/finance/options/{ticker}"
    try:
        r = client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
        )
        if r.status_code != 200:
            return None
        j = r.json()
        results = (((j or {}).get("optionChain") or {}).get("result") or [])
        if not results:
            return None
        res = results[0]
        quote = res.get("quote") or {}
        spot = quote.get("regularMarketPrice")
        if not spot or spot <= 0:
            return None
        options = res.get("options") or []
        if not options:
            return None
        chain = options[0]
        calls = chain.get("calls") or []
        puts = chain.get("puts") or []
        # ATM = strike에 가장 가까운 것
        def _nearest(arr: list[dict]) -> dict | None:
            if not arr:
                return None
            return min(arr, key=lambda x: abs((x.get("strike") or 0) - spot))
        c = _nearest(calls)
        p = _nearest(puts)
        if not c or not p:
            return None
        c_mid = c.get("lastPrice") or 0
        p_mid = p.get("lastPrice") or 0
        if c_mid <= 0 or p_mid <= 0:
            return None
        straddle = c_mid + p_mid
        return float(straddle) / float(spot) * 100.0
    except Exception:
        return None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------
def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    # ISO with time
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_market_reaction(
    ticker: str,
    call_date: str | None = None,
    consensus_snap: Any | None = None,
) -> MarketReaction | None:
    """콜 발표 전후 시장 반응 묶음 fetch.

    call_date: 콜 발표일 (YYYY-MM-DD). None이면 consensus_snap.earnings_history에서 최근 quarter 추정.
    consensus_snap: ConsensusSnapshot — recent_upgrades_downgrades 재사용 + current_price.
    """
    ticker = ticker.upper()
    snap = MarketReaction(ticker=ticker, call_date=call_date)

    anchor = _parse_date(call_date)
    now = datetime.now(timezone.utc)
    if anchor is None:
        # consensus에서 추정 — earnings_history quarter 텍스트 파싱은 불안정 → today - 14d
        anchor = now - timedelta(days=14)
    # 가격 window: anchor - 30d ~ anchor + 45d (영업일 ~30개씩)
    period1 = int((anchor - timedelta(days=45)).timestamp())
    period2 = int(min(now, anchor + timedelta(days=60)).timestamp())

    try:
        with httpx.Client(timeout=YAHOO_TIMEOUT, follow_redirects=True) as client:
            tk_data = _yahoo_chart(client, ticker, period1, period2)
            spx_data = _yahoo_chart(client, "^GSPC", period1, period2)
            implied = _yahoo_options_implied_move(client, ticker)
    except Exception as e:
        log.warning("market reaction client 실패 %s: %s", ticker, e)
        return snap

    if not tk_data:
        snap.notes = "Yahoo chart fetch 실패"
        return snap

    tk_closes = _parse_closes(tk_data)
    spx_closes = _parse_closes(spx_data) if spx_data else []
    if not tk_closes:
        snap.notes = "ticker close 데이터 없음"
        return snap

    # anchor를 영업일에 맞추기 — anchor on_or_after 첫 close가 T
    t0 = _pick(tk_closes, anchor, side="on_or_after")
    if t0 is None:
        # anchor가 미래거나 휴장 — 마지막 close를 T로
        t0 = tk_closes[-1]
    t_dt, t_close = t0
    snap.close_t = t_close

    prev = _pick(tk_closes, t_dt, side="before")
    if prev:
        snap.price_t_minus_1 = prev[1]
    nxt1 = _nth_business(tk_closes, t_dt, 1)
    if nxt1:
        snap.close_t_plus_1d = nxt1[1]
        snap.ret_1d = _ret_pct(t_close, snap.close_t_plus_1d)
    nxt5 = _nth_business(tk_closes, t_dt, 5)
    if nxt5:
        snap.close_t_plus_5d = nxt5[1]
        snap.ret_5d = _ret_pct(t_close, snap.close_t_plus_5d)
    nxt30 = _nth_business(tk_closes, t_dt, 30)
    if nxt30:
        snap.close_t_plus_30d = nxt30[1]
        snap.ret_30d = _ret_pct(t_close, snap.close_t_plus_30d)

    # SPX alpha
    if spx_closes:
        spx_t = _pick(spx_closes, anchor, side="on_or_after") or spx_closes[-1]
        spx_t_dt, spx_t_close = spx_t
        spx_n1 = _nth_business(spx_closes, spx_t_dt, 1)
        spx_n5 = _nth_business(spx_closes, spx_t_dt, 5)
        if spx_n1:
            snap.ret_spx_1d = _ret_pct(spx_t_close, spx_n1[1])
            if snap.ret_1d is not None and snap.ret_spx_1d is not None:
                snap.alpha_1d = snap.ret_1d - snap.ret_spx_1d
        if spx_n5:
            snap.ret_spx_5d = _ret_pct(spx_t_close, spx_n5[1])
            if snap.ret_5d is not None and snap.ret_spx_5d is not None:
                snap.alpha_5d = snap.ret_5d - snap.ret_spx_5d

    if implied is not None:
        snap.implied_move_pct = implied

    # target revisions (consensus snap의 recent_upgrades_downgrades 활용)
    if consensus_snap is not None:
        try:
            snap.current_price = getattr(consensus_snap, "current_price", None)
            udh = getattr(consensus_snap, "recent_upgrades_downgrades", []) or []
            anchor_epoch = int(anchor.timestamp())
            revisions = []
            for item in udh:
                ep = item.get("epoch")
                if ep is None:
                    continue
                # Yahoo epoch는 milliseconds일 수 있음
                ep_int = int(ep)
                if ep_int > 1e11:
                    ep_int = ep_int // 1000
                # call_date 이후만
                if ep_int < anchor_epoch - 86400 * 2:
                    continue
                revisions.append({
                    "firm": item.get("firm"),
                    "to_grade": item.get("to_grade"),
                    "from_grade": item.get("from_grade"),
                    "action": item.get("action"),
                    "date": datetime.fromtimestamp(ep_int, tz=timezone.utc).strftime("%Y-%m-%d"),
                })
            snap.target_revisions = revisions
        except Exception as e:
            log.warning("target revisions 파싱 실패 %s: %s", ticker, e)

    return snap


# ------------------------------------------------------------------
# Payload block (synthesis용)
# ------------------------------------------------------------------
def market_reaction_payload_block(snap: MarketReaction | None) -> str:
    """LLM payload용 한 블록."""
    if snap is None:
        return ""
    lines = [f"### {snap.ticker} market reaction (source: {snap.source})"]
    if snap.call_date:
        lines.append(f"  call_date={snap.call_date}")
    def _f(v: float | None, suffix: str = "") -> str:
        if v is None:
            return "—"
        return f"{v:+.2f}{suffix}" if "%" in suffix else f"${v:.2f}"
    lines.append(f"  price T-1={_f(snap.price_t_minus_1)} → T={_f(snap.close_t)}")
    lines.append(f"  close T+1d={_f(snap.close_t_plus_1d)} (ret_1d={_f(snap.ret_1d, '%')})")
    lines.append(f"  close T+5d={_f(snap.close_t_plus_5d)} (ret_5d={_f(snap.ret_5d, '%')})")
    lines.append(f"  close T+30d={_f(snap.close_t_plus_30d)} (ret_30d={_f(snap.ret_30d, '%')})")
    lines.append(f"  alpha vs ^GSPC: 1d={_f(snap.alpha_1d, '%')} / 5d={_f(snap.alpha_5d, '%')}")
    if snap.implied_move_pct is not None:
        lines.append(f"  options implied move (ATM straddle / spot): {snap.implied_move_pct:.1f}%")
    if snap.target_revisions:
        lines.append(f"  target revisions since call ({len(snap.target_revisions)}):")
        for r in snap.target_revisions[:8]:
            lines.append(
                f"    - {r.get('date','?')} {r.get('firm','?')}: "
                f"{r.get('from_grade','?')} → {r.get('to_grade','?')} ({r.get('action','?')})"
            )
    else:
        lines.append("  target revisions since call: (none)")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Decision page helper — 1줄 요약
# ------------------------------------------------------------------
def fmt_reaction_oneliner(snap: MarketReaction | None) -> str:
    if snap is None:
        return "—"
    parts = []
    if snap.ret_1d is not None:
        parts.append(f"1d {snap.ret_1d:+.1f}%")
    if snap.ret_5d is not None:
        parts.append(f"5d {snap.ret_5d:+.1f}%")
    if snap.ret_30d is not None:
        parts.append(f"30d {snap.ret_30d:+.1f}%")
    if snap.alpha_5d is not None:
        parts.append(f"α5d {snap.alpha_5d:+.1f}%")
    return " · ".join(parts) if parts else "—"
