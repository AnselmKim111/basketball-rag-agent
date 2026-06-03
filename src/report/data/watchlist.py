"""주목할만한 종목 통합 watchlist — 5개 stream을 후보 풀로 합쳐 점수·랭킹·enrichment.

Streams:
  - FollowUp     : prev_snapshot.highlights_tickers (어제 다룬 종목)
  - EarningsActual: Finnhub + FMP earnings (실제 EPS/Rev surprise, 백워드)
  - IPO          : Finnhub IPO calendar (priced + expected, 자금흐름 신호)
  - SectorLeader : screener_adapter signals 중 hot 섹터 매칭 (theme_momentum 연결)
  - TrendReversal: screener_adapter trend_reversal 신호 (ma_golden_cross 등)

Selection: US top REPORT_WATCHLIST_US_TOP (=8), KR top REPORT_WATCHLIST_KR_TOP (=6).
FollowUp은 최대 4개 강제 포함. 한 카테고리가 ≥4개 차지 못하도록 다양성 cap.

Enrichment: 각 종목별 OHLCV → technical(MA·52w·pct_of_52w) + catalyst (earnings_actual or ipo)
+ estimate_revision + news_snippets (티커 substring match) + sector_context.

키 없거나 데이터 없으면 graceful skip — 빈 stream 무시하고 가능한 것만으로 watchlist 구성.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Optional

from src.report.data import (
    screener_adapter,
    fetch_earnings_surprise,
    fetch_estimate_revisions,
    fetch_ipo,
    fetch_market_caps,
    kr_theme_linkage,
    us_theme_linkage,
)

log = logging.getLogger(__name__)

# 카테고리 보너스 (점수 함수에서 max 사용)
_CAT_BONUS = {
    "ipo": 1.00,
    "earnings_actual": 0.90,
    "trend_reversal": 0.80,
    "kr_linkage": 0.75,
    "sector_leader": 0.70,
    "us_theme_leader": 0.65,
    "followup": 0.50,
    "estimate_revision": 0.55,
}

# 후보 풀 크기 cap
_STREAM_CAP = {
    "followup": 12,
    "earnings_actual": 18,
    "ipo": 10,
    "sector_leader": 12,
    "trend_reversal": 20,
    "kr_linkage": 15,
    "us_theme_leader": 15,
}

# trend_reversal로 분류할 screener 시그널 키
_TREND_REVERSAL_SIGNALS = {
    "ma_golden_cross",
    "rsi_oversold_recovery",
    "base_hold_after_breakout",
    "downtrend_exit",
}

# sector_leader로 분류할 시그널 (강한 모멘텀 + 시총 큰 종목)
_SECTOR_LEADER_SIGNALS = {
    "high_52w",
    "high_all",
    "volume_breakout",
    "near_breakout_52w",
    "vcp_breakout",
}


def _env_int(k: str, default: int) -> int:
    try:
        return int(os.getenv(k, "") or default)
    except ValueError:
        return default


def _env_float(k: str, default: float) -> float:
    try:
        return float(os.getenv(k, "") or default)
    except ValueError:
        return default


# ------------------------------------------------------------------
# 후보 dataclass (가벼운 dict 기반)
# ------------------------------------------------------------------
def _new_candidate(ticker: str, market: str, **kwargs) -> dict:
    return {
        "ticker": ticker,
        "market": market,
        "name": kwargs.get("name") or ticker,
        "sector": kwargs.get("sector") or "",
        "market_cap": kwargs.get("market_cap") or 0,
        "categories": [],  # 채워짐
        "signal_strength": 0.0,
        "chg_5d": 0.0,
        "news_mentions": 0,
        # 스트림별 raw data 캐리오버 (enrichment에서 사용)
        "_earnings_raw": kwargs.get("earnings_raw"),
        "_ipo_raw": kwargs.get("ipo_raw"),
        "_signal_raw": kwargs.get("signal_raw"),
    }


def _merge_candidate(pool: dict[tuple[str, str], dict], cand: dict, category: str,
                     strength_add: float = 0.0) -> None:
    """ticker+market 키로 dedup. 같은 종목이 여러 stream에 hit하면 categories·strength 합산."""
    key = (cand["ticker"].upper(), cand["market"].upper())
    existing = pool.get(key)
    if existing:
        if category not in existing["categories"]:
            existing["categories"].append(category)
        existing["signal_strength"] += strength_add
        for src_key in ("_earnings_raw", "_ipo_raw", "_signal_raw"):
            if cand.get(src_key) and not existing.get(src_key):
                existing[src_key] = cand[src_key]
        if not existing.get("sector") and cand.get("sector"):
            existing["sector"] = cand["sector"]
        if not existing.get("market_cap") and cand.get("market_cap"):
            existing["market_cap"] = cand["market_cap"]
    else:
        cand["categories"].append(category)
        cand["signal_strength"] = strength_add
        pool[key] = cand


# ------------------------------------------------------------------
# Stream A — FollowUp
# ------------------------------------------------------------------
def _stream_followup(prev_snapshot: dict | None) -> list[dict]:
    if not prev_snapshot:
        return []
    items = prev_snapshot.get("highlights_meta") or []
    out: list[dict] = []
    for it in items[: _STREAM_CAP["followup"]]:
        ticker = (it.get("ticker") or "").upper()
        if not ticker:
            continue
        # label은 enrichment에서 "NAME(TICKER)" 형식으로 저장됨 → name만 추출.
        # 그렇지 않으면 매일 followup 거칠 때마다 wrap 누적되어 "NIO(NIO)(NIO)(NIO)" 발생.
        raw_label = it.get("label") or ticker
        name = (raw_label.split("(")[0] or ticker).strip()
        out.append(_new_candidate(
            ticker, it.get("market") or "US",
            name=name,
            sector=it.get("sector"),
            market_cap=it.get("market_cap"),
        ))
    return out


# ------------------------------------------------------------------
# Stream B — EarningsActual (Finnhub + FMP merge)
# ------------------------------------------------------------------
def _stream_earnings(earnings_fmp: dict | None) -> list[dict]:
    fh_entries = fetch_earnings_surprise.fetch_finnhub_calendar(days_back=14, days_fwd=0)
    fmp_recent = []
    if earnings_fmp and isinstance(earnings_fmp.get("recent"), dict):
        fmp_recent = earnings_fmp["recent"].get("entries") or []
    top = fetch_earnings_surprise.select_top_surprises(
        fh_entries, top_beats=8, top_misses=5, min_abs_surprise_pct=5.0
    )
    all_picks = top["beats"] + top["misses"]
    out: list[dict] = []
    for e in all_picks[: _STREAM_CAP["earnings_actual"]]:
        sym = (e.get("symbol") or "").upper()
        if not sym:
            continue
        cv = fetch_earnings_surprise.cross_validate_eps(sym, fh_entries, fmp_recent)
        e_enriched = {**e, "source_consensus": cv["source_consensus"],
                      "eps_disputed": cv["eps_disputed"]}
        out.append(_new_candidate(
            sym, "US",
            name=sym,
            earnings_raw=e_enriched,
        ))
    return out


# ------------------------------------------------------------------
# Stream C — IPO
# ------------------------------------------------------------------
def _stream_ipo() -> list[dict]:
    entries = fetch_ipo.fetch_ipo_calendar(days_back=14, days_fwd=14)
    sel = fetch_ipo.select_top_ipos(entries, top_priced=6, top_expected=4, min_value=5e8)
    out: list[dict] = []
    for e in (sel["priced"] + sel["expected"])[: _STREAM_CAP["ipo"]]:
        sym = (e.get("symbol") or "").upper()
        if not sym:
            continue
        out.append(_new_candidate(
            sym, "US",
            name=e.get("name") or sym,
            market_cap=e.get("total_value") or 0,
            ipo_raw=e,
        ))
    return out


# ------------------------------------------------------------------
# Stream D — SectorLeader (screener 시그널 중 hot 섹터 매칭)
# ------------------------------------------------------------------
def _hot_sector_keywords(theme_hot: list[str]) -> list[str]:
    """hot 테마 라벨 → 섹터 키워드 (대소문자 무시 substring match)."""
    if not theme_hot:
        return []
    return [t.lower() for t in theme_hot if t]


def _is_in_hot_sector(sector: str, sector_kw: list[str]) -> bool:
    if not sector or not sector_kw:
        return False
    s = sector.lower()
    return any(k in s or s in k for k in sector_kw)


def _stream_sector_leader(theme_hot: list[str], market: str) -> list[dict]:
    sigs = screener_adapter.load_signals(market)
    if not sigs:
        return []
    sector_kw = _hot_sector_keywords(theme_hot)
    out: list[dict] = []
    pool_by_ticker: dict[str, dict] = {}
    for sig_key, entries in sigs.items():
        if sig_key not in _SECTOR_LEADER_SIGNALS:
            continue
        for e in entries:
            tkr = (e.get("ticker") or "").upper()
            if not tkr:
                continue
            sector = e.get("sector") or ""
            # hot 섹터에 속해야 sector_leader로 자격
            if not _is_in_hot_sector(sector, sector_kw):
                continue
            existing = pool_by_ticker.get(tkr)
            chg = float(e.get("chg_pct") or 0.0)
            if existing:
                existing["_signal_raw"].setdefault("signals", []).append(sig_key)
                existing["signal_strength"] += 1.0
                existing["chg_5d"] = max(existing["chg_5d"], chg)
                continue
            cand = _new_candidate(
                tkr, market,
                name=e.get("name") or tkr,
                sector=sector,
                market_cap=e.get("market_cap") or 0,
                signal_raw={"signals": [sig_key], "raw": e},
            )
            cand["chg_5d"] = chg
            pool_by_ticker[tkr] = cand
    # 강도·시총 순
    sorted_list = sorted(
        pool_by_ticker.values(),
        key=lambda c: (c["signal_strength"], c.get("market_cap") or 0),
        reverse=True,
    )
    for c in sorted_list[: _STREAM_CAP["sector_leader"]]:
        out.append(c)
    return out


# ------------------------------------------------------------------
# Stream E — TrendReversal (screener trend_reversal 신호)
# ------------------------------------------------------------------
def _stream_trend_reversal(market: str) -> list[dict]:
    sigs = screener_adapter.load_signals(market)
    if not sigs:
        return []
    out: list[dict] = []
    pool_by_ticker: dict[str, dict] = {}
    for sig_key, entries in sigs.items():
        if sig_key not in _TREND_REVERSAL_SIGNALS:
            continue
        for e in entries:
            tkr = (e.get("ticker") or "").upper()
            if not tkr:
                continue
            existing = pool_by_ticker.get(tkr)
            chg = float(e.get("chg_pct") or 0.0)
            if existing:
                existing["_signal_raw"]["signals"].append(sig_key)
                existing["signal_strength"] += 1.0
                existing["chg_5d"] = max(existing["chg_5d"], chg)
                continue
            cand = _new_candidate(
                tkr, market,
                name=e.get("name") or tkr,
                sector=e.get("sector") or "",
                market_cap=e.get("market_cap") or 0,
                signal_raw={"signals": [sig_key], "raw": e},
            )
            cand["chg_5d"] = chg
            cand["signal_strength"] = 1.0
            pool_by_ticker[tkr] = cand
    sorted_list = sorted(
        pool_by_ticker.values(),
        key=lambda c: (c["signal_strength"], c.get("market_cap") or 0),
        reverse=True,
    )
    for c in sorted_list[: _STREAM_CAP["trend_reversal"]]:
        out.append(c)
    return out


# ------------------------------------------------------------------
# 시총 backfill — 0인 후보 일괄 fetch
# ------------------------------------------------------------------
def _backfill_market_caps(us_pool: dict, kr_pool: dict) -> None:
    us_missing = [c["ticker"] for c in us_pool.values() if not (c.get("market_cap") or 0)]
    kr_missing = [c["ticker"] for c in kr_pool.values() if not (c.get("market_cap") or 0)]
    if us_missing:
        try:
            caps = fetch_market_caps.fetch_us_market_caps(us_missing)
            for c in us_pool.values():
                if not (c.get("market_cap") or 0):
                    cap = caps.get(c["ticker"].upper())
                    if cap:
                        c["market_cap"] = cap
        except Exception:
            log.exception("[watchlist] US 시총 backfill 실패")
    if kr_missing:
        try:
            kr_caps = fetch_market_caps.fetch_kr_market_caps()
            for c in kr_pool.values():
                if not (c.get("market_cap") or 0):
                    cap = kr_caps.get(c["ticker"])
                    if cap:
                        c["market_cap"] = cap
        except Exception:
            log.exception("[watchlist] KR 시총 backfill 실패")


# ------------------------------------------------------------------
# Stream F — KR theme linkage (미국 hot 테마 → 한국 수혜주 매핑)
# screener.db 비어있어도 작동. theme_rows의 r5d 강도를 chg_5d로 사용.
# ------------------------------------------------------------------
def _stream_kr_theme_linkage(theme_rows: list[dict] | None, top_n_themes: int = 8) -> list[dict]:
    if not theme_rows:
        return []
    # 5D 강도 절대값 큰 순 (강하게 움직이는 테마 우선) → 상위 top_n_themes만
    ranked = sorted(
        [r for r in theme_rows if r.get("r5d") is not None],
        key=lambda r: -abs(float(r.get("r5d") or 0)),
    )[:top_n_themes]
    out: list[dict] = []
    seen: set[str] = set()
    for r in ranked:
        label = r.get("label") or ""
        r5d = float(r.get("r5d") or 0)
        for name, ticker, sector in kr_theme_linkage.beneficiaries_with_tickers(label):
            if ticker in seen:
                continue
            seen.add(ticker)
            cand = _new_candidate(
                ticker, "KR",
                name=name,
                sector=sector,
                market_cap=0,  # enrichment에서 fetch_prices가 채울 수 있음
                signal_raw={"theme": label, "r5d": r5d, "beneficiary_of": label},
            )
            cand["chg_5d"] = r5d  # 미국 테마 5D 강도 = 한국 수혜주 잠재력 proxy
            cand["signal_strength"] = min(abs(r5d) / 5.0, 2.0)  # 5%당 1.0, max 2.0
            out.append(cand)
            if len(out) >= _STREAM_CAP["kr_linkage"]:
                return out
    return out


# ------------------------------------------------------------------
# Stream G — US theme leader (미국 hot 테마 → 핵심 미국 종목)
# us_screener.db sector_leader 비활성 시 폴백.
# ------------------------------------------------------------------
def _stream_us_theme_leader(theme_rows: list[dict] | None, top_n_themes: int = 6) -> list[dict]:
    if not theme_rows:
        return []
    ranked = sorted(
        [r for r in theme_rows if r.get("r5d") is not None],
        key=lambda r: -abs(float(r.get("r5d") or 0)),
    )[:top_n_themes]
    out: list[dict] = []
    seen: set[str] = set()
    for r in ranked:
        label = r.get("label") or ""
        r5d = float(r.get("r5d") or 0)
        for ticker in us_theme_linkage.core_tickers_for_label(label):
            if ticker in seen:
                continue
            seen.add(ticker)
            cand = _new_candidate(
                ticker, "US",
                name=ticker,
                sector=label.split("(")[0].strip(),
                market_cap=0,
                signal_raw={"theme": label, "r5d": r5d, "leader_of": label},
            )
            cand["chg_5d"] = r5d
            cand["signal_strength"] = min(abs(r5d) / 5.0, 2.0)
            out.append(cand)
            if len(out) >= _STREAM_CAP["us_theme_leader"]:
                return out
    return out


# ------------------------------------------------------------------
# 점수 함수
# ------------------------------------------------------------------
def _score(c: dict, max_cap: float) -> float:
    w_cap = _env_float("REPORT_WATCHLIST_W_CAP", 0.20)
    w_chg = _env_float("REPORT_WATCHLIST_W_CHG", 0.25)
    w_signal = _env_float("REPORT_WATCHLIST_W_SIGNAL", 0.25)
    w_news = _env_float("REPORT_WATCHLIST_W_NEWS", 0.15)
    w_cat = _env_float("REPORT_WATCHLIST_W_CATEGORY", 0.15)
    cap = float(c.get("market_cap") or 0)
    norm_cap = math.log10(max(cap, 1)) / math.log10(max(max_cap, 10)) if max_cap > 1 else 0.0
    norm_cap = max(0.0, min(1.0, norm_cap))
    norm_chg = min(abs(float(c.get("chg_5d") or 0)) / 25.0, 1.0)
    norm_signal = min(float(c.get("signal_strength") or 0) / 5.0, 1.0)
    norm_news = min(int(c.get("news_mentions") or 0) / 3.0, 1.0)
    cat_bonus = max((_CAT_BONUS.get(cat, 0.3) for cat in c.get("categories") or []), default=0.3)
    return (
        w_cap * norm_cap + w_chg * norm_chg + w_signal * norm_signal
        + w_news * norm_news + w_cat * cat_bonus
    )


# ------------------------------------------------------------------
# Enrichment
# ------------------------------------------------------------------
def _annotate_news_mentions(pool: dict, news: list[dict] | None) -> None:
    if not news:
        return
    # news 항목: {title, summary, source} 같은 dict 가정. 본문에 ticker substring 출현 카운트.
    corpus = []
    for n in news[:30]:
        if not isinstance(n, dict):
            continue
        text = " ".join(str(n.get(k) or "") for k in ("title", "summary", "headline", "text"))
        corpus.append(text)
    big_blob = " ".join(corpus)
    for cand in pool.values():
        tkr = cand["ticker"]
        # 단순 substring match — 짧은 티커(<3)는 false positive 위험 → 공백 boundary로 보호
        if len(tkr) <= 2:
            count = big_blob.count(f" {tkr} ") + big_blob.count(f"({tkr})") + big_blob.count(f"${tkr}")
        else:
            count = big_blob.upper().count(tkr.upper())
        cand["news_mentions"] = count


def _ticker_news_snippets(ticker: str, news: list[dict] | None, max_n: int = 2) -> list[str]:
    if not news:
        return []
    found: list[str] = []
    tkr_up = ticker.upper()
    for n in news:
        if not isinstance(n, dict):
            continue
        title = n.get("title") or n.get("headline") or ""
        summary = n.get("summary") or n.get("text") or ""
        joined = (title + " " + summary).upper()
        if len(tkr_up) <= 2:
            in_text = (f" {tkr_up} " in joined) or (f"({tkr_up})" in joined) or (f"${tkr_up}" in joined)
        else:
            in_text = tkr_up in joined
        if in_text:
            src = n.get("source") or n.get("publisher") or "뉴스"
            snippet = (title or summary).strip()
            if len(snippet) > 140:
                snippet = snippet[:137] + "..."
            found.append(f"{snippet} ({src})")
            if len(found) >= max_n:
                break
    return found


def _technical_from_df(df) -> dict:
    """OHLCV df → MA20/50/200, 52w hi/lo, pct_of_52w_high."""
    try:
        import pandas as pd  # type: ignore  # noqa
    except Exception:
        return {}
    if df is None or len(df) < 5:
        return {}
    out: dict = {}
    try:
        close = df["Close"].astype(float)
        hi = df["High"].astype(float)
        lo = df["Low"].astype(float)
        cur = float(close.iloc[-1])
        out["close"] = cur
        if len(close) >= 200:
            out["ma200"] = float(close.tail(200).mean())
            out["above_ma200"] = cur > out["ma200"]
        if len(close) >= 50:
            out["ma50"] = float(close.tail(50).mean())
            out["above_ma50"] = cur > out["ma50"]
        if len(close) >= 20:
            out["ma20"] = float(close.tail(20).mean())
            out["above_ma20"] = cur > out["ma20"]
        window = min(len(df), 252)
        if window >= 30:
            hi52 = float(hi.tail(window).max())
            lo52 = float(lo.tail(window).min())
            out["support_52w_low"] = lo52
            out["resistance_52w_high"] = hi52
            if hi52 > 0:
                out["pct_52w_high"] = round(cur / hi52 * 100, 2)
                out["is_new_high"] = cur >= hi52
        if len(close) >= 22:
            out["chg_1m"] = round(float((cur / close.iloc[-22] - 1) * 100), 2)
        if len(close) >= 6:
            out["chg_5d"] = round(float((cur / close.iloc[-6] - 1) * 100), 2)
        if len(close) >= 2:
            out["chg_1d"] = round(float((cur / close.iloc[-2] - 1) * 100), 2)
    except Exception:
        log.exception("[watchlist] technical 계산 실패")
    return out


def _load_ohlcv_for(ticker: str, market: str, days: int = 260):
    """KR은 screener_adapter 우선. US도 시도. 둘 다 빈응답이면 FDR 폴백."""
    df = screener_adapter.load_ohlcv_df(ticker, market, days=days)
    if df is not None and len(df) >= 30:
        return df
    # FDR 폴백 — fetch_prices 재사용
    try:
        from src.report.data.fetch_prices import fetch_ohlcv
        df2 = fetch_ohlcv(ticker, days=days)
        if df2 is not None and len(df2) >= 30:
            return df2
    except Exception:
        pass
    return df


def _sector_context_from_themes(sector: str, theme_rows: list[dict] | None) -> str:
    if not theme_rows or not sector:
        return ""
    s = sector.lower()
    for r in theme_rows:
        lab = (r.get("label") or "").lower()
        if lab and (lab in s or s in lab):
            bucket = r.get("bucket") or "중립"
            r5d = r.get("r5d")
            r1m = r.get("r1m")
            parts = [f"{r.get('label')} {bucket}"]
            if r5d is not None:
                parts.append(f"5D {r5d:+.1f}%")
            if r1m is not None:
                parts.append(f"1M {r1m:+.1f}%")
            return " · ".join(parts)
    return ""


def _enrich_one(cand: dict, theme_rows: list[dict] | None,
                news: list[dict] | None) -> dict:
    """후보 1개 → LLM payload 형식 enriched dict 변환."""
    ticker = cand["ticker"]
    market = cand["market"]
    df = _load_ohlcv_for(ticker, market, days=260)
    tech = _technical_from_df(df)
    snippets = _ticker_news_snippets(ticker, news)
    sector_ctx = _sector_context_from_themes(cand.get("sector") or "", theme_rows)

    out: dict = {
        "label": f"{cand.get('name') or ticker}({ticker})",
        "ticker": ticker,
        "market": market,
        "sector": cand.get("sector") or "",
        "market_cap": cand.get("market_cap") or 0,
        "categories": list(cand.get("categories") or []),
        "chg_1d": tech.get("chg_1d"),
        "chg_5d": tech.get("chg_5d") if tech.get("chg_5d") is not None else cand.get("chg_5d"),
        "chg_1m": tech.get("chg_1m"),
        "close": tech.get("close"),
        "technical": {
            "pct_52w_high": tech.get("pct_52w_high"),
            "is_new_high": tech.get("is_new_high"),
            "ma200": tech.get("ma200"),
            "above_ma200": tech.get("above_ma200"),
            "above_ma50": tech.get("above_ma50"),
            "above_ma20": tech.get("above_ma20"),
            "support_52w_low": tech.get("support_52w_low"),
            "resistance_52w_high": tech.get("resistance_52w_high"),
        },
        "news_snippets": snippets,
        "sector_context": sector_ctx,
    }
    er = cand.get("_earnings_raw")
    if er:
        out["earnings_actual"] = {
            "asof": er.get("date"),
            "eps_actual": er.get("eps_actual"),
            "eps_est": er.get("eps_est"),
            "eps_surprise_pct": er.get("eps_surprise_pct"),
            "rev_actual": er.get("rev_actual"),
            "rev_est": er.get("rev_est"),
            "rev_surprise_pct": er.get("rev_surprise_pct"),
            "source_consensus": er.get("source_consensus"),
            "eps_disputed": er.get("eps_disputed"),
        }
    ir = cand.get("_ipo_raw")
    if ir:
        out["ipo"] = {
            "name": ir.get("name"),
            "date": ir.get("date"),
            "exchange": ir.get("exchange"),
            "price": ir.get("price"),
            "shares": ir.get("shares"),
            "total_value": ir.get("total_value"),
            "status": ir.get("status"),
        }
    sr = cand.get("_signal_raw")
    if sr:
        out["screener_signals"] = sr.get("signals") or []
        # KR theme linkage 정보 — LLM이 "미국 X 테마(5D Y%) 수혜주" 맥락 활용
        if sr.get("beneficiary_of"):
            out["theme_linkage"] = {
                "us_theme_label": sr.get("beneficiary_of"),
                "us_theme_r5d": sr.get("r5d"),
            }
    return out


# ------------------------------------------------------------------
# 메인 진입점
# ------------------------------------------------------------------
def build_watchlist(date_iso: str, theme_rows: list[dict] | None,
                    earnings: dict | None, news: list[dict] | None,
                    prev_snapshot: dict | None) -> dict:
    """4종 stream + F/U → 통합 후보 풀 → 점수·랭킹·selection·enrichment.

    반환: {"us": [enriched dict, ...], "kr": [enriched dict, ...]}.
    """
    us_top = _env_int("REPORT_WATCHLIST_US_TOP", 8)
    kr_top = _env_int("REPORT_WATCHLIST_KR_TOP", 6)
    theme_hot = (theme_rows and [r.get("label") for r in theme_rows[:5] if r.get("label")]) or []

    # 후보 풀 (US와 KR 분리)
    us_pool: dict[tuple[str, str], dict] = {}
    kr_pool: dict[tuple[str, str], dict] = {}

    # FollowUp
    for c in _stream_followup(prev_snapshot):
        pool = us_pool if c["market"].upper() == "US" else kr_pool
        _merge_candidate(pool, c, "followup", strength_add=0.5)

    # EarningsActual (US 위주)
    for c in _stream_earnings(earnings):
        _merge_candidate(us_pool, c, "earnings_actual", strength_add=1.5)

    # IPO (US만)
    for c in _stream_ipo():
        _merge_candidate(us_pool, c, "ipo", strength_add=1.2)

    # SectorLeader (US + KR 각각)
    for c in _stream_sector_leader(theme_hot, "US"):
        _merge_candidate(us_pool, c, "sector_leader", strength_add=0.8)
    for c in _stream_sector_leader(theme_hot, "KR"):
        _merge_candidate(kr_pool, c, "sector_leader", strength_add=0.8)

    # TrendReversal (KR — 신규 신호; US — 기존 신호로 우선 커버 stretch)
    for c in _stream_trend_reversal("KR"):
        _merge_candidate(kr_pool, c, "trend_reversal", strength_add=1.0)
    for c in _stream_trend_reversal("US"):
        _merge_candidate(us_pool, c, "trend_reversal", strength_add=1.0)

    # KR theme linkage (미국 hot 테마 → 한국 수혜주 hardcoded 매핑)
    # screener.db 비어있어도 작동 — KR pool 활성화 보장.
    for c in _stream_kr_theme_linkage(theme_rows):
        _merge_candidate(kr_pool, c, "kr_linkage", strength_add=1.0)

    # US theme leader (미국 hot 테마 → 미국 핵심 종목 hardcoded 매핑)
    # us_screener.db 비어있어도 sector_leader 폴백 활성화.
    for c in _stream_us_theme_leader(theme_rows):
        _merge_candidate(us_pool, c, "us_theme_leader", strength_add=0.9)

    # 뉴스 mention 카운트 부착
    _annotate_news_mentions(us_pool, news)
    _annotate_news_mentions(kr_pool, news)

    # 시총 보강 — 0인 후보에 한해 fetch (KR: FDR/screener.db · US: FMP /stable/quote)
    _backfill_market_caps(us_pool, kr_pool)

    # 카테고리별 카운트 (활성화 진단)
    def _cat_breakdown(pool: dict) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in pool.values():
            for cat in (c.get("categories") or []):
                out[cat] = out.get(cat, 0) + 1
        return out

    log.info("[watchlist] 후보 풀 US=%d KR=%d (theme_hot=%s)",
             len(us_pool), len(kr_pool), theme_hot)
    log.info("[watchlist] US 카테고리 breakdown: %s", _cat_breakdown(us_pool))
    log.info("[watchlist] KR 카테고리 breakdown: %s", _cat_breakdown(kr_pool))

    # Estimate revision 보강 (US 후보 중 시총 큰 상위 20개에만 호출 — Finnhub rate 절약)
    revision_targets = sorted(
        [c for c in us_pool.values() if c.get("market_cap") or 0],
        key=lambda c: -(c.get("market_cap") or 0),
    )[:20]
    revisions = fetch_estimate_revisions.batch_revisions(
        [c["ticker"] for c in revision_targets], cap=20
    )
    for c in us_pool.values():
        rev = revisions.get(c["ticker"])
        if rev:
            if "estimate_revision" not in c["categories"]:
                c["categories"].append("estimate_revision")
            c.setdefault("_signal_raw", {})
            c["_revision"] = rev

    # Selection
    def _select(pool: dict, top_n: int) -> list[dict]:
        if not pool:
            return []
        max_cap = max((c.get("market_cap") or 0 for c in pool.values()), default=1)
        scored = sorted(pool.values(), key=lambda c: _score(c, max_cap), reverse=True)
        # FollowUp 최대 4개 강제 보장
        followups = [c for c in scored if "followup" in (c.get("categories") or [])]
        forced = followups[:4]
        forced_keys = {c["ticker"] for c in forced}
        rest = [c for c in scored if c["ticker"] not in forced_keys]
        # 카테고리 다양성 cap (한 카테고리 ≤ ceil(top_n/2))
        cat_cap = max(2, (top_n + 1) // 2)
        cat_counts: dict[str, int] = {}
        for c in forced:
            for cat in c.get("categories") or []:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        picked = list(forced)
        for c in rest:
            if len(picked) >= top_n:
                break
            cats = c.get("categories") or []
            if any(cat_counts.get(cat, 0) >= cat_cap for cat in cats):
                continue
            picked.append(c)
            for cat in cats:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        # 부족하면 cap 무시하고 채움
        for c in rest:
            if len(picked) >= top_n:
                break
            if c["ticker"] not in {p["ticker"] for p in picked}:
                picked.append(c)
        return picked[:top_n]

    us_picked = _select(us_pool, us_top)
    kr_picked = _select(kr_pool, kr_top)

    log.info("[watchlist] selection US=%d KR=%d", len(us_picked), len(kr_picked))

    # Enrichment (가장 비싼 단계 — OHLCV fetch + technical 계산)
    us_enriched = [_enrich_one(c, theme_rows, news) for c in us_picked]
    kr_enriched = [_enrich_one(c, theme_rows, news) for c in kr_picked]

    # estimate_revision 부착 (enrichment 후, US만)
    for orig, en in zip(us_picked, us_enriched):
        rev = orig.get("_revision")
        if rev:
            en["estimate_revision"] = rev

    return {"us": us_enriched, "kr": kr_enriched}


def snapshot_tickers(watchlist: dict) -> list[dict]:
    """다음날 F/U용 — snapshot에 저장될 metadata list."""
    out: list[dict] = []
    for market_key in ("us", "kr"):
        for item in watchlist.get(market_key) or []:
            out.append({
                "ticker": item.get("ticker"),
                "market": item.get("market"),
                "label": item.get("label"),
                "sector": item.get("sector"),
                "market_cap": item.get("market_cap"),
                "categories": item.get("categories"),
                "chg_5d": item.get("chg_5d"),
            })
    return out
