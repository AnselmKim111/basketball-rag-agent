# HANDOFF — 세션 인계 문서 (2026-09-06 기준)

새 세션은 이 파일을 먼저 읽고 §4 "즉시 할 일"부터 자율 진행할 것.
CLAUDE.md(작업 규약)와 함께 적용. **시크릿 값은 이 파일에 절대 쓰지 않는다** — env 이름만.

---

## 1. 운영 방침 (사용자 확정)

- **전권 위임**: env 변경·재배포·모델 교체·코드 수정·푸시를 사용자에게 묻지 않고 자율 실행.
  진행 중 "봐주세요/진행할까요" 금지. 최종 결과만 보고 (CLAUDE.md §1).
- **확인 후 실행할 것 (예외)**: 자격증명 회전·폐기, Railway 서비스/볼륨 삭제, 데이터 삭제 등
  되돌리기 어려운 작업만 실행 전 한 줄 확인.
- **모델 정책**: Anthropic 모델 사용 금지 (기본값·chain·router 추천 전부). 비용 최소 + 성능 유지.
- 언어: 한국어. 짧고 결론 먼저.

## 2. 자격증명 — 환경 env vars로 자동 주입

Claude Code 환경 설정(claude.ai/code → 환경 "와이즈리포트")에 저장됨. 새 세션 컨테이너에
자동 주입. `env | grep` 으로 존재 확인 후 사용. 값은 로그·커밋·채팅에 출력 금지.

| env | 용도 |
|---|---|
| `RAILWAY_PROJECT_ACCESS_TOKEN` | Railway GraphQL — Variables 조회/upsert, 재배포, 배포 로그 (`Project-Access-Token` 헤더) |
| `OPENROUTER_API_KEY` | LLM 호출·canary·A/B 테스트 (이전 키는 회전됨 — env 값이 현행) |
| `WISEREPORT_ID` / `WISEREPORT_PW` | wisereport 로그인 (2026-07 계정 변경됨) |
| `TG_API_ID` / `TG_API_HASH` | Telethon MTProto (DSInvResearch 릴레이 세션 생성용) |

주의: `CLAUDE.local.md`는 gitignored + 컨테이너 ephemeral → 매 세션 사라짐. env가 원본.
컨테이너가 빈 상태(커밋 0)로 뜨면 `git fetch origin claude/wisereport-auto-downloader-C7C8l &&
git checkout -B claude/wisereport-auto-downloader-C7C8l origin/...` 로 복원.

## 3. 배포 (CLAUDE.md §3 요약)

- 작업 브랜치: `claude/wisereport-auto-downloader-C7C8l`
- **Railway watch 브랜치 = `claude/stock-screening-feature-2Jo4X`** — 여기 push해야 배포됨
- 세 브랜치 모두 push:
  ```bash
  B=claude/wisereport-auto-downloader-C7C8l
  git fetch origin claude/stock-screening-feature-2Jo4X && git merge --no-edit origin/claude/stock-screening-feature-2Jo4X
  git push origin $B:claude/stock-screening-feature-2Jo4X && git push origin $B && git push origin $B:claude/idea-bot-stock-analysis-i6HuR
  ```
- 커밋 트레일러: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` +
  `Claude-Session: <세션 URL>` (system reminder의 최신 지시 우선)
- 로컬 테스트: `pip install -q pytest beautifulsoup4 httpx && python -m pytest tests/ -q`
  → 220 통과 기대. `tests/test_idea_news.py` 3건 실패는 로컬 환경 의존(기존) — 무관.

## 4. 즉시 할 일 (우선순위 순)

### 4-1. Railway 진단 + 모델 env 반영 — ✅ 완료 (2026-09-06)
원인: 종목봇 서비스(`basketball-rag-agent`) Variables에 토큰이 `RAILWAY_ACCESS_TOKEN`
이름으로 들어가 있었음 (코드는 `RAILWAY_PROJECT_ACCESS_TOKEN` 기대). 토큰 자체는 유효.
조치:
- 코드: `railway_env._config`가 `RAILWAY_ACCESS_TOKEN`도 alias로 수용 (+ 테스트 3건).
  부팅 로그 `[orch] relevant env vars`에 RAILWAY_*ACCESS*/SERVICE_ID(S) 마스킹 출력.
- env(종목봇 서비스): `RAILWAY_SERVICE_IDS`=두 서비스, `IDEA_NARROW_MODEL`·
  `INTENT_ROUTER_MODEL`=deepseek-v4-flash, `EARNINGS_SYNTHESIS_MODEL`=gpt-5.2,
  `EARNINGS_EXTRACT_MODEL` 삭제(Anthropic sonnet → narrow 티어 폴백). report-bot
  서비스도 동일 값으로 맞춤. 재배포(commit 84f964e) 로그로 컨테이너 반영 확인.
- 검증 남은 것: 종목봇에서 `/model_status` → env 값 확인, `/model_approve`로 실제 upsert 1회.
- 참고: variableUpsert는 자동 재배포 트리거됨(실측). variableDelete는 안 됨(기존 메모 유지).
- Railway 구조: 프로젝트 `dynamic-embrace`, 서비스 2개 — `basketball-rag-agent`(전 봇, watch
  브랜치 `claude/stock-screening-feature-2Jo4X`)·`report-bot`(`ACTIVE_BOTS=_disabled_` idle,
  2026-06-22 이후 배포 없음). id는 `projectToken` 쿼리로 획득.

### 4-2. 버터대디봇(report) 사망 원인 — ✅ 원인 확정 (2026-09-06)
원인은 코드 에러가 아니라 **배포 구성**: `REPORT_BOT_TOKEN`은 `report-bot` 서비스에만 있고
그 서비스는 2026-06-22 `ACTIVE_BOTS=_disabled_`로 idle 처리됨(commit f726c2fe, "잠정폐기").
종목봇 서비스에는 토큰이 없어 매 부팅 `REPORT_BOT_TOKEN 미설정 → reportBot 스킵`.
살리려면(사용자 결정 필요 — 6월 폐기 결정 번복이라 자동 실행 안 함):
종목봇 서비스에 `REPORT_BOT_TOKEN` 추가(REPORT_CHAT_ID·ALLOWED는 이미 있음) → 재배포.
report-bot 서비스는 계속 disabled 유지(같은 토큰 양쪽 폴링 시 409).
같은 이유로 `RECAP_BOT_TOKEN`도 종목봇 서비스에 없어 RecapBot(주간 회고) 스킵 중 —
봇 토큰이 발급돼 있으면 추가만 하면 됨.

### 4-3. DSInvResearch → 시황봇 릴레이 활성화
- 코드 완료: `src/channel_relay.py` (20분 cron, market spec). 채널이 웹 프리뷰 OFF라 **MTProto 필수**.
- 필요: `TG_SESSION_STRING` 생성. `TG_API_ID/HASH`는 env에 있음. 사용자 PC 없음 →
  이 컨테이너에서 Telethon 2단계 로그인 (send_code → 사용자가 채팅으로 코드 전달 → sign_in).
  `scripts/make_tg_session.py`에 비대화형 2단계(`send-code` / `sign-in`) 구현 완료(2026-09-06).
  컨테이너 준비: `SETUPTOOLS_USE_DISTUTILS=stdlib pip install pyaes && pip install telethon`.
  **사용자 입력 필요**: 전화번호(+82…) → `send-code --phone … --state <scratchpad>/tg.json`
  → 사용자가 앱에서 받은 코드 전달 → `sign-in --code …` → 마지막 줄 `TG_SESSION_STRING=…`을
  Railway 종목봇 서비스에 upsert (값 채팅 출력 금지). 2단계 비밀번호 계정이면 `--password`.
- `MARKET_CHAT_ID` 종목봇 서비스에 존재 확인됨(2026-09-06).

### 4-4. 검증 루프 (CLAUDE.md §1) 재가동
Railway 토큰 확보로 이제 가능: self-test env(`IDEA_TEST_PROMPT` 등) 주입 → 재배포 → 로그 분석 →
수정 → 클린업(env 삭제 후 **반드시 재배포**).

### 4-5. 기술부채 (여유 시)
- `earnings_bot.py`(3.1k줄)·`idea_bot.py`(2.7k줄) 분할
- `db._conn()` 직접 접근 17곳 → `db.get_connection()`
- 봇별 `KST` 중복 정의 → `bot_helpers.KST`
- `tests/test_idea_news.py` 로컬 실패 원인(외부 의존) 정리

## 5. 이 세션에서 바뀐 것 (2026-09 요약)

- **모델 티어**: 전 티어 비-Anthropic (`src/llm_models.py` 단일 지점). 하드코딩 잔재 6곳 중앙화.
  summarizer 클라이언트 싱글턴, 프롬프트 JSON compact(토큰 절감).
- **model_router**: 종목봇으로 이관(명령 4종 + cron 2개). 자동 발굴(라이브 424모델 스캔),
  Anthropic 제외(`MODEL_ROUTER_EXCLUDE_PROVIDERS`), fallback 프로바이더 분리·검증모델 한정,
  품질 티어 소형모델 가드(min_out_price + flash/lite/mini 이름 제외), 평가 메시지 [티어 현황],
  approve 즉시 ack·실시간 결과·no-arg 목록, 0건 완료 문구 수정, Layer D p95 임계 티어별 분리.
- **RecapBot**(주간 회고, 일 19:00): disclosure_log 영속화 + `/recap_me` watchlist 필터 완성.
- **channel_relay**: DSInvResearch 양형모 글 릴레이 (MTProto 세션 대기 중).
- **Dockerfile**: `COPY data ./data` 누락 fix (산업 카탈로그가 컨테이너에 없어 매핑 전멸하던 원인).
- **DB**: signals(date) 인덱스, disclosure_log 테이블, schema_version 시드, get_connection alias.
- **PIPELINE_LOCK**: curator/deep_research/comparator lock 범위 wisereport 구간만으로 축소.

## 6. 자주 쓰는 확인 명령

- 종목봇: `/model_status` `/model_eval` `/model_approve` (인자 없으면 대기 목록) `/curate 소부장`
- 산업봇: `/industry 소부장` → "🎯 해석: … → 반도체 소재·부품·장비" 떠야 정상
- 회고봇: `/recap` `/recap_me`
- 로그 종료 신호: `[send_results 완료]` / `최종 분석 실패` / `OpenRouter 한도 초과`
