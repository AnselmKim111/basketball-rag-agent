"""IdeaBot 서브패키지 — 모듈 분리로 idea_bot.py 슬림화.

분리 모듈:
  - src/idea/tickers.py — (name, ticker6) DART 검증 + fuzzy 매칭.

향후 추가:
  - src/idea/pipeline.py — parse/research/importance/narrow/synthesis 단계 함수들.
  - src/idea/handlers.py — 사용자 명령 핸들러 (/history /show /dive /refine 등).
  - src/idea/results.py — _send_results, Top 1 분기차트.
"""
