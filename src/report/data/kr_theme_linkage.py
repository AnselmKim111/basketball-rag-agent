"""미국 테마 → 한국 수혜주 ticker 매핑.

flow_charts.US_KR_LINKAGE(종목명만)와 짝. watchlist.py가 screener.db 비어있을 때
fallback stream으로 활용. ticker는 KOSPI/KOSDAQ 6자리 코드.

매핑 기준: 사용자가 실제 매매할 수 있는 대형·중형 핵심주 (시총 5천억 이상 위주).
이 외 종목은 graceful skip — 검증되지 않은 ticker 박지 않음.
"""
from __future__ import annotations

# 종목명 → 6자리 ticker
KR_NAME_TO_TICKER: dict[str, str] = {
    # 반도체
    "삼성전자": "005930",
    "SK하이닉스": "000660",
    "한미반도체": "042700",
    # 우주·항공
    "한화에어로스페이스": "012450",
    "한국항공우주": "047810",
    # 방산
    "LIG넥스원": "079550",
    "현대로템": "064350",
    # 연료전지·수소
    "두산퓨얼셀": "336260",
    "에스퓨얼셀": "288620",
    # 원자력·우라늄
    "두산에너빌리티": "034020",
    "한전기술": "052690",
    # 사이버보안
    "안랩": "053800",
    # 로봇·AI
    "레인보우로보틱스": "277810",
    "두산로보틱스": "454910",
    # 태양광·신재생
    "OCI홀딩스": "010060",
    "한화솔루션": "009830",
    "씨에스윈드": "112610",
    # 2차전지·리튬
    "에코프로비엠": "247540",
    "포스코퓨처엠": "003670",
    "LG에너지솔루션": "373220",
    "삼성SDI": "006400",
    # 바이오
    "알테오젠": "196170",
    "HLB": "028300",
    "리가켐바이오": "141080",
}

# 테마 키워드 → 한국 수혜주 후보 (flow_charts.US_KR_LINKAGE의 ticker-aware 버전)
# 매핑 없는 종목은 자동 skip되므로 flow_charts 매핑보다 짧을 수 있음.
US_KR_THEME_BENEFICIARIES: dict[str, list[str]] = {
    "반도체": ["삼성전자", "SK하이닉스", "한미반도체"],
    "우주": ["한화에어로스페이스", "한국항공우주"],
    "방산": ["LIG넥스원", "한화에어로스페이스", "현대로템"],
    "원전": ["두산에너빌리티", "한전기술"],
    "우라늄": ["두산에너빌리티", "한전기술"],
    "수소": ["두산퓨얼셀", "에스퓨얼셀"],
    "연료전지": ["두산퓨얼셀", "에스퓨얼셀"],
    "사이버": ["안랩"],
    "로봇": ["레인보우로보틱스", "두산로보틱스"],
    "AI": ["레인보우로보틱스", "두산로보틱스"],
    "태양광": ["OCI홀딩스", "한화솔루션"],
    "신재생": ["한화솔루션", "씨에스윈드"],
    "리튬": ["에코프로비엠", "포스코퓨처엠", "LG에너지솔루션"],
    "2차전지": ["에코프로비엠", "삼성SDI", "포스코퓨처엠"],
    "바이오": ["알테오젠", "HLB", "리가켐바이오"],
}

# 섹터 라벨 (watchlist enrichment·차트 색상에 사용)
KR_SECTOR_BY_THEME: dict[str, str] = {
    "반도체": "반도체",
    "우주": "방산/우주",
    "방산": "방산",
    "원전": "원자력",
    "우라늄": "원자력",
    "수소": "수소/연료전지",
    "연료전지": "수소/연료전지",
    "사이버": "사이버보안",
    "로봇": "로봇/AI",
    "AI": "로봇/AI",
    "태양광": "신재생/태양광",
    "신재생": "신재생/태양광",
    "리튬": "2차전지",
    "2차전지": "2차전지",
    "바이오": "바이오",
}


def match_theme_keyword(label: str) -> str | None:
    """theme label (예: '태양광(TAN)') → 매칭되는 한국 테마 키워드."""
    if not label:
        return None
    for kw in US_KR_THEME_BENEFICIARIES.keys():
        if kw in label:
            return kw
    return None


def beneficiaries_with_tickers(label: str) -> list[tuple[str, str, str]]:
    """label → [(name, ticker, sector), ...]. 매핑 없거나 ticker 없는 종목은 skip."""
    kw = match_theme_keyword(label)
    if not kw:
        return []
    sector = KR_SECTOR_BY_THEME.get(kw, "")
    out: list[tuple[str, str, str]] = []
    for name in US_KR_THEME_BENEFICIARIES.get(kw, []):
        ticker = KR_NAME_TO_TICKER.get(name)
        if ticker:
            out.append((name, ticker, sector))
    return out
