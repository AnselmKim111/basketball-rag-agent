"""KR/US 스크리너 공통 핵심 — 시장 비의존 순수 함수.

- `signals`: 단일 종목 OHLCV → 발현 신호 계산 (시장 무관)
- `formatter`: 미미 스타일 메시지 헬퍼 (섹터 그룹·이모지 섹션·% 포맷)

KR/US 시장 특화(데이터 소스·시총 필터·라벨·시간대)는 `src/screener/`,
`src/us_screener/`에 분리 유지.
"""
