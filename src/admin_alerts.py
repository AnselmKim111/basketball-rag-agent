"""운영 알림(헬스 + 크론 미완주) — admin chat_ids로 ⚠ 알림 발송.

조용한 외부 소스 실패(예: FDR 시총 컬럼 사라짐)·크론 미완주가 며칠간 무감지로
흐르는 것을 막기 위한 공용 헬퍼. send 자체는 plain text(parse_mode 없음)로 보내
파싱 에러 잠재 위험도 차단.

사용 예:
    from src.admin_alerts import alert_admin, BASELINES
    if len(caps) < BASELINES["us_caps"]:
        await alert_admin(bot, ("US_SCREENER_ALLOWED_CHAT_IDS","SCREENER_ALLOWED_CHAT_IDS"),
                          "⚠ US 시총 fetch 급감",
                          f"평소 ~500, 오늘 {len(caps)} (FDR 컬럼 변경 의심)")
"""
from __future__ import annotations

import logging

from telegram import Bot

from src.bot_helpers import parse_admin_chat_ids

log = logging.getLogger(__name__)

# 데이터 소스 베이스라인 — 정상 운영 시 최소 기대치. 미만이면 알림.
# 보수적으로 시작 (거짓 양성 < 거짓 음성), 며칠 관측 후 조정.
BASELINES = {
    "us_caps": 100,         # us_screener.fetch_market_caps (FDR S&P500). 평소 ~500.
    "us_sectors": 100,      # us_screener.fetch_sectors. 평소 ~500.
    "kr_caps": 2000,        # screener.fetch_market_caps (pykrx ALL). 평소 ~2700.
    "kr_sectors": 200,      # screener.fetch_sectors. 평소 ~560.
    "kr_naver_match_pct": 50,  # incremental Naver batch target_match 비율(%). 평소 거의 100.
}


async def alert_admin(bot: Bot, env_keys: tuple[str, ...], title: str, message: str) -> None:
    """admin chat_ids에 운영 알림 발송. 하나 실패해도 다음 시도. parse_mode 없음(파싱 위험 0).

    env_keys: ("US_SCREENER_ALLOWED_CHAT_IDS", "SCREENER_ALLOWED_CHAT_IDS") 같이 우선순위 순.
    """
    admin_ids = parse_admin_chat_ids(*env_keys)
    if not admin_ids:
        log.warning("[admin_alert] chat_ids 없음 — 알림 생략 (%s)", title)
        return
    body = f"⚠️ {title}\n{message}"
    for cid in admin_ids:
        try:
            await bot.send_message(chat_id=cid, text=body)
        except Exception:
            log.exception("[admin_alert] 발송 실패 cid=%s", cid)
