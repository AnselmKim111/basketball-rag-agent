# HANDOFF — 세션 인계 문서 (2026-10-22 기준)

새 세션은 이 파일을 먼저 읽고 **§0(보안사고)** → §4 "즉시 할 일" 순으로 진행할 것.
CLAUDE.md(작업 규약)와 함께 적용. **시크릿 값은 이 파일에 절대 쓰지 않는다** — env 이름만.

---

## 0. 🚨 2026-10-21 Railway env 유출 사고 — 복구 진행 중 (최우선)

**사고**: Railway가 고객 환경변수 저장소 침해를 공지(10-21, status.railway.com/incident/QH8L8B3E).
암호화된 env에 접근, 일부는 복호화됨. 빌드·배포 전면 중단(10-22 기준 미재개, 실행 중 컨테이너는
구 env로 계속 동작). OpenRouter가 키 3개를 선제 비활성화(10-22, 401 'User not found.') → **모든
LLM 기능 중단**. Claude가 쓰던 `RAILWAY_PROJECT_ACCESS_TOKEN`도 폐기됨('Project Token not found')
→ 세션에서 Railway 로그·Variables 조회 불가.

**조사 결과 (2026-10-22, 워크플로 7+56 에이전트)**:
- git 이력(전 브랜치 391커밋·1,124 blob)에 실제 시크릿 커밋 0건. GitHub 마지막 push 09-25, 외부 커밋·브랜치 없음.
- 차트 채널(@krchartkim·@uschartkim) 10-21까지 정상 게시, 스팸·삭제 흔적 없음 → 봇 토큰 악용 흔적 없음
  (Railway 로그를 못 봐서 확정은 아님 — 409 Conflict·'중복 로그인 감지' 로그로 재확인할 것).
- **최고 위험**: `TG_SESSION_STRING`(사용자 **개인 텔레그램 계정** 세션 — 비번·코드 없이 계정 전체 접근) +
  `TG_API_ID/HASH`; `TG_LOGIN_PASSWORD`(2FA, 09-06 env 경유); `RAILWAY_ACCESS_TOKEN`(런타임 env에 저장돼
  있었음 → 유출자가 Variables 읽기·**쓰기**·재배포 가능 → 주입된 변수 감사 필요);
  `OPENROUTER_MANAGEMENT_KEY`(설정돼 있었다면 부팅 로그에 앞 60자 평문 출력되던 버그 — 수정됨).
- 봇 토큰 12개(이 프로젝트): @AnselmsSlave2·3·4·5·6·8·9·10·11·12·13·14 (2 종목·3 산업·4 시황·5 글로벌·
  6 아이디어·8 국장스크리닝·9 공시·10 미장스크리닝·11 어닝콜·12 버터대디(report-bot 서비스)·13 미장차트·
  14 국장차트). 1(꼬붕이)·7(김비서)은 다른 프로젝트(Dailylife 등) — 같은 사고 범위.
- 기타 시크릿: OPENROUTER_API_KEY, OPENAI_API_KEY, WISEREPORT_ID/PW, DART_API_KEY, FMP_API_KEY,
  ALPHA_VANTAGE_KEY, API_NINJAS_KEY, FINNHUB_API_KEY, (있으면) KRX_ID/PW·RECAP_BOT_TOKEN·ANTHROPIC_API_KEY.

**사용자 조치 순서 (사용자 계정 필요 — Claude 대행 불가, 순서 중요)**:
1. 텔레그램 설정 → 기기: 다른 세션 전부 종료 → 2FA 비밀번호 변경 → 개인정보·보안 → 활성 웹사이트
   연결 해제, (Premium이면) 비즈니스 → 챗봇 연결 확인. **반드시 2번보다 먼저** (유출 세션으로
   BotFather 대화를 보면 새 토큰도 샌다).
2. BotFather `/revoke` — 위 12개(+ Dailylife 쪽 1·7). 새 토큰은 채팅에 붙이지 말고 Railway에 직접 입력.
3. OpenRouter: Activity(키별) 내보내기·검토 → 새 키 발급(크레딧 한도 설정, 서비스별 1개) → management
   키가 있었다면 폐기(런타임엔 다시 넣지 않음). 비활성 키는 검토 후 삭제.
4. Railway: 감사 로그 확인(변수 조회·추가, 멤버 초대, 웹훅, 배포) → **두 서비스+Shared Variables 이름을
   §0 목록과 대조해 모르는 변수 삭제**(유출 토큰으로 주입 가능했음) → GitHub 재연결 + autodeploy 재활성
   ('Wait for CI'는 켜지 말 것 — CI가 09-24부터 계속 실패 중) → 새 값을 **sealed 변수**로 한 번에
   staged 입력 → Deploy 1회. report-bot 서비스(idle)는 시크릿 전부 삭제(또는 서비스 삭제 — 확인 후).
   구 배포 Rollback 금지(옛 변수 스냅샷 복원).
5. 나머지 재발급: WISEREPORT_PW, DART_API_KEY, FMP/AV/API Ninjas/Finnhub/OpenAI 키, (있으면) KRX_PW.
6. Railway 프로젝트 토큰: **런타임 env에 다시 넣지 않는 것을 권장**(넣으면 env 유출 = 전체 장악). 없으면
   /model_approve·Layer D는 수동 적용 안내로, tg_login은 비활성으로 동작(코드 대응 완료). Claude 세션용
   토큰은 Claude Code 환경 '와이즈리포트'에만 새로 발급해 저장.
7. 채널 릴레이 세션: PC에서 `scripts/make_tg_session.py` **대화형 모드**로 생성 → 종목봇 서비스의
   `TG_SESSION_STRING`만 설정 (TG_LOGIN_* 경로 사용 금지 — 2FA가 다시 Railway env를 거침).
   가능하면 보조 계정 사용 권장(개인 계정 세션을 서버에 두지 않기).
8. Claude Code 환경 '와이즈리포트' 시크릿 갱신(OPENROUTER_API_KEY, RAILWAY_PROJECT_ACCESS_TOKEN,
   WISEREPORT_ID/PW, TG_API_ID/HASH).
9. Dailylife·Test_Railway_for_Agent 등 다른 Railway 프로젝트도 1~5 동일 적용.

**배포 후 확인 (Claude가 할 일 — Railway 토큰 확보 시)**: 부팅 로그 `[orch] relevant env vars`에 값 대신
`<set …>`만, 모든 봇 `폴링 시작`, 관리자 채팅 '🔐 자격증명 점검' 메시지 전부 ✓, IDEA/EARNINGS/
SCREENER self-test(CLAUDE.md §1), 장애 기간 누락분 복구(/earnings 재실행, /recent, DART 공시).

**코드 대응 (commit 1a184e9 + 리뷰 반영)** — 키 교체·재배포가 안전하게 돌도록:
- 로그 마스킹(`src/log_redact.py`): 시크릿 env 값·봇토큰·apikey 쿼리·sk-or 키·StringSession 마스킹,
  부팅 diag deny-by-default(비밀번호류는 `<set>`만), /report 서브프로세스 env 최소화.
- 봇별 기동 격리(orchestrator): 토큰 거부된 봇만 건너뛰고 cron은 살아있는 봇으로 재배치 — 예전엔 토큰
  1개만 무효여도 전체 크래시루프 + 트레이스백에 토큰 평문. 일시 오류는 재시도.
- 부팅 시 '🔐 자격증명 점검' 관리자 메시지(env 이름별 ✓/✗, 값 미출력, `CRED_SELF_CHECK=0`으로 끔).
- OpenRouter 401 차단기(summarizer): 재시도·폴백·health 기록 없이 즉시 차단 + 관리자 알림 1회,
  사용자에겐 '키 무효(401)' 안내 (`OPENROUTER_AUTH_COOLDOWN_S`, 기본 900).
- Layer D: 401 직후·다중 프로바이더 동시 장애 시 롤백 보류, 토큰 없으면 수동 안내, 제외 프로바이더
  (anthropic) 복원 금지. `/model_*` 승인 명령은 `*` 불인정(숫자 chat id 필요).
- 텔레그램 세션: 무효 세션에서 `input()` 대기·20분마다 traceback 제거, 1회 알림 후 MTProto 중단.
  tg_login은 Railway 토큰 실검증 후에만 동작, TG_LOGIN_* 모든 종료 경로에서 삭제, 단일 서비스에만 반영.
- 전송: `verify=False` 제거(텔레그램·DART, `DART_TLS_VERIFY=0` 탈출구), Finnhub 헤더 인증, 키 포함 URL 로그 제거.
- 장애 중 dedup: 카테고리 리포트·공시(`DISCLOSURE_SEND_MAX_ATTEMPTS`)·어닝 watch가 실패분을 '발송됨'으로
  찍지 않음, pre-call 실패 스팸 차단(인증 장애 기록은 메모리만 → 재배포 후 즉시 재시도).

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
| `RAILWAY_PROJECT_ACCESS_TOKEN` | Railway GraphQL — Variables 조회/upsert, 재배포, 배포 로그 (`Project-Access-Token` 헤더) — **2026-10-22 폐기됨, 재발급 대기(§0)** |
| `OPENROUTER_API_KEY` | LLM 호출·canary·A/B 테스트 — **2026-10-22 OpenRouter가 비활성(401), 재발급 대기(§0)** |
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
`RECAP_BOT_TOKEN`도 어디에도 없어 RecapBot 전용 봇은 스킵 — 대신 **종목봇에 합류**(2026-09-06):
`/recap` `/recap_global` `/recap_me` 명령 + 일 19:00 cron(`recap_weekly_company`, REPORT_CHAT_ID로
발송). 나중에 전용 토큰을 넣으면 전용 봇이 담당하고 종목봇 cron은 자동 스킵(중복 가드).

### 4-3. DSInvResearch → 시황봇 릴레이 활성화 — ✅ 세션 생성 완료 (2026-09-06)
- `TG_SESSION_STRING`·`TG_API_ID`·`TG_API_HASH` 종목봇 서비스 Railway env에 반영됨.
  릴레이(`src/channel_relay.py`, 20분 cron, MARKET_CHAT_ID 발송)는 자동 활성.
- 방법: Claude Code 컨테이너는 TLS 재종단 프록시라 MTProto 불가 → `src/tg_login.py`가
  Railway 컨테이너 부팅 시 env 주도로 2단계 로그인 대행 (TG_LOGIN_PHONE → 코드 →
  TG_LOGIN_CODE(+TG_LOGIN_PASSWORD) → sign_in → TG_SESSION_STRING upsert → TG_LOGIN_* 자동 삭제).
  ~~세션이 만료/로그아웃되면 같은 절차 반복~~ → **2026-10-22 사고 이후 폐기된 절차.** 재생성은 §0-7
  (PC에서 `scripts/make_tg_session.py` 대화형 → 종목봇 서비스 `TG_SESSION_STRING`만 설정).
- 보안 메모: 2단계 비밀번호가 채팅에 노출됨 — 사용자에게 변경 권고함. 세션은 비밀번호 변경과
  무관하게 유효.
- 확인 포인트: 배포 로그 `channel_relay` 라인(20분 간격)에 MTProto 읽기 실패가 없어야 정상.

### 4-3b. 신고가봇(KR 스크리너) 정확도 복구 — ✅ 완료 (2026-09-23)
- 증상: 소부장 52주 신고가 근접 종목 누락 + 하단 🧩 중소형 22종목 노이즈.
- 원인: pykrx(KRX) 2026-09부터 로그인 필수 → 매일 실패 → 16:26 Naver 폴백 = NXT
  애프터마켓 드리프트 구간 → validator가 매일 75~85% "불일치" 탈락 (219→37).
- 조치: 정규장(15:30) 분봉 기준 오늘 row 교체(저장·검증 동일 경로), Naver 1순위 +
  6-worker 병렬(6분), 🧩 섹션 제거(핵심 3섹션에 시총 표기로 합류), EPS 열 자동 숨김,
  차트 채널 base_date별 dedupe. 정정판 self-test: validated 219 / rejected 0.
- 2차(같은 날): 사용자 "미미 수준으로 깔끔하게" → 메시지를 미미 포맷으로 전면 교체
  (업종별 신고가 한 줄 + ★ + 소형주 버킷 + 돌파직전, 나머지 섹션 미표시), 장중고가 기준
  신고가(`high_52w_hi`) 추가, Naver 업종명 그룹핑(`sector_naver` 캐시). CLAUDE.md §6 참조.
- 내일(09-24) 16:00 cron이 첫 정식 실행 — 로그 `[validator] 완료 ... rejected=0` 확인.
- 남은 개선 후보: near_breakout의 `vol_trend ≥ 1.3` 게이트가 소부장 눌림목을 거를 수
  있음(백테스트 없이 손대지 않음). pykrx 부활은 KRX 계정(KRX_ID/KRX_PW) 필요 — 사용자
  결정 사항, Naver로 충분해 보류.

### 4-3c. 미국 스크리너 커버리지 복구 — (2026-09-25)
- 증상: "61종목 계산 · 458종목 base_date 누락". 원인·원칙·실측은 CLAUDE.md §6-US.
- 리뷰 워크플로 2라운드(1차 80 에이전트/36 확정, 2차 94 에이전트/32 확정) 반영 후 배포.
- 배포 첫 실행은 신규 ~2,040종목 5년 백필(~6분) 포함 — self-test로 선반영.

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
