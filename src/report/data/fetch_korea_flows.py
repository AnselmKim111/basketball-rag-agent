"""한국 투자자별 수급 + 규모별 지수 fetch (pykrx).

리포트 [9] 한국 시장 연결의 핵심 데이터:
  - 규모별 지수 가격: KOSPI 대형(1002)/중형(1003)/소형(1004), KOSDAQ(2001 종합)
  - 투자자별 일별 순매수 시계열: 기관/외국인/개인 (KOSPI/KOSDAQ)

pykrx 버전별 시그니처 차이를 try/except로 방어. 시뮬레이션 환경 egress는
한국(pykrx/KRX)이 통과하므로 작동 기대 — Railway self-test로 검증.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)


def _import_pykrx():
    from pykrx import stock  # type: ignore
    return stock


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


# 규모별 지수 코드 (pykrx KRX 지수)
KR_SIZE_INDICES = {
    "KOSPI 대형주": "1002",
    "KOSPI 중형주": "1003",
    "KOSPI 소형주": "1004",
    "KOSDAQ": "2001",
}


def fetch_size_index_ohlcv(days: int = 365) -> dict[str, object]:
    """규모별 지수 일별 OHLCV {label: DataFrame}. pykrx get_index_ohlcv."""
    out: dict[str, object] = {}
    try:
        stock = _import_pykrx()
    except Exception:
        log.exception("[kr_flows] pykrx import 실패")
        return out
    end = date.today()
    start = end - timedelta(days=int(days * 1.5) + 10)
    for label, code in KR_SIZE_INDICES.items():
        try:
            df = stock.get_index_ohlcv(_ymd(start), _ymd(end), code)
            if df is not None and not df.empty:
                # 컬럼 한글 → 표준
                ren = {"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume"}
                df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
                out[label] = df
        except Exception:
            log.warning("[kr_flows] 지수 %s(%s) 실패", label, code)
    log.info("[kr_flows] 규모별 지수 %d개 확보", len(out))
    return out


def fetch_investor_flows(days: int = 120) -> dict[str, object]:
    """시장별(KOSPI/KOSDAQ) 투자자별 일별 순매수 시계열.

    반환: {"KOSPI": DataFrame(날짜 index, 기관/외국인/개인 순매수), "KOSDAQ": ...}
    pykrx get_market_trading_value_by_date(fromdate, todate, market, detail) 시도.
    """
    out: dict[str, object] = {}
    try:
        stock = _import_pykrx()
    except Exception:
        log.exception("[kr_flows] pykrx import 실패")
        return out
    end = date.today()
    start = end - timedelta(days=int(days * 1.6) + 10)

    for market in ("KOSPI", "KOSDAQ"):
        df = None
        # 시그니처 1: get_market_trading_value_by_date (detail=True → 투자자별 순매수)
        for attempt in (
            lambda: stock.get_market_trading_value_by_date(_ymd(start), _ymd(end), market, detail=True),
            lambda: stock.get_market_trading_value_by_date(_ymd(start), _ymd(end), market),
        ):
            try:
                df = attempt()
                if df is not None and not df.empty:
                    break
            except Exception:
                df = None
        if df is None or df.empty:
            log.warning("[kr_flows] %s 투자자 순매수 미확보", market)
            continue
        # 컬럼 정규화: 기관합계/외국인합계/개인 → 표준 라벨
        colmap = {}
        for c in df.columns:
            cs = str(c)
            if "기관" in cs:
                colmap[c] = "기관"
            elif "외국인" in cs:
                colmap[c] = "외국인"
            elif "개인" in cs:
                colmap[c] = "개인"
        sub = df.rename(columns=colmap)
        keep = [c for c in ("기관", "외국인", "개인") if c in sub.columns]
        if not keep:
            log.warning("[kr_flows] %s 투자자 컬럼 인식 실패: %s", market, list(df.columns))
            continue
        out[market] = sub[keep]
        log.info("[kr_flows] %s 투자자 순매수 %d일 확보 (cols=%s)", market, len(sub), keep)
    return out
