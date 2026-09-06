# HANDOFF — 세션 인계 문서 (2026-09-05 기준)

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

### 4-1. Railway 진단 + 모델 env 반영 (최우선)
사용자가 Railway Variables에 `RAILWAY_PROJECT_ACCESS_TOKEN`을 넣었는데도 종목봇
`/model_approve`가 "Railway 설정 누락"으로 실패 중. 이 세션의 토큰으로 직접 진단:

```python
# Project Token 스코프 확인 → project/environment id 자동 획득
query { projectToken { projectId environmentId } }
# 서비스 목록
query($pid:String!){ project(id:$pid){ services{ edges{ node{ id name } } } } }
# 서비스 변수 조회
query($pid:String!,$eid:String!,$sid:String!){ variables(projectId:$pid, environmentId:$eid, serviceId:$sid) }
# upsert
mutation($input:VariableUpsertInput!){ variableUpsert(input:$input) }
# 재배포 (variableDelete는 자동 재배포 안 됨 — CLAUDE.md §6 주의)
mutation($eid:String!,$sid:String!){ serviceInstanceRedeploy(environmentId:$eid, serviceId:$sid) }
# 로그
query($id:String!){ deploymentLogs(deploymentId:$id, limit:500){ message timestamp severity } }
```
엔드포인트 `https://backboard.railway.com/graphql/v2`, 헤더 `Project-Access-Token: <token>`.
코드 참조: `src/model_router/railway_env.py`.

체크리스트:
1. 봇 서비스 Variables에 `RAILWAY_PROJECT_ACCESS_TOKEN` 실제 존재? 추가 후 재배포 됐나?
2. 컨테이너에 `RAILWAY_PROJECT_ID`/`RAILWAY_ENVIRONMENT_ID`/`RAILWAY_SERVICE_ID` 자동 주입되나?
   (railway_env._config 가 이걸 기대 — 없으면 Variables에 명시 추가)
3. 서비스가 2개(봇/스크리너 분리, `ACTIVE_BOTS`)면 `RAILWAY_SERVICE_IDS`에 콤마로 둘 다.
4. 모델 env 목표값 (비-Anthropic, 라이브 가격 2026-09-03 기준):
   ```
   OPENROUTER_MODEL=deepseek/deepseek-v4-flash
   IDEA_NARROW_MODEL=deepseek/deepseek-v4-flash
   INTENT_ROUTER_MODEL=deepseek/deepseek-v4-flash
   IDEA_SYNTHESIS_MODEL=deepseek/deepseek-v4-pro
   REPORT_SYNTHESIS_MODEL=deepseek/deepseek-v4-pro
   EARNINGS_SYNTHESIS_MODEL=openai/gpt-5.2
   OPENROUTER_FALLBACK_MODEL=moonshotai/kimi-k2.6
   IDEA_RESEARCH_MODEL=perplexity/sonar-pro
   ```
   (코드 기본값 `src/llm_models.py`와 동일 — env가 우선하므로 env도 맞춰야 적용)
5. 재배포 → deploymentLogs로 부팅·봇 폴링 확인 → 종목봇 `/model_status`로 반영 확인.

### 4-2. 버터대디봇(report, `REPORT_BOT_TOKEN`) 사망 원인
2026-06-23 이후 무응답·리포트 미발송. 배포 로그에서 `report` 봇 관련 에러
(getUpdates 409 Conflict = 토큰 중복 사용 / Unauthorized = 토큰 만료 / `ACTIVE_BOTS` 제외) 확인.
model_router 명령·cron은 이미 종목봇으로 이관 완료라 급하진 않음.

### 4-3. DSInvResearch → 시황봇 릴레이 활성화
- 코드 완료: `src/channel_relay.py` (20분 cron, market spec). 채널이 웹 프리뷰 OFF라 **MTProto 필수**.
- 필요: `TG_SESSION_STRING` 생성. `TG_API_ID/HASH`는 env에 있음. 사용자 PC 없음 →
  이 컨테이너에서 Telethon 2단계 로그인 (send_code → 사용자가 채팅으로 코드 전달 → sign_in).
  참고 스크립트 `scripts/make_tg_session.py` (대화형) — 비대화형 2단계 버전으로 재작성해
  scratchpad에서 실행 (`SETUPTOOLS_USE_DISTUTILS=stdlib pip install pyaes` 후 `pip install telethon`).
  사용자 전화번호 요청 → 코드 요청 → 세션 문자열을 Railway `TG_SESSION_STRING`에 upsert.
- `MARKET_CHAT_ID` 존재 확인 (릴레이 발송 대상).

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
