# 새 봇 추가 가이드

이 문서는 기존 봇(Company/Industry/Market/Global)에 더해 **새 봇**(예: 아이디어봇)을
충돌 없이 추가하는 절차를 설명합니다. **별도 Claude Code 세션**에서 작업해도
이 가이드만 보고 따라할 수 있도록 자체 완결적으로 작성됨.

---

## 아키텍처 한 눈에

```
오케스트레이터(src/orchestrator.py) — 단일 프로세스, 단일 asyncio loop
 ├─ CompanyBot      (TELEGRAM_BOT_TOKEN)        src/bot_worker.py
 ├─ IndustryBot     (INDUSTRY_BOT_TOKEN)        src/category_bots.py
 ├─ MarketBot       (MARKET_BOT_TOKEN)          src/category_bots.py
 ├─ GlobalBot       (GLOBAL_BOT_TOKEN)          src/category_bots.py
 └─ <YourBot>       (<YOUR_BOT_TOKEN>)          src/<your_bot>.py    ← 추가하려는 것
                                                                       
공유 자원
 ├─ PIPELINE_LOCK (asyncio.Lock) — wisereport 세션 직렬화. 모든 봇 공통.
 ├─ state_store   — seen_rpt_ids.json (dedup). category당 dedup_key 분리.
 ├─ DART corp_map — 메모리 캐시 (idempotent).
 └─ OpenRouter    — 호출당 client 생성, 공유 상태 없음.
```

각 봇은 **자기 토큰**으로 별도 텔레그램 polling. 토큰이 다르면 텔레그램 API에서 격리됨.

---

## 절대 지킬 것

1. **새 봇은 자기 모듈에**: `src/<your_bot>.py` 같이 새 파일에 작성. 기존 파일
   (`category_bots.py`, `bot_worker.py`) 수정 금지 — 머지 충돌 유발.

2. **wisereport 호출은 반드시 PIPELINE_LOCK 안에서**: wisereport는 같은 계정
   동시 로그인을 감지해 세션을 무효화함. 새 봇이 wisereport에 접속한다면:
   ```python
   from src.pipeline_lock import PIPELINE_LOCK

   async with PIPELINE_LOCK:
       # wisereport 작업
       ...
   ```

3. **state_store dedup_key는 봇마다 고유하게**: 다른 봇과 같은 키 쓰면 dedup이
   섞임. 예: `idea_daily`, `idea_top10`.

4. **공용 헬퍼는 `src.bot_helpers`에서 import**: `category_bots.py`의 underscore
   prefix 함수(`_send_text` 등)는 직접 호출하지 말 것 (private). 대신:
   ```python
   from src.bot_helpers import (
       is_authorized, send_text_chunked, send_pdf,
       safe_dirname, download_root_for,
   )
   ```

5. **모든 외부 호출에 try/except**: 봇 핸들러에서 raise되는 예외는
   PTB가 잡지만, asyncio.create_task로 떼어낸 작업은 봇 프로세스를 죽일 수 있음.
   외부 자원 호출(httpx, playwright, openai)은 반드시 try로 감싸기.

---

## 새 봇 추가 절차 (5단계)

### 1) `src/<your_bot>.py` 작성

스켈레톤:
```python
"""<YourBot> — 한 줄 설명."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from src.bot_helpers import (
    download_root_for, is_authorized, safe_dirname,
    send_pdf, send_text_chunked,
)
from src.pipeline_lock import PIPELINE_LOCK

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ALLOWED_CHAT_IDS_ENV = "IDEA_ALLOWED_CHAT_IDS"   # ← 본인 봇용 환경변수
CHAT_ID_ENV = "IDEA_CHAT_ID"                     # ← 자동 발송 대상

HELP_TEXT = "📊 <YourBot>\n\n사용법: ..."


async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_CHAT_IDS_ENV):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update, ALLOWED_CHAT_IDS_ENV):
        return
    text = (update.message.text or "").strip()
    # ... 처리 로직
    bot: Bot = context.bot
    chat_id = str(update.effective_chat.id)
    async with PIPELINE_LOCK:        # ← wisereport 쓸 때만
        # blocking 작업은 run_in_executor
        loop = asyncio.get_running_loop()
        # result = await loop.run_in_executor(None, _blocking_task)
        await send_text_chunked(bot, chat_id, "결과...")


async def scheduled_job(bot: Bot) -> None:
    """orchestrator의 APScheduler가 호출 — 매일/주간 등."""
    log.info("[scheduled] <yourbot> 작업 시작")
    chat_id = os.environ.get(CHAT_ID_ENV)
    if not chat_id:
        log.error("%s 미설정", CHAT_ID_ENV)
        return
    # ... 작업
    log.info("[scheduled] <yourbot> 작업 완료")


def build_app(token: str) -> Application:
    """orchestrator가 호출 — Application 인스턴스 반환."""
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], _help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    return app
```

### 2) `src/orchestrator.py`의 `BOT_SPECS`에 1줄 append

```python
BOT_SPECS: list[BotSpec] = [
    # ... 기존 4개 ...
    BotSpec(
        name="idea",
        token_env="IDEA_BOT_TOKEN",
        builder=__import__("src.idea_bot", fromlist=["build_app"]).build_app,
        jobs=[
            ScheduledJob(
                func=__import__("src.idea_bot", fromlist=["scheduled_job"]).scheduled_job,
                job_id="idea_daily",
                cron={"hour": 8, "minute": 0},
                description="아이디어 매일 08:00 KST",
            ),
        ],
    ),
]
```

또는 그냥 import문을 위에 추가하고 직접 참조:
```python
from src.idea_bot import build_app as build_idea_app, scheduled_job as idea_job
```

### 3) Railway 환경변수 등록

Railway Dashboard → Variables에 추가:
- `IDEA_BOT_TOKEN` — 텔레그램 BotFather에서 새로 만든 봇 토큰
- `IDEA_CHAT_ID` — 자동 발송 대상 chat_id (1813560888 등)
- `IDEA_ALLOWED_CHAT_IDS` — 인가 chat_id 콤마 구분

### 4) 로컬 검증

```bash
python -c "import ast; ast.parse(open('src/idea_bot.py').read()); print('OK')"
python -c "from src.orchestrator import BOT_SPECS; print(len(BOT_SPECS), '봇 등록')"
```

### 5) Push & 배포 확인

```bash
git add src/idea_bot.py src/orchestrator.py BOTS.md
git commit -m "Add IdeaBot — ..."
git push -u origin claude/<your-branch>
```

Railway가 GitHub webhook 받아 빌드 시작 (1-2분 후) → 빌드 ~3분 → 부팅. 총 4-5분.
배포 SUCCESS 확인은:
```bash
RAILWAY_TOKEN=<token> railway deployment list -s basketball-rag-agent
```

---

## 흔한 실수

| 실수 | 증상 | 해결 |
|---|---|---|
| 같은 텔레그램 토큰을 두 봇이 사용 | `Conflict: terminated by other getUpdates` 무한 반복 | BotFather에서 새 봇 만들기 |
| wisereport 호출에 PIPELINE_LOCK 빠뜨림 | 다른 봇 작업 중 새 봇이 끼어들면 wisereport 세션 깨짐 | `async with PIPELINE_LOCK:` 감싸기 |
| state_store dedup_key 충돌 | 다른 봇 발송 결과까지 dedup에 들어가 누락 | 봇 prefix (`idea_*`) 사용 |
| Playwright을 asyncio loop 안에서 sync 호출 | 이벤트 루프 블록 | `await loop.run_in_executor(None, _blocking)` |
| import error로 다른 봇까지 죽음 | 오케스트레이터 부팅 실패 | 무거운 import는 함수 본문 안으로 |

---

## 참고 파일

- `src/bot_helpers.py` — 공용 헬퍼 (send/auth/path)
- `src/pipeline_lock.py` — `PIPELINE_LOCK` (모든 봇 공유)
- `src/state_store.py` — dedup file storage
- `src/wisereport.py` — `WisereportClient` (sync, run_in_executor 필요)
- `src/summarizer.py` — OpenRouter 클라이언트 + PDF 텍스트 추출
- `src/deepdive/dart_client.py` — DART API + corp_code 매핑
- `src/orchestrator.py` — 등록 list (`BOT_SPECS`)
- `src/category_bots.py` — Industry/Market/Global 참고 구현. **수정 금지**
- `src/bot_worker.py` — Company 참고 구현. **수정 금지**
