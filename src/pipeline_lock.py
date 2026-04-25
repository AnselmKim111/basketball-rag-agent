"""모든 wisereport 파이프라인 작업을 직렬화하기 위한 글로벌 lock.

CompanyBot의 종목 리포트 작업과 IndustryBot/MarketBot/GlobalBot의 카테고리
작업이 모두 같은 자격증명으로 wisereport에 로그인한다. 동시 로그인하면
wisereport의 중복 로그인 감지에 걸려 일부 세션이 무효화된다.

orchestrator 내 모든 봇이 이 lock을 공유 → 들어오는 요청은 큐로 직렬 처리.
"""

import asyncio

PIPELINE_LOCK = asyncio.Lock()
