"""가격 시계열 무결성 검사 — 분할/조정 불일치·forward-fill 글리치 탐지.

validator는 '오늘 종가'만 Naver 재fetch로 교차검증한다. 하지만 연초대비(YTD)·
신호 후속수익률 회고는 **과거 종가**를 쓰는데 이건 검증된 적이 없다.
과거 데이터에 스케일 브레이크(예: 액면분할 후 조정 불일치, 시뮬레이션 forward-fill)가
있으면 +244% YTD, 5영업일 +98.7% 같은 물리적으로 불가능한 값이 메시지에 노출된다.

탐지 원리 — 시장 구조적 사실:
  - 한국(KOSPI/KOSDAQ): 일일 가격제한폭 ±30%. 인접 거래일 종가비가 [0.70, 1.30] 밖이면
    정규 거래로는 불가능 → 분할·정지해제·데이터 손상. 마진 두고 [0.65, 1.35]로 판정.
  - 미국(NYSE/Nasdaq): 일일 제한 없음 → 정상 종목도 +50% 가능. 분할 수준(>5배/<1/5)만 차단.
"""
from __future__ import annotations

# 한국: ±30% 제한 + 라운딩/엣지 마진
KR_DAILY_LO, KR_DAILY_HI = 0.65, 1.35
# 미국: 제한 없음 — 분할급 스케일 브레이크만 (5배/0.2배)
US_DAILY_LO, US_DAILY_HI = 0.20, 5.00


def series_is_continuous(closes: list, lo: float, hi: float) -> bool:
    """인접 종가비가 모두 [lo, hi] 안이면 True (스케일 브레이크 없음).

    closes: 거래일 오름차순 종가 리스트. 빈/단일 원소면 검사 불가 → True(통과).
    0/음수 종가가 있으면 False(손상).
    """
    for a, b in zip(closes, closes[1:]):
        if a <= 0 or b <= 0:
            return False
        r = b / a
        if r < lo or r > hi:
            return False
    return True
