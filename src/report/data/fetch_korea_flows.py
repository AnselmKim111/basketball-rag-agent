"""한국 투자자별 수급 + 규모별 지수 fetch (pykrx).

리포트 [9] 한국 시장 연결의 핵심 데이터:
  - 규모별 지수 가격: KOSPI 대형(1002)/중형(1003)/소형(1004), KOSDAQ(2001 종합)
  - 투자자별 일별 순매수 시계열: 기관/외국인/개인 (KOSPI/KOSDAQ)

pykrx 버전별 시그니처 차이를 try/except로 방어. 시뮬레이션 환경 egress는
한국(pykrx/KRX)이 통과하므로 작동 기대 — Railway self-test로 검증.
"""
from __future__ import annotations

import logging
import re
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
    """규모별/대표 지수 일별 OHLCV {label: DataFrame}.

    1순위 pykrx 규모별(대형/중형/소형). 단 pykrx 지수는 KRX OTP 엔드포인트를 타는데
    일부 클라우드(예: Railway)는 egress 차단으로 항상 실패한다. 그 경우 FDR로
    KOSPI200(대형 벤치마크)·KOSPI(종합)·KOSDAQ(중소형·성장) breadth 세트를 안정 확보 —
    FDR은 가격 백본과 동일 경로라 작동 신뢰도가 높다. 결과적으로 어느 환경에서든
    한국 대형 vs 중소형 흐름을 보여주는 3~4개 차트를 보장.
    """
    out: dict[str, object] = {}
    try:
        stock = _import_pykrx()
    except Exception:
        stock = None
        log.warning("[kr_flows] pykrx import 실패 — FDR breadth 폴백 사용")
    end = date.today()
    start = end - timedelta(days=int(days * 1.5) + 10)
    if stock is not None:
        for label, code in KR_SIZE_INDICES.items():
            df = None
            # pykrx 여러 시그니처 시도 (환경별 함수명 차이)
            for attempt in (
                lambda: stock.get_index_ohlcv(_ymd(start), _ymd(end), code),
                lambda: stock.get_index_ohlcv_by_date(_ymd(start), _ymd(end), code),
            ):
                try:
                    df = attempt()
                    if df is not None and not df.empty:
                        break
                except Exception:
                    df = None
            if df is not None and not df.empty:
                ren = {"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume"}
                df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
                out[label] = df
            else:
                log.warning("[kr_flows] 지수 %s(%s) pykrx 실패", label, code)

    # 진짜 규모별(대형/중형/소형)을 2개 미만 확보 → FDR breadth 세트로 보강
    true_size = sum(1 for k in out if any(s in k for s in ("대형", "중형", "소형")))
    if true_size < 2:
        from src.report.data.fetch_prices import fetch_ohlcv
        have_kosdaq = any("KOSDAQ" in k for k in out)
        fdr_set = [
            ("KOSPI200 (대형주)", "KS200"),
            ("KOSPI (종합)", "KS11"),
            ("KOSDAQ (중소형·성장)", "KQ11"),
        ]
        for label, sym in fdr_set:
            if have_kosdaq and "KOSDAQ" in label:
                continue  # pykrx KOSDAQ 중복 방지
            df = fetch_ohlcv(sym, days=days)
            if df is not None and len(df) > 5:
                out[label] = df
        log.warning("[kr_flows] pykrx 규모별 %d개 < 2 → FDR breadth 보강", true_size)
    log.info("[kr_flows] 규모별/대표 지수 %d개 확보: %s", len(out), list(out.keys()))
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
        log.info("[kr_flows] %s 투자자 순매수 %d일 확보 (cols=%s, pykrx)", market, len(sub), keep)

    # pykrx 실패한 시장은 Naver 투자자 매매동향 폴백
    for market in ("KOSPI", "KOSDAQ"):
        if market in out:
            continue
        nv = _fetch_naver_investor_flows(market, days=days)
        if nv is not None and len(nv) > 5:
            out[market] = nv
            log.info("[kr_flows] %s 투자자 순매수 %d일 확보 (Naver 폴백)", market, len(nv))
    return out


def _fetch_naver_investor_flows(market: str, days: int = 120):
    """Naver 투자자 매매동향(investorDealTrendDay) HTML 파싱 → 일별 순매수 DataFrame.

    컬럼: 기관/외국인/개인 (순매수, 백만원 단위). 페이지당 ~20일 → bizdate로 과거 반복.
    반환: pandas.DataFrame (날짜 index asc) 또는 None.
    """
    import requests
    from bs4 import BeautifulSoup
    import pandas as pd
    from datetime import date as _date, timedelta as _td

    sosok = "01" if market == "KOSPI" else "02"
    rows: dict[str, dict] = {}  # iso_date -> {기관,외국인,개인}
    bizdate = _date.today()
    pages = days // 18 + 2
    first_diag = True
    for _ in range(pages):
        ymd = bizdate.strftime("%Y%m%d")
        url = (f"https://finance.naver.com/sise/investorDealTrendDay.naver"
               f"?bizdate={ymd}&sosok={sosok}")
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            log.warning("[kr_flows] Naver 투자자 %s fetch 실패: %s", market, e)
            break
        table = soup.find("table", {"class": "type_1"})
        if table is None:
            break
        oldest = None
        for tr in table.find_all("tr"):
            tds = [td.get_text(strip=True).replace(",", "") for td in tr.find_all("td")]
            if len(tds) < 4:
                continue
            # 첫 칸: 날짜 (YY.MM.DD 또는 YYYY.MM.DD)
            datestr = tds[0]
            mdt = re.match(r"(\d{2,4})\.(\d{1,2})\.(\d{1,2})", datestr)
            if not mdt:
                continue
            y, mo, dd = mdt.groups()
            if len(y) == 2:
                y = "20" + y
            iso = f"{y}-{int(mo):02d}-{int(dd):02d}"
            # 컬럼 순서: 날짜, 개인, 외국인, 기관계, ... (Naver 표준)
            def _num(x):
                try:
                    return float(x)
                except Exception:
                    return None
            vals = [_num(t) for t in tds[1:4]]
            if first_diag:
                log.warning("[kr_flows] DIAG Naver(%s) 첫행 tds=%s", market, tds[:6])
                first_diag = False
            if any(v is None for v in vals):
                continue
            indiv, foreign, inst = vals  # 개인, 외국인, 기관계
            rows[iso] = {"개인": indiv, "외국인": foreign, "기관": inst}
            oldest = iso
        if oldest is None:
            break
        # 다음 페이지: 가장 오래된 날짜 하루 전
        try:
            od = _date.fromisoformat(oldest) - _td(days=1)
            bizdate = od
        except Exception:
            break
        if len(rows) >= days:
            break
    if not rows:
        return None
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df[["기관", "외국인", "개인"]]
