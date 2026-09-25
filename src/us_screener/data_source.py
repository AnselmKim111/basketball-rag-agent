"""미국 시장 OHLCV/유니버스 fetch — FDR 1순위, Stooq 폴백.

한국 src/screener/data_source.py와 **동일한 함수 시그니처**를 제공해 us_screener의
incremental/validator/backfill 모듈이 무수정 재사용 가능하게 함. 단 내부 구현은
미국 데이터 소스(FDR DataReader, Stooq CSV).

데이터 소스 우선순위 (종목별 OHLCV, 2026-09-25 재설계):
  1. Yahoo chart API 직접 — 행 단위로 빈 봉(None) 스킵, 빈 봉 날짜는 gap으로 보고
  2. Nasdaq historical API — gap 날짜만 채움 (Yahoo가 특정일을 통째로 비우는 사고 대응)
  3. FDR DataReader (Yahoo backed, NaN 행 스킵) → Stooq CSV → Nasdaq 전체 — Yahoo 실패 시

실측 사고(2026-09-23~25): Yahoo가 09-22 봉을 S&P 종목 ~80%에서 None으로 반환 →
구 파서가 NaN 한 행에서 ValueError → 호출부 except가 **종목 전체를 조용히 버림** →
매일 525종목 중 458종목 base_date 누락. 한 행의 결함이 종목 전체를 날리지 않게 할 것.

유니버스: Nasdaq screener API (NYSE+NASDAQ+AMEX 전 종목, 시총·섹터 포함) 중
시총 ≥ US_SCREENER_MIN_MARKET_CAP 보통주 전체. 실패 시 S&P500(FDR)+NASDAQ100 폴백.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

log = logging.getLogger(__name__)

DEFAULT_RETRIES = 3

# 클래스 주식 심볼 매핑 (FDR StockListing 형식 → Yahoo 형식)
_SYMBOL_FIX = {
    "BRKB": "BRK-B",  # Berkshire Hathaway B
    "BFB": "BF-B",    # Brown-Forman B
}


_HTTP_UA = {"User-Agent": "Mozilla/5.0"}


def _yahoo_symbol(ticker: str) -> str:
    """DB 티커 → Yahoo 심볼 (BRKB→BRK-B, BRK.A→BRK-A)."""
    return _SYMBOL_FIX.get(ticker, ticker.replace(".", "-").replace("/", "-"))


def _nasdaq_symbol(ticker: str) -> str:
    """DB 티커 → Nasdaq API 기본 심볼 (BRKB→BRK.B, BRK-A→BRK.A)."""
    fixed = _SYMBOL_FIX.get(ticker)
    if fixed:
        return fixed.replace("-", ".")
    return ticker.replace("-", ".").replace("/", ".")


def _nasdaq_symbol_variants(ticker: str) -> list[str]:
    """Nasdaq API 경로용 심볼 후보 (URL-encoded). 클래스주는 Nasdaq 내부 표기 'BF%sl%B'가
    먼저 — 'BF.B'는 200 + 0행을 돌려준다 (실측). screener에 없는 NYSE 클래스주(LEN.B 등)는
    반대로 'X.Y'만 통하는 경우가 있어 둘 다 시도."""
    from urllib.parse import quote
    base = _nasdaq_symbol(ticker)
    if "." not in base:
        return [quote(base, safe="")]
    left, right = base.split(".", 1)
    return [quote(f"{left}%sl%{right}", safe=""), quote(base, safe="")]


# 소스별 결과 카운터 (배치 커버리지 로그·운영노트용). 스레드 안전.
import threading as _threading
from collections import Counter as _Counter
_STATS_LOCK = _threading.Lock()
SOURCE_STATS: "_Counter[str]" = _Counter()


def _stat(key: str, n: int = 1) -> None:
    with _STATS_LOCK:
        SOURCE_STATS[key] += n


def reset_source_stats() -> None:
    with _STATS_LOCK:
        SOURCE_STATS.clear()


def source_stats() -> dict:
    with _STATS_LOCK:
        return dict(SOURCE_STATS)


def _local_dt(ts: int, meta: dict) -> datetime:
    """epoch → 거래소 현지 시각. meta.gmtoffset은 '조회 시점' 오프셋이라 DST 경계를 넘는 과거
    봉에 쓰면 1시간 어긋난다 (리뷰 지적) → exchangeTimezoneName으로 변환."""
    tzname = (meta or {}).get("exchangeTimezoneName") or "America/New_York"
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(int(ts), ZoneInfo(tzname))
    except Exception:
        return datetime.fromtimestamp(int(ts) + int((meta or {}).get("gmtoffset") or 0), tz=timezone.utc)


def now_et() -> datetime:
    """미국 동부시간 현재 (DST 자동). zoneinfo 없으면 EDT(-4) 근사."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-4)))


# 정규장 마감 16:00 ET + 반영 버퍼 — 이 시각 전의 '오늘' 봉은 진행 중(미확정)
US_CLOSE_READY_HHMM = (16, 30)


def _is_unfinished_session(iso: str, now: Optional[datetime] = None) -> bool:
    now = now or now_et()
    return iso == now.date().isoformat() and (now.hour, now.minute) < US_CLOSE_READY_HHMM


def _finite(*vals) -> bool:
    for v in vals:
        if v is None:
            return False
        try:
            if not math.isfinite(float(v)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _cents_row(ticker: str, iso: str, o, h, l, c, v) -> Optional[tuple]:
    """(ticker, iso, o, h, l, c, v, None) cent 정수 행. 결측·비정상이면 None (행 단위 스킵)."""
    if not _finite(o, h, l, c, v):
        return None
    o, h, l, c, v = float(o), float(h), float(l), float(c), float(v)
    if c <= 0 or v < 0:
        return None
    return (str(ticker), iso, int(round(o * 100)), int(round(h * 100)),
            int(round(l * 100)), int(round(c * 100)), int(v), None)


def _import_fdr():
    import FinanceDataReader as fdr  # type: ignore
    return fdr


# ------------------------------------------------------------------
# 종목별 OHLCV
# ------------------------------------------------------------------
def fetch_ohlcv_by_ticker_via_fdr(ticker: str, start_iso: str, end_iso: str) -> list[tuple]:
    """단일 종목 일별 OHLCV — FDR DataReader.

    반환: (ticker, date_iso, o, h, l, c, v, value=None) — 한국과 동일 형식.
    """
    # 클래스 주식 심볼 정규화 → Yahoo 형식 (BRKB/BFB는 점도 없이 와서 특수 매핑)
    fetch_sym = _SYMBOL_FIX.get(ticker, ticker.replace(".", "-"))
    try:
        fdr = _import_fdr()
        df = fdr.DataReader(fetch_sym, start_iso, end_iso)
    except Exception as e:
        log.warning("[us_data] FDR fetch %s 실패: %s", ticker, e)
        return []
    if df is None or df.empty:
        return []
    cols = list(df.columns)
    o_c = next((c for c in ("Open", "open") if c in cols), None)
    h_c = next((c for c in ("High", "high") if c in cols), None)
    l_c = next((c for c in ("Low", "low") if c in cols), None)
    c_c = next((c for c in ("Close", "close") if c in cols), None)
    v_c = next((c for c in ("Volume", "volume") if c in cols), None)
    if not all([o_c, h_c, l_c, c_c, v_c]):
        log.warning("[us_data] FDR %s 알 수 없는 컬럼: %s", ticker, cols)
        return []
    rows: list[tuple] = []
    for idx, row in df.iterrows():
        try:
            iso = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            r = _cents_row(ticker, iso, row[o_c], row[h_c], row[l_c], row[c_c], row[v_c])
        except Exception:
            continue
        # NaN 행(Yahoo 빈 봉)은 그 행만 스킵 — 예전엔 int(NaN)이 루프 밖으로 터져 종목 전체 유실.
        # DB ohlcv는 INTEGER 컬럼 → cent 단위(×100) 저장.
        if r:
            rows.append(r)
    return rows


def fetch_ohlcv_by_ticker_via_stooq(ticker: str, start_iso: str, end_iso: str) -> list[tuple]:
    """Stooq CSV 직접 — FDR 폴백 + cross-validation 독립 소스.

    URL: https://stooq.com/q/d/l/?s={sym}.us&i=d  (Date,Open,High,Low,Close,Volume)
    반환: cent 단위 정수 (한국 data_source와 동일하게 INTEGER 저장).
    """
    import requests
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.text.strip()
    except Exception as e:
        log.warning("[us_data] Stooq fetch %s 실패: %s", ticker, e)
        return []
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        return []
    rows: list[tuple] = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        d_iso = parts[0]
        if d_iso < start_iso or d_iso > end_iso:
            continue
        try:
            r = _cents_row(ticker, d_iso, parts[1], parts[2], parts[3], parts[4], parts[5])
        except Exception:
            continue
        if r:
            rows.append(r)
    return rows


def _yahoo_chart(ticker: str, start_iso: str, end_iso: str) -> tuple[list[tuple], list[str]]:
    """Yahoo chart API 직접 호출 → (rows, gap_dates).

    gap_dates: Yahoo가 타임스탬프는 주면서 OHLCV를 None으로 비운 날짜 (거래일인데 봉 없음).
    429/5xx는 짧게 재시도. 실패 시 ([], []).
    """
    import requests
    start_dt = datetime.strptime(start_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # period2는 exclusive 성격 — 하루 여유
    end_dt = datetime.strptime(end_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{_yahoo_symbol(ticker)}"
    params = {"period1": int(start_dt.timestamp()), "period2": int(end_dt.timestamp()),
              "interval": "1d", "includePrePost": "false"}
    data = None
    last_status = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15, headers=_HTTP_UA)
            last_status = resp.status_code
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 404:
                _stat("yahoo_404")
                return [], []
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_status = type(e).__name__
            if attempt == 2:
                log.warning("[us_data] Yahoo %s 실패: %s", ticker, e)
            time.sleep(1.0 * (attempt + 1))
    if not data:
        # 429/5xx 재시도 소진도 기록 — 예전엔 조용히 빈 결과 (원인 추적 불가)
        _stat(f"yahoo_fail_{last_status}")
        log.warning("[us_data] Yahoo %s 재시도 소진 (last=%s)", ticker, last_status)
        return [], []
    try:
        res = ((data.get("chart") or {}).get("result") or [None])[0]
        if not res:
            return [], []
        ts = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        meta = res.get("meta") or {}
    except Exception:
        return [], []
    opens, highs, lows = q.get("open") or [], q.get("high") or [], q.get("low") or []
    closes, vols = q.get("close") or [], q.get("volume") or []
    rows: list[tuple] = []
    gaps: list[str] = []
    now = now_et()
    for i, t in enumerate(ts):
        # 거래소 현지 날짜 (EDT/EST) — UTC로 자르면 날짜가 밀릴 수 있음
        iso = _local_dt(t, meta).strftime("%Y-%m-%d")
        if iso < start_iso or iso > end_iso:
            continue
        if _is_unfinished_session(iso, now):
            continue  # 장중 진행 봉 — 저장하면 base_date가 앞당겨져 '누락' 재현 (리뷰 지적)
        pick = lambda arr: arr[i] if i < len(arr) else None  # noqa: E731
        r = _cents_row(ticker, iso, pick(opens), pick(highs), pick(lows), pick(closes), pick(vols))
        if r:
            rows.append(r)
        else:
            gaps.append(iso)
    # 같은 날짜 중복(장중 스냅샷 행) 방지 — 마지막 값 우선
    dedup = {r[1]: r for r in rows}
    rows = [dedup[k] for k in sorted(dedup)]
    gaps = sorted(set(gaps) - set(dedup))
    _stat("yahoo_ok")
    if gaps:
        _stat("yahoo_gap_rows", len(gaps))
    return rows, gaps


def _yahoo_session_bar(ticker: str, date_iso: str) -> Optional[tuple]:
    """Yahoo 일봉이 비었을 때 그날 정규장 OHLCV를 분봉(30m)+meta로 재구성.

    **공식 종가가 있는 날만**: meta.regularMarketTime이 date_iso(=Yahoo의 최신 세션)일 때만
    regularMarketPrice(종가 경매 포함 공식 종가)·일중 고저·거래량을 쓴다. 과거 날짜를 마지막
    30분봉 종가로 만들면 공식 종가와 수 센트 어긋나 검증에서 오탈락/오통과한다 (리뷰 실측
    7/10 종목 1센트 초과 차이) → 그 경우 None (Nasdaq 보충에 맡기거나 공백 유지).
    """
    import requests
    d = datetime.strptime(date_iso, "%Y-%m-%d")
    p1 = int((d - timedelta(days=1)).replace(tzinfo=timezone.utc).timestamp())
    p2 = int((d + timedelta(days=2)).replace(tzinfo=timezone.utc).timestamp())
    try:
        resp = requests.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{_yahoo_symbol(ticker)}",
            params={"period1": p1, "period2": p2, "interval": "30m", "includePrePost": "false"},
            timeout=15, headers=_HTTP_UA)
        resp.raise_for_status()
        res = ((resp.json().get("chart") or {}).get("result") or [None])[0] or {}
    except Exception as e:
        log.debug("[us_data] Yahoo 분봉 %s %s 실패: %s", ticker, date_iso, e)
        return None
    meta = res.get("meta") or {}
    mt = meta.get("regularMarketTime")
    if not mt or _local_dt(mt, meta).strftime("%Y-%m-%d") != date_iso:
        return None
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o_s = q.get("open") or []
    first_open = None
    for i, t in enumerate(res.get("timestamp") or []):
        local = _local_dt(t, meta)
        if local.strftime("%Y-%m-%d") != date_iso or not ((9, 30) <= (local.hour, local.minute) < (16, 0)):
            continue
        v = o_s[i] if i < len(o_s) else None
        if _finite(v):
            first_open = v
            break
    if first_open is None:
        return None
    r = _cents_row(ticker, date_iso, first_open, meta.get("regularMarketDayHigh"),
                   meta.get("regularMarketDayLow"), meta.get("regularMarketPrice"),
                   meta.get("regularMarketVolume"))
    if r:
        _stat("yahoo_session_fill")
    return r


def last_trade_date(ticker: str) -> Optional[str]:
    """Yahoo meta의 마지막 체결일 (거래소 현지 날짜). 실패 시 None."""
    import requests
    try:
        resp = requests.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{_yahoo_symbol(ticker)}",
            params={"range": "5d", "interval": "1d"}, timeout=15, headers=_HTTP_UA)
        resp.raise_for_status()
        meta = ((((resp.json().get("chart") or {}).get("result")) or [{}])[0] or {}).get("meta") or {}
        mt = meta.get("regularMarketTime")
        if not mt:
            return None
        return _local_dt(mt, meta).strftime("%Y-%m-%d")
    except Exception:
        return None


def fetch_ohlcv_by_ticker_via_yahoo(ticker: str, start_iso: str, end_iso: str) -> list[tuple]:
    return _yahoo_chart(ticker, start_iso, end_iso)[0]


def _money(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def fetch_ohlcv_by_ticker_via_nasdaq(ticker: str, start_iso: str, end_iso: str) -> list[tuple]:
    """Nasdaq historical API — Yahoo와 독립 소스. 당일 봉은 보통 다음날 반영(1일 lag).

    https://api.nasdaq.com/api/quote/{SYM}/historical?assetclass=stocks&fromdate&todate&limit
    """
    import requests
    # Nasdaq은 fromdate == todate를 거부("Provided date is less than from date") —
    # 양쪽 하루씩 넓혀 요청하고 아래에서 원래 구간으로 필터.
    q_from = (datetime.strptime(start_iso, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    q_to = (datetime.strptime(end_iso, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    table: list = []
    for sym in _nasdaq_symbol_variants(ticker):
        try:
            resp = requests.get(
                f"https://api.nasdaq.com/api/quote/{sym}/historical",
                params={"assetclass": "stocks", "fromdate": q_from, "todate": q_to,
                        "limit": 9999},
                timeout=20, headers={**_HTTP_UA, "Accept": "application/json"})
            resp.raise_for_status()
            table = ((resp.json().get("data") or {}).get("tradesTable") or {}).get("rows") or []
        except Exception as e:
            log.debug("[us_data] Nasdaq hist %s(%s) 실패: %s", ticker, sym, e)
            table = []
        if table:
            break
    if not table:
        _stat("nasdaq_empty")
        return []
    rows: list[tuple] = []
    for x in table:
        try:
            m, d, y = str(x.get("date", "")).split("/")
            iso = f"{y}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            continue
        if iso < start_iso or iso > end_iso:
            continue
        r = _cents_row(ticker, iso, _money(x.get("open")), _money(x.get("high")),
                       _money(x.get("low")), _money(x.get("close")),
                       _money(x.get("volume")))
        if r:
            rows.append(r)
    rows.sort(key=lambda r: r[1])
    return rows


def fetch_ohlcv_by_ticker_via_naver(ticker: str, start_iso: str, end_iso: str,
                                    known_dates: Optional[Iterable[str]] = None) -> list[tuple]:
    """한국 모듈 호환 진입점 (이름 유지) — 미국 종목별 OHLCV 통합 체인.

    1) Yahoo 일봉 직접 → 빈 봉(gap) 중 known_dates(DB 보유)에 없는 날짜만 보충:
       Nasdaq historical(1일 lag) → 남은 최근 gap(end 기준 5일 내)은 Yahoo 분봉 재구성
    2) Yahoo 실패 시 Stooq → Nasdaq 전체
    FDR은 체인에서 제외: 같은 Yahoo 엔드포인트라 Yahoo 실패 직후 이득이 없고 소켓 timeout이
    없어 병렬 풀 전체를 붙잡을 수 있다 (리뷰 지적). 함수 자체는 다른 호출부용으로 유지.
    """
    rows, gaps = _yahoo_chart(ticker, start_iso, end_iso)
    if rows:
        need = set(gaps) - set(known_dates or ())
        if need:
            wide_from = (datetime.strptime(min(need), "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
            fill = [r for r in fetch_ohlcv_by_ticker_via_nasdaq(ticker, wide_from, max(need))
                    if r[1] in need]
            got = {r[1] for r in fill}
            if fill:
                _stat("nasdaq_fill_rows", len(fill))
            recent_floor = (datetime.strptime(end_iso, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
            for iso in sorted(need - got):
                if iso >= recent_floor:          # 공식 종가를 줄 수 있는 최신 세션 후보만
                    r = _yahoo_session_bar(ticker, iso)
                    if r:
                        fill.append(r)
            if fill:
                have = {r[1] for r in rows}
                rows = sorted(rows + [r for r in fill if r[1] not in have], key=lambda r: r[1])
        return rows
    for fn in (fetch_ohlcv_by_ticker_via_stooq, fetch_ohlcv_by_ticker_via_nasdaq):
        try:
            rows = fn(ticker, start_iso, end_iso)
        except Exception as e:  # 한 소스 예외가 체인을 끊지 않게
            log.warning("[us_data] %s %s 예외: %s", fn.__name__, ticker, e)
            rows = []
        if rows:
            _stat(f"fallback_{fn.__name__.rsplit('_', 1)[-1]}")
            return [r for r in rows if not _is_unfinished_session(r[1])]
    return []


def fetch_market_ohlcv_by_date(date_str: str, market: str = "ALL") -> list[tuple]:
    """미국은 date-batch 미지원 (종목별 fetch만). 빈 리스트 반환 → 호출자가 ticker-batch."""
    return []


# ------------------------------------------------------------------
# 유니버스 (S&P500 + Nasdaq100)
# ------------------------------------------------------------------
def _fdr_listing(name: str):
    try:
        fdr = _import_fdr()
        return fdr.StockListing(name)
    except Exception:
        log.exception("[us_data] FDR.StockListing(%s) 실패", name)
        return None


# NASDAQ100 종목 (2025 기준). FDR StockListing('NASDAQ100') 미지원 환경 대비 하드코딩.
# S&P500과 합집합 → 중복 자동 제거. 대부분 S&P500 포함, 고유 종목(외국계 ADR 등) 보강.
_NASDAQ100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "APP", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR",
    "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO",
    "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DLTR", "DXCM", "EXC",
    "FANG", "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX",
    "ILMN", "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU",
    "MAR", "MCHP", "MDB", "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MU",
    "NFLX", "NVDA", "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD",
    "PEP", "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM", "TMUS",
    "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]


# ------------------------------------------------------------------
# Nasdaq screener — 미국 상장 전 종목 (NYSE/NASDAQ/AMEX) + 시총·섹터
# ------------------------------------------------------------------
_NASDAQ_SCREENER_CACHE: dict = {"at": 0.0, "rows": None}
# 보통주가 아닌 상품(우선주·채권·워런트·유닛·권리) — 이 종목들도 발행사 시총이 그대로 붙어 옴
# 채권·워런트·권리·구조화상품 — 어떤 경우에도 보통주 아님
_DEBT_RE = re.compile(
    r"\d+(\.\d+)?\s*%|\bnotes?\b|debenture|\bbonds?\b|subordinated|\bdue\s+(\w+\s+\d+\s+)?20\d\d\b"
    r"|\bwarrants?\b|contingent value right|\brights?\s*$|\bstrats\b|trust for\b|trust certificates"
    r"|tangible equity units?|corporate units?|\bzones\b|exchangeable subordinated",
    re.I,
)
# 보통 지분 표기 — 이게 있으면 이름에 'Preferred Bank'·'Series A Common' 등이 있어도 포함
_COMMON_RE = re.compile(
    r"common stock|common shares|ordinary shares?|capital stock|american depositar|american depositor"
    r"|\badrs?\b|\bads\b|registry shares|voting shares|class [a-z] shares|common units"
    r"|limited partner|partnership units|\bl\.?p\.?\s+units",
    re.I,
)
# 우선주·우선주 예탁증서 (American Depositary는 _COMMON_RE가 먼저 잡음)
_PREF_RE = re.compile(
    r"preferred|preference|depositary shares|depository shares|cumulative|redeemable|perpetual"
    r"|mandator(y|ily) convertible",
    re.I,
)
# 폐쇄형 펀드(CEF) — 이름에 Fund, 또는 금융·신탁 업종의 Trust/beneficial interest
_FUND_RE = re.compile(r"\bfund\b", re.I)
_CEF_TRUST_RE = re.compile(r"\btrust\b|beneficial interest", re.I)
_CEF_INDUSTRIES = {
    "investment managers", "finance companies", "trusts except educational religious and charitable",
    "finance/investors services", "investment bankers/brokers/service", "",
}
# 유닛: SPAC 유닛은 제외하되 MLP 'Common Units'(L.P./Partners)는 보통 지분으로 포함
_UNIT_RE = re.compile(r"\bunits?\b", re.I)
_MLP_RE = re.compile(r"\bl\.?\s?p\.?(\s|$)|partners|limited partner|limited liability company", re.I)
_NAME_SUFFIX_RE = re.compile(
    r"\s+(Class [A-Z] )?(Common Stock|Ordinary Shares|Common Shares|Class [A-Z] Ordinary Shares"
    r"|American Depositary Shares.*|American Depository Shares.*|Depositary Shares.*"
    r"|New York Registry Shares|Registered Shares|Shares of Beneficial Interest.*"
    r"|Common Units.*|\(.*\))\s*$",
    re.I,
)


def _nasdaq_to_db_symbol(sym: str) -> str:
    """Nasdaq 표기 → DB 티커. 기존 이력 연속성 위해 BRK/B→BRKB, BF/B→BFB 유지."""
    sym = sym.strip().upper()
    special = {"BRK/B": "BRKB", "BF/B": "BFB"}
    if sym in special:
        return special[sym]
    return sym.replace("/", ".")


def _nasdaq_screener_rows(max_age_s: int = 900) -> list[dict]:
    """Nasdaq screener 전 종목 (메모 15분). 실패 시 []."""
    now = time.time()
    if _NASDAQ_SCREENER_CACHE["rows"] is not None and now - _NASDAQ_SCREENER_CACHE["at"] < max_age_s:
        return _NASDAQ_SCREENER_CACHE["rows"]
    import requests
    rows: list[dict] = []
    for attempt in range(3):
        try:
            resp = requests.get(
                "https://api.nasdaq.com/api/screener/stocks",
                params={"tableonly": "true", "download": "true"},
                timeout=30, headers={**_HTTP_UA, "Accept": "application/json"})
            resp.raise_for_status()
            rows = ((resp.json().get("data") or {}).get("rows")) or []
            if rows:
                break
        except Exception as e:
            log.warning("[us_data] Nasdaq screener 실패 attempt=%d: %s", attempt + 1, e)
        time.sleep(2 * (attempt + 1))
    # 실패도 짧게 캐시 — 같은 refresh 안에서 universe·raw_count·caps가 각각 재시도하며
    # 수 분씩 잡아먹지 않게 (리뷰 지적). 실패 캐시는 max_age_s 대신 5분.
    _NASDAQ_SCREENER_CACHE.update(at=now if rows else now - max(0, max_age_s - 300), rows=rows)
    return rows


def _cap_of(row: dict) -> int:
    try:
        return int(float(row.get("marketCap") or 0))
    except (TypeError, ValueError):
        return 0


def is_common_equity(name: str, symbol: str, industry: str = "") -> bool:
    """보통주(ADR·클래스주·REIT·MLP 포함)만 True. 우선주·채권·워런트·권리·유닛·CEF 제외.

    판정 순서: 채권/파생 → 제외 · 보통지분 표기 → 포함(단 CEF 제외) · 우선주 표기 → 제외.
    발행사 시총이 우선주·채권 행에도 그대로 붙어 오므로 시총 필터만으로는 걸러지지 않는다.
    """
    if not symbol or "^" in symbol or len(symbol.replace("/", "")) > 6:
        return False
    n = (name or "").strip()
    ind = (industry or "").strip().lower()
    if _DEBT_RE.search(n):
        return False
    is_cef = bool(_FUND_RE.search(n)) or (ind in _CEF_INDUSTRIES and bool(_CEF_TRUST_RE.search(n)))
    if is_cef:
        return False
    if _COMMON_RE.search(n):
        return True
    if _PREF_RE.search(n):
        return False
    if _UNIT_RE.search(n) and not _MLP_RE.search(n):
        return False
    return True


def clean_company_name(name: str) -> str:
    n = (name or "").strip()
    for _ in range(2):
        n = _NAME_SUFFIX_RE.sub("", n).strip()
    return n.rstrip(" ,.") or (name or "").strip()


def _effective_caps(rows: list[dict]) -> dict[str, int]:
    """{nasdaq_symbol: cap}. 클래스주('BF/B','HEI/A')는 Nasdaq이 시총을 비우는 경우가 많아
    같은 회사 다른 클래스(베이스 심볼) 시총을 상속 — 시총은 회사 단위 기준이므로 타당.
    (실측 2026-09-25: BF/A·BF/B·HEI/A·AKO/A·AKO/B 시총 공란 → 필터에서 조용히 탈락)"""
    base_cap: dict[str, int] = {}
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        cap = _cap_of(r)
        if sym and cap > 0:
            b = sym.split("/")[0]
            base_cap[b] = max(base_cap.get(b, 0), cap)
    out: dict[str, int] = {}
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        cap = _cap_of(r) or base_cap.get(sym.split("/")[0], 0)
        if cap > 0:
            out[sym] = cap
    return out


_OTHERLISTED_CACHE: dict = {"at": 0.0, "rows": None}
_BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/120 Safari/537.36",
               "Accept": "text/plain,*/*"}


def _otherlisted_rows(max_age_s: int = 3600) -> list[dict]:
    """nasdaqtrader SymDir otherlisted.txt (NYSE/NYSE American/Arca 상장) — ETF·테스트 제외.

    Nasdaq screener에서 통째로 빠지는 NYSE 클래스주(MOG.A/B, LEN.B, UHAL.B, GEF.B, TAP.A,
    MKC.V ...)를 보충하기 위한 원장. 기본 UA는 406 → 브라우저 UA 필요 (실측).
    """
    now = time.time()
    if _OTHERLISTED_CACHE["rows"] is not None and now - _OTHERLISTED_CACHE["at"] < max_age_s:
        return _OTHERLISTED_CACHE["rows"]
    import requests
    rows: list[dict] = []
    try:
        resp = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
                            timeout=30, headers=_BROWSER_UA)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        head = lines[0].split("|") if lines else []
        idx = {k: i for i, k in enumerate(head)}
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) < len(head) or line.startswith("File Creation Time"):
                continue
            if parts[idx.get("ETF", 4)] == "Y" or parts[idx.get("Test Issue", 6)] == "Y":
                continue
            rows.append({"symbol": parts[idx.get("ACT Symbol", 0)].strip().upper(),
                         "name": parts[idx.get("Security Name", 1)].strip()})
    except Exception as e:
        log.warning("[us_data] otherlisted.txt 실패: %s", e)
    if rows:
        _OTHERLISTED_CACHE.update(at=now, rows=rows)
    return rows


def _nasdaq_summary_cap(ticker: str) -> int:
    """Nasdaq quote summary의 MarketCap (screener에 없는 클래스주 보충용). 실패 시 0."""
    import requests
    for sym in _nasdaq_symbol_variants(ticker):
        try:
            resp = requests.get(f"https://api.nasdaq.com/api/quote/{sym}/summary",
                                params={"assetclass": "stocks"}, timeout=15,
                                headers={**_HTTP_UA, "Accept": "application/json"})
            sd = ((resp.json().get("data") or {}).get("summaryData") or {})
            v = (sd.get("MarketCap") or {}).get("value")
            cap = int(float(str(v).replace(",", ""))) if v and v != "N/A" else 0
        except Exception:
            cap = 0
        if cap > 0:
            return cap
    return 0


def _sec_cap(ticker: str) -> int:
    """SEC 발행주식수 × Yahoo 현재가 — Nasdaq에 시총이 없는 클래스주(MOG.A 등) 최후 수단."""
    try:
        from src.us_screener import fundamentals
        shares = (fundamentals.ticker_fundamentals(ticker) or {}).get("shares")
        if not shares:
            return 0
        rows, _ = _yahoo_chart(ticker, (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"),
                               datetime.now().strftime("%Y-%m-%d"))
        return int(float(shares) * rows[-1][5] / 100) if rows else 0
    except Exception as e:
        log.debug("[us_data] SEC cap %s 실패: %s", ticker, e)
        return 0


def nasdaq_screener_raw_count() -> int:
    return len(_nasdaq_screener_rows())


def fetch_nasdaq_universe(min_cap: float, always_include: Iterable[str] = ()) -> list[dict]:
    """시총 ≥ min_cap 보통주 전체: [{ticker, name, market_cap, sector, industry}]. 실패 시 [].

    always_include: DB 티커 집합(지수 멤버 등) — 보통주면 시총 공란/미달이어도 포함.
    """
    rows = _nasdaq_screener_rows()
    caps = _effective_caps(rows)
    keep = set(always_include or ())
    # 지수 멤버의 다른 클래스(BF/B 멤버 → BF/A)도 시총 공란이면 함께 포함
    keep_bases = {str(r.get("symbol") or "").strip().upper().split("/")[0] for r in rows
                  if _nasdaq_to_db_symbol(str(r.get("symbol") or "")) in keep}
    out: dict[str, dict] = {}
    if not rows:
        return []          # screener 실패 → 보충(summary/SEC 수십 회)도 건너뛰고 폴백으로
    class_budget = 20
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        name = str(r.get("name") or "")
        if not is_common_equity(name, sym, r.get("industry") or ""):
            continue
        t = _nasdaq_to_db_symbol(sym)
        cap = caps.get(sym, 0)
        sibling_of_member = "/" in sym and not cap and sym.split("/")[0] in keep_bases
        if "/" in sym and not cap and not sibling_of_member and class_budget > 0:
            # 클래스주 시총 공란 + 상속할 베이스 행 없음 (AKO/B·CRD/A 실측) → SEC 주식수×가격
            class_budget -= 1
            cap = _sec_cap(t) or _nasdaq_summary_cap(t)
        if cap < min_cap and t not in keep and not sibling_of_member:
            continue
        out[t] = {"ticker": t, "name": clean_company_name(name), "market_cap": cap or None,
                  "sector": (r.get("sector") or "").strip(),
                  "industry": (r.get("industry") or "").strip()}

    # 보충: screener에 없는 NYSE 클래스주 (otherlisted.txt 'X.Y') — 베이스 심볼 시총 상속,
    # 없으면 Nasdaq summary 조회(호출 상한). 예: LEN→LEN.B, UHAL→UHAL.B, MOG.A(summary).
    screener_syms = {str(r.get("symbol") or "").strip().upper().replace("/", ".") for r in rows}
    summary_budget = 60
    added = []
    for o in _otherlisted_rows():
        sym = o["symbol"]
        if "." not in sym or sym in screener_syms:
            continue
        t = _nasdaq_to_db_symbol(sym.replace(".", "/"))
        if t in out or not is_common_equity(o["name"], sym):
            continue
        cap = caps.get(sym.split(".")[0], 0)
        if not cap and summary_budget > 0:
            summary_budget -= 1
            cap = _nasdaq_summary_cap(t) or _sec_cap(t)
        if cap >= min_cap or t in keep:
            out[t] = {"ticker": t, "name": clean_company_name(o["name"]), "market_cap": cap or None,
                      "sector": "", "industry": ""}
            added.append(t)
    # 보충 목록 영속화: otherlisted.txt·cap 조회가 실패한 날 전날 목록으로 대체 — 하루 실패로
    # LEN.B·PBR.A 같은 대형 클래스주가 비활성화되지 않게 (리뷰 지적)
    try:
        import json as _json
        from src.us_screener import db as _db
        if added:
            _db.meta_set("us_class_supplement", _json.dumps(
                [{k: out[t][k] for k in ("ticker", "name", "market_cap")} for t in added]))
        elif not _otherlisted_rows():
            prev = _json.loads(_db.meta_get("us_class_supplement") or "[]")
            for it in prev:
                if it.get("ticker") and it["ticker"] not in out:
                    out[it["ticker"]] = {**it, "sector": "", "industry": ""}
                    added.append(it["ticker"])
            if prev:
                log.warning("[us_data] otherlisted 실패 — 전일 클래스주 보충 %d 재사용", len(prev))
    except Exception:
        log.exception("[us_data] 클래스주 보충 영속화 실패")
    if added:
        log.info("[us_data] screener 누락 클래스주 보충 %d: %s", len(added), ", ".join(added[:30]))
    return list(out.values())


def fetch_us_tickers() -> list[tuple]:
    """S&P500(FDR) + Nasdaq100(하드코딩) 합집합. 반환: (symbol, name, index_label).

    index_label = 'S&P500' | 'NASDAQ100' (둘 다면 S&P500 우선).
    유니버스 본체는 fetch_nasdaq_universe — 이 함수는 지수 라벨·폴백용.
    """
    out: dict[str, tuple] = {}
    # 1) S&P500 — FDR StockListing
    df = _fdr_listing("S&P500")
    if df is not None and not df.empty:
        cols = list(df.columns)
        sym_c = next((c for c in ("Symbol", "Code", "Ticker") if c in cols), None)
        name_c = next((c for c in ("Name", "name") if c in cols), None)
        if sym_c:
            for _, row in df.iterrows():
                try:
                    sym = str(row[sym_c]).strip().upper()
                    nm = str(row[name_c]).strip() if name_c else sym
                except Exception:
                    continue
                if sym and sym != "NAN" and sym not in out:
                    out[sym] = (sym, nm, "S&P500")
    # 2) NASDAQ100 — 하드코딩 (S&P500 미포함 종목만 추가)
    for sym in _NASDAQ100:
        if sym not in out:
            out[sym] = (sym, sym, "NASDAQ100")
    log.info("[us_data] 지수 멤버 fetch: %d종목 (S&P500 FDR + NASDAQ100 하드코딩)", len(out))
    return list(out.values())


def fetch_market_caps() -> dict[str, int]:
    """{ticker: market_cap(USD)} — Nasdaq screener (전 종목). FDR S&P500 리스트엔 시총 컬럼 없음
    (2026-09 실측 cols=[Symbol, Name, Sector, Industry])."""
    out: dict[str, int] = {}
    for sym, cap in _effective_caps(_nasdaq_screener_rows()).items():
        out.setdefault(_nasdaq_to_db_symbol(sym), cap)
    if out:
        log.info("[us_data] market_cap fetch: %d종목 (Nasdaq screener)", len(out))
    else:
        log.warning("[us_data] market_cap fetch 0 — Nasdaq screener 실패")
    return out


def fetch_sectors() -> dict[str, str]:
    """{symbol: sector} — FDR StockListing의 Sector/Industry 컬럼 (미국은 GICS 제공)."""
    out: dict[str, str] = {}
    for idx_name in ("S&P500",):  # NASDAQ100 StockListing 미지원 → S&P500만
        df = _fdr_listing(idx_name)
        if df is None or df.empty:
            continue
        cols = list(df.columns)
        sym_c = next((c for c in ("Symbol", "Code", "Ticker") if c in cols), None)
        sec_c = next((c for c in ("Sector", "Industry", "sector", "industry") if c in cols), None)
        if not sym_c or not sec_c:
            continue
        for _, row in df.iterrows():
            try:
                sym = str(row[sym_c]).strip().upper()
                sec = str(row[sec_c]).strip()
            except Exception:
                continue
            if sym and sec and sec.lower() != "nan" and sym not in out:
                out[sym] = sec
    if out:
        log.info("[us_data] sector fetch: %d종목", len(out))
    return out


def apply_sector_keywords(name: str) -> Optional[str]:
    """미국은 FDR이 GICS 섹터를 제공하므로 keyword 휴리스틱 불필요."""
    return None


# ------------------------------------------------------------------
# 거래일 helper
# ------------------------------------------------------------------
def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def last_n_business_days(end: date, n: int) -> list[str]:
    out: list[str] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    out.reverse()
    return out
