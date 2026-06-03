"""포지션 list → enriched dict (OHLCV 기반 손익률 + technical + thesis 분류).

watchlist._enrich_one 룰 재사용. 추가로 buy_price 대비 손익률 계산.

Output:
  positions_enriched: [
    {ticker, market, buy_price, shares, current_price, pnl_pct, pnl_amount,
     chg_5d, chg_1m, technical{}, thesis_class, sector, market_cap}
  ]
  summary: {total_positions, total_cost, total_value, total_pnl_pct, total_pnl_amount,
            us_count, kr_count}
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def _load_ohlcv(ticker: str, market: str, days: int = 260):
    """KR은 screener_adapter 우선 → FDR 폴백. US는 FDR."""
    try:
        from src.report.data import screener_adapter
        df = screener_adapter.load_ohlcv_df(ticker, market, days=days)
        if df is not None and len(df) >= 30:
            return df
    except Exception:
        pass
    try:
        from src.report.data.fetch_prices import fetch_ohlcv
        df = fetch_ohlcv(ticker, days=days)
        if df is not None and len(df) >= 30:
            return df
    except Exception:
        pass
    return None


def _technical(df) -> dict:
    """MA20/50/200 + 52w hi/lo + pct_of_52w_high."""
    if df is None or len(df) < 20:
        return {}
    try:
        close = df["Close"]
        last = float(close.iloc[-1])
        out = {"close": round(last, 4)}
        if len(close) >= 5:
            out["chg_5d"] = round((last / float(close.iloc[-6]) - 1) * 100, 2) if len(close) >= 6 else None
        if len(close) >= 20:
            out["chg_1m"] = round((last / float(close.iloc[-21]) - 1) * 100, 2) if len(close) >= 21 else None
            ma20 = float(close.rolling(20).mean().iloc[-1])
            out["above_ma20"] = bool(last >= ma20)
        if len(close) >= 50:
            ma50 = float(close.rolling(50).mean().iloc[-1])
            out["above_ma50"] = bool(last >= ma50)
            out["ma50"] = round(ma50, 4)
        if len(close) >= 200:
            ma200 = float(close.rolling(200).mean().iloc[-1])
            out["above_ma200"] = bool(last >= ma200)
            out["ma200"] = round(ma200, 4)
        window = close.iloc[-252:] if len(close) >= 252 else close
        hi = float(window.max()); lo = float(window.min())
        out["high_52w"] = round(hi, 4)
        out["low_52w"] = round(lo, 4)
        out["pct_52w_high"] = round(last / hi * 100, 1) if hi > 0 else None
        return out
    except Exception:
        log.exception("[portfolio_enrich] technical 계산 실패")
        return {}


def _thesis_class(pos: dict, tech: dict, theme_hot: list[str]) -> str:
    """thesis 분류 — chg_5d 기반.

    - chg_5d ≥ +5%: 강화
    - chg_5d ≤ -5%: 약화
    - hot 테마 매칭이면서 chg_5d 음수: 디커플링
    - 그 외: 유지
    """
    chg = tech.get("chg_5d")
    if not isinstance(chg, (int, float)):
        return "유지"
    if chg >= 5:
        return "강화"
    if chg <= -5:
        return "약화"
    # hot 테마 매칭은 enrichment 단계에서는 단순화 — sector 명시 안 됨
    return "유지"


def enrich_positions(positions: list[dict], theme_hot: Optional[list[str]] = None) -> dict:
    """포지션 list → enriched + summary."""
    enriched: list[dict] = []
    total_cost = 0.0
    total_value = 0.0
    us_count = 0
    kr_count = 0
    for pos in positions:
        ticker = (pos.get("ticker") or "").strip().upper()
        market = (pos.get("market") or "US").strip().upper()
        try:
            bp = float(pos.get("buy_price") or 0)
            sh = float(pos.get("shares") or 0)
        except (ValueError, TypeError):
            continue
        if not ticker or bp <= 0 or sh <= 0:
            continue
        df = _load_ohlcv(ticker, market)
        tech = _technical(df)
        current = tech.get("close")
        pnl_pct = None
        pnl_amount = None
        cost = bp * sh
        value = None
        if isinstance(current, (int, float)) and current > 0:
            pnl_pct = round((current / bp - 1) * 100, 2)
            pnl_amount = round((current - bp) * sh, 2)
            value = current * sh
            total_value += value
        else:
            value = cost  # fallback — 미확보 시 매수가로 가치 계산
            total_value += cost
        total_cost += cost
        if market == "US":
            us_count += 1
        else:
            kr_count += 1
        enriched.append({
            "ticker": ticker,
            "market": market,
            "buy_price": bp,
            "shares": sh,
            "current_price": current,
            "value": round(value, 2) if value else None,
            "cost": round(cost, 2),
            "pnl_pct": pnl_pct,
            "pnl_amount": pnl_amount,
            "chg_5d": tech.get("chg_5d"),
            "chg_1m": tech.get("chg_1m"),
            "technical": {
                "above_ma20": tech.get("above_ma20"),
                "above_ma50": tech.get("above_ma50"),
                "above_ma200": tech.get("above_ma200"),
                "pct_52w_high": tech.get("pct_52w_high"),
                "ma50": tech.get("ma50"),
                "ma200": tech.get("ma200"),
                "high_52w": tech.get("high_52w"),
                "low_52w": tech.get("low_52w"),
            },
            "thesis_class": _thesis_class(pos, tech, theme_hot or []),
            "added_at": pos.get("added_at"),
        })
    summary = {
        "total_positions": len(enriched),
        "us_count": us_count,
        "kr_count": kr_count,
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "total_pnl_amount": round(total_value - total_cost, 2),
        "total_pnl_pct": round((total_value / total_cost - 1) * 100, 2) if total_cost > 0 else None,
    }
    log.info("[portfolio_enrich] %d종목 enrich · 총가치 %.0f vs 비용 %.0f (PnL %s%%)",
             len(enriched), summary["total_value"], summary["total_cost"], summary["total_pnl_pct"])
    return {"positions": enriched, "summary": summary}
