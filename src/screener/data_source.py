"""KRX 일별 OHLCV/종목리스트 fetch — pykrx 우선, FinanceDataReader 폴백.

설계:
  - 모든 fetch는 retry + sleep으로 rate limit 회피
  - blocking I/O이므로 호출자가 run_in_executor로 감쌀 것
  - pykrx 실패 시 FinanceDataReader로 폴백 (KRX endpoint 변경/차단 대응)
  - 오늘 날짜에 데이터 없으면 빈 결과 반환 (휴장일 시그널)
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

log = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_S = 1.5
DEFAULT_RETRIES = 3


def _import_pykrx():
    """무거운 import는 함수 안에서 — orchestrator 부팅 실패 방지."""
    from pykrx import stock  # type: ignore
    return stock


def _import_fdr():
    import FinanceDataReader as fdr  # type: ignore
    return fdr


def fetch_market_ohlcv_by_date(date_str: str, market: str = "ALL") -> list[tuple]:
    """date_str(YYYYMMDD)에 해당하는 모든 종목 OHLCV 반환.

    반환 row: (ticker, date_iso, open, high, low, close, volume, value)
    빈 결과 = 휴장일 또는 데이터 소스 실패. FDR은 종목별이라 여기서는 pykrx만.
    pykrx 일관 실패시 backfill에서 ticker-batch 모드로 자동 전환.
    """
    stock = _import_pykrx()
    last_err = None
    for attempt in range(DEFAULT_RETRIES):
        try:
            df = stock.get_market_ohlcv(date_str, market=market)
            if df is None or df.empty:
                return []
            iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            cols = list(df.columns)
            # pykrx 버전별 컬럼 호환 (한글 또는 영어)
            col_map = {
                "open": next((c for c in ("시가", "Open", "open") if c in cols), None),
                "high": next((c for c in ("고가", "High", "high") if c in cols), None),
                "low":  next((c for c in ("저가", "Low", "low") if c in cols), None),
                "close": next((c for c in ("종가", "Close", "close") if c in cols), None),
                "volume": next((c for c in ("거래량", "Volume", "volume") if c in cols), None),
                "value": next((c for c in ("거래대금", "Value", "value") if c in cols), None),
            }
            if not all(col_map[k] for k in ("open", "high", "low", "close", "volume")):
                log.warning("[data_source] %s 알 수 없는 컬럼 구조: %s", date_str, cols)
                return []
            rows: list[tuple] = []
            for ticker, row in df.iterrows():
                try:
                    o = int(row[col_map["open"]])
                    h = int(row[col_map["high"]])
                    l = int(row[col_map["low"]])
                    c = int(row[col_map["close"]])
                    v = int(row[col_map["volume"]])
                    val = int(row[col_map["value"]]) if col_map["value"] else None
                except Exception:
                    continue
                if c <= 0 or v < 0:
                    continue
                rows.append((str(ticker), iso, o, h, l, c, v, val))
            return rows
        except Exception as e:
            last_err = e
            log.warning("[data_source] fetch %s 실패 attempt=%d err=%s", date_str, attempt + 1, e)
            time.sleep(2 ** attempt)
    log.error("[data_source] fetch %s 최종 실패 err=%s", date_str, last_err)
    return []


def fetch_ohlcv_by_ticker_via_naver(
    ticker: str, start_iso: str, end_iso: str
) -> list[tuple]:
    """단일 종목 일별 OHLCV — Naver Finance siseJson API.

    pykrx/FDR 환경 미스매치 (시뮬레이션 시간) 우회용. Naver가 가장 최근 정규장
    종가 데이터를 정확히 제공.

    응답 형식 (JS array literal):
      [['날짜','시가','고가','저가','종가','거래량','외국인소진율'],
       ['20260507',271000,272000,270000,271500,12345678,xxx],
       ...]

    반환: (ticker, date_iso, o, h, l, c, v, value=None) — fetch_ohlcv_by_ticker_via_fdr와 호환.
    """
    import requests
    start_ymd = start_iso.replace("-", "")
    end_ymd = end_iso.replace("-", "")
    url = (
        "https://api.finance.naver.com/siseJson.naver"
        f"?symbol={ticker}&requestType=1"
        f"&startTime={start_ymd}&endTime={end_ymd}&timeframe=day"
    )
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.text.strip()
    except Exception as e:
        log.warning("[data_source] Naver fetch %s 실패: %s", ticker, e)
        return []
    # JS array literal → JSON: 작은따옴표를 큰따옴표로, trailing whitespace/newline 제거.
    # 헤더 row는 모든 컬럼이 문자열이라 그대로 둠. 데이터 row는 숫자 + ' '단순 전환.
    import json, re
    norm = text.replace("'", '"')
    norm = re.sub(r"\s+", "", norm)
    norm = norm.rstrip(",")
    try:
        data = json.loads(norm)
    except Exception as e:
        log.warning("[data_source] Naver %s JSON 파싱 실패: %s preview=%s", ticker, e, text[:200])
        return []
    if not data or len(data) < 2:
        return []
    # 첫 row가 헤더 (한글), 나머지가 데이터
    rows: list[tuple] = []
    for row in data[1:]:
        try:
            ymd = str(row[0])  # '20260508'
            iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
            o, h, l, c, v = int(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5])
        except Exception:
            continue
        if c <= 0 or v < 0:
            continue
        rows.append((str(ticker), iso, o, h, l, c, v, None))
    return rows


def fetch_ohlcv_by_ticker_via_fdr(
    ticker: str, start_iso: str, end_iso: str
) -> list[tuple]:
    """단일 종목 OHLCV 1년치 (FDR — Naver/Yahoo backed).

    반환: (ticker, date_iso, open, high, low, close, volume, value).
    실패 시 빈 리스트.
    """
    try:
        fdr = _import_fdr()
        df = fdr.DataReader(ticker, start_iso, end_iso)
    except Exception as e:
        log.warning("[data_source] FDR fetch %s 실패 err=%s", ticker, e)
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
        log.warning("[data_source] FDR %s 알 수 없는 컬럼: %s", ticker, cols)
        return []
    rows: list[tuple] = []
    for idx, row in df.iterrows():
        try:
            iso = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            o = int(row[o_c]); h = int(row[h_c]); l = int(row[l_c])
            c = int(row[c_c]); v = int(row[v_c])
        except Exception:
            continue
        if c <= 0 or v < 0:
            continue
        rows.append((str(ticker), iso, o, h, l, c, v, None))
    return rows


def fetch_market_caps(date_str: str | None = None) -> dict[str, int]:
    """KOSPI+KOSDAQ 전 종목 시가총액 (원 단위) — pykrx 시도, 실패 시 FDR 폴백.

    date_str=None이면 최근 영업일. 빈 dict면 fetch 실패.
    """
    # pykrx 시도 (15일 backoff — 미래 날짜/연말 휴장 대응)
    stock = _import_pykrx()
    if date_str is None:
        d = date.today()
        for _ in range(15):
            ds = d.strftime("%Y%m%d")
            if d.weekday() < 5:  # 평일만
                try:
                    df = stock.get_market_cap(ds, market="ALL")
                    if df is not None and not df.empty:
                        date_str = ds
                        break
                except Exception:
                    pass
            d -= timedelta(days=1)
        else:
            df = None
    else:
        try:
            df = stock.get_market_cap(date_str, market="ALL")
            if df is None or df.empty:
                df = None
        except Exception as e:
            log.warning("[data_source] pykrx market_cap %s 실패: %s", date_str, e)
            df = None

    if df is not None:
        cols = list(df.columns)
        cap_c = next((c for c in ("시가총액", "MarketCap", "market_cap") if c in cols), None)
        if cap_c:
            out: dict[str, int] = {}
            for ticker, row in df.iterrows():
                try:
                    cap = int(row[cap_c])
                except Exception:
                    continue
                if cap > 0:
                    out[str(ticker)] = cap
            if out:
                log.info("[data_source] pykrx market_cap fetched %d종목 (date=%s)", len(out), date_str)
                return out

    # FDR 폴백
    log.warning("[data_source] pykrx market_cap 빈 결과 → FDR StockListing 폴백")
    return _fetch_market_caps_via_fdr()


def _fetch_market_caps_via_fdr() -> dict[str, int]:
    """FDR StockListing의 Marcap 컬럼으로 시총 fetch."""
    try:
        fdr = _import_fdr()
    except Exception:
        log.exception("[data_source] FDR import 실패")
        return {}
    out: dict[str, int] = {}
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
        except Exception:
            log.exception("[data_source] FDR.StockListing(%s) 실패", market)
            continue
        if df is None or df.empty:
            continue
        cols = list(df.columns)
        code_c = next((c for c in ("Code", "Symbol", "ticker", "Ticker") if c in cols), None)
        cap_c = next((c for c in ("Marcap", "MarketCap", "market_cap") if c in cols), None)
        if not code_c or not cap_c:
            log.warning("[data_source] FDR %s 시총 컬럼 인식 실패: %s", market, cols)
            continue
        for _, row in df.iterrows():
            try:
                t = str(row[code_c]).zfill(6)
                cap = int(row[cap_c])
            except Exception:
                continue
            if len(t) == 6 and t.isdigit() and cap > 0:
                out[t] = cap
    if out:
        log.info("[data_source] FDR market_cap fetched %d종목", len(out))
    return out


def fetch_sectors() -> dict[str, str]:
    """{ticker: sector_str} — pykrx KRX 업종지수 → keyword 휴리스틱 폴백.

    1차: pykrx의 KRX 업종 인덱스(1006~1027 KOSPI + 2002~2026 KOSDAQ)별
         portfolio_deposit_file로 종목→섹터 매핑.
    2차: FDR StockListing의 Industry/Sector/Dept 컬럼 시도.
    3차: 종목명 keyword 휴리스틱 (정확도 50-60%, 채우는 게 목적).
    """
    out: dict[str, str] = {}

    # 1차: pykrx 업종지수
    try:
        out.update(_fetch_sectors_via_pykrx())
    except Exception:
        log.exception("[data_source] pykrx 업종지수 sector fetch 실패")

    # 2차: FDR — 'Industry'/'Sector' 또는 'Dept' 컬럼이 있을 때만
    try:
        fdr_secs = _fetch_sectors_via_fdr()
        for k, v in fdr_secs.items():
            out.setdefault(k, v)
    except Exception:
        log.exception("[data_source] FDR sector fetch 실패")

    # 3차: keyword 휴리스틱 — 미커버 종목 채움
    try:
        kw_secs = _sector_keywords_apply(out)
        for k, v in kw_secs.items():
            out.setdefault(k, v)
    except Exception:
        log.exception("[data_source] keyword 섹터 휴리스틱 실패")

    if out:
        log.info("[data_source] sector fetched %d종목 (pykrx+FDR+keyword 통합)", len(out))
    return out


# KRX 업종지수 코드 → 섹터명 매핑 (pykrx)
_KRX_SECTOR_INDICES = {
    # KOSPI
    "1004": "음식료품",
    "1006": "섬유의복",
    "1008": "종이목재",
    "1009": "화학",
    "1010": "의약품",
    "1011": "비금속광물",
    "1012": "철강금속",
    "1013": "기계",
    "1014": "전기전자",
    "1015": "의료정밀",
    "1016": "운수장비",
    "1017": "유통업",
    "1018": "전기가스업",
    "1019": "건설업",
    "1020": "운수창고업",
    "1021": "통신업",
    "1022": "금융업",
    "1023": "은행",
    "1024": "증권",
    "1025": "보험",
    "1026": "서비스업",
    # KOSDAQ (대표 산업)
    "2002": "기타서비스",
    "2003": "IT부품",
    "2222": "IT종합",
    "2003": "IT부품",
}


def _fetch_sectors_via_pykrx() -> dict[str, str]:
    """KRX 업종지수의 portfolio_deposit_file로 ticker→sector 매핑.

    각 인덱스 fetch는 ~1초. 총 21개 ≈ 21초. universe refresh 시 한 번만.
    실패한 인덱스는 skip하고 진행.
    """
    try:
        stock = _import_pykrx()
    except Exception:
        return {}
    out: dict[str, str] = {}
    # 최근 영업일 ymd 추정
    d = date.today()
    for _ in range(7):
        if d.weekday() < 5:
            break
        d -= timedelta(days=1)
    ymd = d.strftime("%Y%m%d")
    for idx_code, sec_name in _KRX_SECTOR_INDICES.items():
        try:
            df = stock.get_index_portfolio_deposit_file(ymd, idx_code)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        for ticker in df.index:
            t = str(ticker).zfill(6)
            if len(t) == 6 and t.isdigit():
                out.setdefault(t, sec_name)
    if out:
        log.info("[data_source] pykrx 업종지수 sector %d종목", len(out))
    return out


def _fetch_sectors_via_fdr() -> dict[str, str]:
    """FDR StockListing의 Dept/Industry/Sector 컬럼 시도. 폴백 용도."""
    try:
        fdr = _import_fdr()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        cols = list(df.columns)
        code_c = next((c for c in ("Code", "Symbol", "ticker", "Ticker") if c in cols), None)
        sec_c = next(
            (c for c in ("Industry", "Sector", "업종", "industry", "sector") if c in cols),
            None,
        )
        if not code_c or not sec_c:
            continue
        for _, row in df.iterrows():
            try:
                t = str(row[code_c]).zfill(6)
                s = str(row[sec_c] or "").strip()
            except Exception:
                continue
            if len(t) == 6 and t.isdigit() and s and s.lower() != "nan":
                out[t] = s
    return out


# 종목명 keyword 휴리스틱 (정확도 50-60%, 미커버 종목 채우기 용)
_SECTOR_KEYWORDS = [
    # 반도체 (소재·장비·설계)
    ("반도체", [
        "반도체", "하이닉스", "삼성전자", "DB하이텍", "하나마이크론", "솔브레인", "원익",
        "주성엔지니어링", "한미반도체", "리노공업", "유진테크", "테스나", "이오테크닉스",
        "후성", "동진쎄미켐", "테크윙", "ISC", "심텍", "티엘비", "코미코", "싸이맥스",
        "에프에스티", "디아이티", "프로텍", "OLED재료", "엘비세미콘", "네패스",
        "타이거일렉", "심텍홀딩스", "AP시스템", "텔레칩스", "코스텍시스", "두산테스나",
        "파두", "에스에이엠티", "HB테크놀러지", "시지트로닉스",
    ]),
    ("디스플레이", ["디스플레이", "LCD", "LED", "OLED", "LG디스플레이", "덕산", "이녹스"]),
    # 2차전지·ESS·신재생
    ("2차전지/배터리", [
        "에코프로", "엘앤에프", "포스코퓨처엠", "솔루스", "에너지솔루션", "삼성SDI",
        "코스모신소재", "LG에너지", "SK이노베이션", "SK온", "나노신소재", "SKC",
        "동화기업", "천보", "엔켐", "후성", "더블유씨피", "코스모화학", "피엔티",
        "일진머티리얼즈", "리튬", "양극재", "음극재", "전해질",
    ]),
    ("신재생/태양광", ["신재생", "태양광", "한화솔루션", "OCI", "현대에너지솔루션", "SDN"]),
    # 자동차
    ("자동차", [
        "현대차", "기아", "모비스", "타이어", "한온", "만도", "S&T모티브", "현대위아",
        "현대오토에버", "현대글로비스", "한국타이어", "넥센타이어", "디케이티",
        "해성옵틱스",
    ]),
    # 바이오/제약
    ("바이오/제약", [
        "바이오", "제약", "셀트리온", "한미약품", "유한양행", "녹십자", "대웅", "동아",
        "메디톡스", "삼성바이오", "에스디바이오", "바이넥스", "휴젤", "씨젠", "에이비엘",
        "헬릭스미스", "지놈", "차바이오", "헬스케어", "코아스템", "코오롱티슈진", "드림씨아이에스",
        "인바디", "코스맥스", "에이치엠넥스",
    ]),
    # 로봇·자동화
    ("로봇/자동화", ["로봇", "두산로보틱스", "레인보우로보틱스", "현대오토에버", "디케이티", "해성옵틱스"]),
    # 게임/콘텐츠
    ("게임", ["게임", "엔씨소프트", "넷마블", "카카오게임즈", "크래프톤", "펄어비스", "컴투스", "위메이드", "데브시스터즈"]),
    ("플랫폼/IT서비스", ["카카오", "네이버", "NAVER", "쿠팡", "다음", "엔씨", "아이티센", "MDS"]),
    # 화학
    ("화학", ["화학", "롯데케미칼", "LG화학", "한화솔루션", "효성", "금호석유", "OCI", "SKC", "코오롱"]),
    # 철강·금속
    ("철강/금속", ["철강", "포스코", "POSCO", "현대제철", "동국제강", "세아", "고려아연", "풍산"]),
    # 조선
    ("조선", ["조선", "중공업", "해양", "HD현대중공업", "삼성중공업", "한화오션", "현대미포"]),
    # 방산·우주·항공
    ("방산/우주/항공", [
        "방산", "KAI", "한화에어로", "LIG넥스원", "대한항공", "아시아나", "한국항공",
        "퍼스텍", "와이제이링크", "한국가구", "인텔리안",
    ]),
    # 원자력·전력
    ("원전/전력", [
        "원전", "원자력", "두산에너빌리티", "한전", "한국전력", "한국가스공사", "전력",
        "가온전선", "PS일렉트로닉스", "제룡", "대한전선", "두산퓨얼셀",
    ]),
    # 광통신·CPO·양자
    ("광통신/CPO", ["광통신", "CPO", "다원넥스뷰", "서울바이오시스", "아이크래프트", "옵틱스"]),
    ("양자/보안", ["양자", "QKD", "핑거", "보안"]),
    # 건설·인프라
    ("건설", ["건설", "현대건설", "GS건설", "DL이앤씨", "대우건설", "삼성물산", "HDC"]),
    # 금융
    ("금융", [
        "은행", "증권", "보험", "카드", "캐피탈", "우리금융", "KB금융", "신한지주",
        "하나금융", "미래에셋", "한국투자", "교보생명", "BNK", "DGB", "JB금융",
        "삼성생명", "삼성화재", "DB손해", "현대해상",
    ]),
    # 식음료·유통
    ("식음료", ["CJ제일", "오뚜기", "농심", "SPC", "대상", "동원", "롯데제과", "오리온", "하이트", "CJ프레시", "신세계푸드"]),
    ("유통/리테일", ["백화점", "마트", "이마트", "GS리테일", "롯데쇼핑", "신세계", "현대백화점"]),
    # 엔터·미디어
    ("엔터/미디어", ["엔터테인먼트", "JYP", "YG엔터", "SM엔터", "HYBE", "하이브", "CJ ENM"]),
    # 통신
    ("통신", ["SK텔레콤", "KT ", "LG유플러스", "통신"]),
    # 우주·스페이스
    ("우주/스페이스", ["우주", "위성", "스페이스", "인텔리안테크", "와이제이링크"]),
    # 지주사
    ("지주사", ["지주", "홀딩스", "코오롱", "한솔홀딩스"]),
    # 히트펌프·HVAC
    ("히트펌프/HVAC", ["오텍", "히트펌프", "냉동공조"]),
    # 가구·라이프스타일
    ("가구/라이프", ["가구", "한샘", "이케아"]),
]


def _sector_keywords_apply(already_mapped: dict[str, str]) -> dict[str, str]:
    """이미 매핑된 ticker는 skip. universe.py에서 호출 시 종목명 사용 예정.

    여기선 ticker→name 매핑이 없으므로 빈 dict 반환.
    실제 휴리스틱은 universe.refresh_market_caps에서 종목명과 함께 적용.
    """
    return {}


def apply_sector_keywords(name: str) -> str | None:
    """종목명 → 섹터 추정 (휴리스틱). 매칭 없으면 None."""
    if not name:
        return None
    for sec, keywords in _SECTOR_KEYWORDS:
        for kw in keywords:
            if kw in name:
                return sec
    return None


def fetch_kospi_kosdaq_tickers() -> list[tuple]:
    """KOSPI+KOSDAQ 보통주 ticker 리스트. pykrx → FDR 자동 폴백.

    반환: (ticker, name, market). ETF/SPAC/우선주/관리종목 제외 (universe.py에서 한 번 더 필터).
    """
    out = _fetch_tickers_via_pykrx()
    if out:
        return out
    log.warning("[data_source] pykrx ticker list 빈 결과 → FDR 폴백 시도")
    out = _fetch_tickers_via_fdr()
    if out:
        log.info("[data_source] FDR ticker list 성공: %d종목", len(out))
    else:
        log.error("[data_source] pykrx + FDR 모두 ticker list 실패")
    return out


def _fetch_tickers_via_pykrx() -> list[tuple]:
    try:
        stock = _import_pykrx()
    except Exception:
        log.exception("[data_source] pykrx import 실패")
        return []
    out: list[tuple] = []
    today_str = datetime.now().strftime("%Y%m%d")
    for market in ("KOSPI", "KOSDAQ"):
        last_err = None
        for attempt in range(DEFAULT_RETRIES):
            try:
                tickers = stock.get_market_ticker_list(today_str, market=market)
                if not tickers:
                    raise RuntimeError("empty ticker list")
                for t in tickers:
                    try:
                        name = stock.get_market_ticker_name(t)
                    except Exception:
                        name = t
                    out.append((str(t), str(name), market))
                break
            except Exception as e:
                last_err = e
                log.warning(
                    "[data_source] pykrx %s ticker list 실패 attempt=%d err=%s",
                    market, attempt + 1, e,
                )
                time.sleep(2 ** attempt)
        else:
            log.error("[data_source] pykrx %s ticker list 최종 실패 err=%s", market, last_err)
    return out


def _fetch_tickers_via_fdr() -> list[tuple]:
    try:
        fdr = _import_fdr()
    except Exception:
        log.exception("[data_source] FDR import 실패")
        return []
    out: list[tuple] = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
        except Exception:
            log.exception("[data_source] FDR.StockListing(%s) 실패", market)
            continue
        if df is None or df.empty:
            continue
        # FDR 컬럼은 버전에 따라 'Symbol'/'Code'/'Ticker', 'Name'/'name'
        cols = list(df.columns)
        code_c = next((c for c in ("Code", "Symbol", "ticker", "Ticker") if c in cols), None)
        name_c = next((c for c in ("Name", "name") if c in cols), None)
        if not code_c or not name_c:
            log.warning("[data_source] FDR %s 컬럼 인식 실패: %s", market, cols)
            continue
        for _, row in df.iterrows():
            t = str(row[code_c]).zfill(6)
            n = str(row[name_c])
            # 6자리 숫자 ticker만 (예: ETF/ETN 코드 제외)
            if not (len(t) == 6 and t.isdigit()):
                continue
            out.append((t, n, market))
    return out


def last_n_business_days(end: date, n: int) -> list[str]:
    """end 포함 과거 n개 영업일(월-금)을 YYYYMMDD 문자열 리스트로 반환 (asc).

    KRX 공휴일은 fetch 단계에서 빈 결과로 자연스럽게 걸러짐.
    """
    out: list[str] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:  # 월-금
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    out.reverse()
    return out


def is_business_day(d: date) -> bool:
    return d.weekday() < 5
