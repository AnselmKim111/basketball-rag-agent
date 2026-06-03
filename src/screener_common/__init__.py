"""KR/US 스크리너 봇의 공통 헬퍼.

src.screener_bot, src.us_screener_bot 사이 95% 미러 중복 제거용.
어댑터 패턴 — 시장별 차이(통화·channel·뉴스URL·earnings 조회)는
caller가 인자로 주입.
"""
from src.screener_common.badges import BADGE
from src.screener_common.permalink import permalink
from src.screener_common.format import fmt_pct, fmt_won, fmt_usd
from src.screener_common.chart_caption import render_caption

__all__ = ["BADGE", "permalink", "fmt_pct", "fmt_won", "fmt_usd", "render_caption"]
