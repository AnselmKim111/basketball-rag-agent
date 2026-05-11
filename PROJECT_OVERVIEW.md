# 🤖 한국 주식 텔레그램 봇 시스템 — 개관

> 친구에게 "이거 어떻게 만들었어?" 답하는 1장짜리 설명서.
> 코드는 [GitHub repo URL — 형이 채워서 보내] 에 있고, 이 파일만 읽으면 전체
> 그림 + 직접 만들고 싶을 때 필요한 것들 다 보임.

---

## 1. 한 줄 요약

**한국 주식·산업·시황·공시·기술적 신호를 텔레그램에서 자동·on-demand로 받는 7개 봇.** 단일 프로세스 멀티봇 (asyncio), Railway 배포. 핵심 차별점은 종목명 한 단어 입력으로 **DART 공시 + 증권사 리포트 + 웹 딥리서치를 합쳐 한 편의 통합 마크다운 리포트로 받는 `/research`** 와, 헤지펀드 PM 관점 5축 평가로 오늘의 진짜 기회를 짚어주는 `/curate`.

---

## 2. 7개 봇 구성

각 봇은 별도 BotFather 토큰 → 같은 코드베이스에서 같은 asyncio 이벤트 루프 위에 polling. 공유 자원(wisereport 세션, DART API)은 `PIPELINE_LOCK` 1개로 직렬화.

| 봇 | 역할 | 주요 명령 | 자동 작업 |
|---|---|---|---|
| **CompanyBot** | 종목 분석 | `/research <종목명>`, `/curate`, `/deepdive`, `/report` | 없음 (사용자 명령 기반) |
| **IndustryBot** | 산업 리포트 | `/industry`, `/curate`, `/recent` | 매일 09:00 Top10 |
| **MarketBot** | 시황 | `/recent`, `/curate` | 매일 09:00 신규 |
| **GlobalBot** | 글로벌 시장 | `/recent` | 매주 토 09:00 Top10 |
| **IdeaBot** | 테마 → 종목 발굴 | `/idea <테마>`, `/dive`, `/refine`, `/contrarian` | 없음 |
| **DisclosureBot** | DART 공시 알림 | (자동만) | 5분/30분 폴링 |
| **ScreenerBot** | KRX 기술적 신호 | `/screen`, `/status`, `/backfill` | 매일 16:00 |

---

## 3. 핵심 기능 2개

### 3-A. `/research <종목명>` — 통합 딥리서치 (8-15분, ~$0.15)

종목 한 단어 입력 → 5단계 파이프라인:

```
Stage 0: 종목명 → DART corp_code 매칭
Stage 1: 3축 병렬 데이터 수집 (asyncio.gather)
  A. DART (공시 API)
     - 최신 사업보고서 PDF + 텍스트 (사업의 내용 섹션 추출)
     - 최신 IR자료 PDF + 텍스트
     - 분기 재무 (매출/영업이익/순이익, 12분기) + 잠정실적 보강
     - 분기 추이 차트 PNG (matplotlib)
  B. wisereport (증권사 리포트 사이트, Playwright 자동화)
     - 종목 리포트 3건 + 산업 리포트 1건 PDF 다운로드
     - PyMuPDF로 텍스트 추출
  C. perplexity sonar-pro (웹 딥리서치)
     - 최근 90일 핵심 이슈·실적·정책·컨센서스 6-10 bullet
Stage 2: claude-sonnet-4.5로 통합 합성
  - 3축 raw 텍스트 ~30K tokens → 8개 섹션 마크다운 리포트
  - 출처 명시 강제 ([DART 사업보고서], [<브로커명> 리포트], [web])
Stage 3: 발송
  - 통합 리포트 본문 (4000자 청크 분할)
  - 분기 차트 PNG
  - 참고 PDF 묶음 (DART 사업보고서 + IR + 증권사 3건 + 산업 1건)
  - 완료 ack
```

결과 리포트 8개 섹션: Executive Summary / 업의 본질 / 핵심 투자 포인트 / 분기 실적 트렌드 / 최근 90일 핵심 이슈 / 시장 컨센서스 / 리스크·카운터-thesis / 투자 결론.

### 3-B. `/curate` — PM-grade 큐레이션 (5-15분, ~$0.05)

오늘의 Top10 리포트를 헤지펀드 PM 관점 5축으로 평가해 선별:

```
⏱️  타이밍   — 방금 발생/D-N 임박 catalyst인가
🎯 주도섹터  — 시장 흐름을 끌고 가는 영역인가
🆕 새로움    — 1-2주 내 부상한 신규 thesis인가 (정보우위)
💥 크기      — 구조 변화·대형 수주급 임팩트인가
⚡ 시너지    — 위 4축이 동시 충족되는 "킬러 셋업"인가
```

내부 흐름:
1. wisereport에서 최근 5일 리포트 메타 50-100건 수집 (popular + latest, 카테고리별)
2. recon 단계: claude-sonnet이 "오늘의 킬러 셋업"·주도 영역·새로 부상 thesis 정리
3. curate 단계: 5축 0-3점 채점 + 시너지·정보우위·클러스터링(같은 thesis 여러 증권사) 가중치로 Top N JSON 반환
4. PDF 다운 + 각 리포트 kimi-k2.6으로 5000자 요약 + 선별 이유와 함께 발송

---

## 4. 아키텍처

### 단일 프로세스 멀티봇
- `src/orchestrator.py`의 `BOT_SPECS` 리스트가 진실의 원천 — 각 봇의 (이름, 토큰env, builder, 자동 cron job, 자동완성 명령 목록) 1개씩 entry
- 모든 봇이 같은 asyncio 이벤트 루프에서 polling — 메모리 1개, 세션 1개
- 새 봇 추가는 `BOT_SPECS`에 한 줄 append로 끝 (`BOTS.md` 참조)

### PIPELINE_LOCK (직렬화)
- `src/pipeline_lock.py`의 글로벌 asyncio.Lock 1개
- wisereport 세션·DART API 호출은 모두 `async with PIPELINE_LOCK:` 안에서
- 이유: wisereport 동시 로그인 X, DART API rate limit 보호
- 단점: 동시 사용자 작업은 줄서기

### state_store (영속 상태)
- `src/state_store.py` — rpt_id + 정규화 title 두 축으로 중복 발송 차단
- 저장 위치: `RAILWAY_VOLUME_MOUNT_PATH` → `/data` (5GB 영구 볼륨, Railway attach) → fallback `/tmp`
- ScreenerBot은 별도 SQLite (`/data/screener.db`)

### LLM 모델 티어 (비용·지능 trade-off)
| 티어 | 환경변수 | 모델 | 용도 | 비용 |
|---|---|---|---|---|
| Summary | `OPENROUTER_MODEL` | `moonshotai/kimi-k2.6` | PDF 요약·DART·deepdive·idea parse | 갓성비 |
| Fallback | `OPENROUTER_FALLBACK_MODEL` | `anthropic/claude-sonnet-4.5` | retry 3차 폴백 | 안전망 |
| Research | `IDEA_RESEARCH_MODEL` | `perplexity/sonar-pro` | 웹검색 + 인용 | 검색 특화 |
| Narrow | `IDEA_NARROW_MODEL` | `anthropic/claude-haiku-4.5` | 큰 출력 (30→10 narrow) | 빠름 |
| Synthesis | `IDEA_SYNTHESIS_MODEL` | `anthropic/claude-sonnet-4.5` | /research·/curate 최종 합성 | 진짜 지능 |

요약·추출은 절대 sonnet으로 올리지 않음 (비용 6-8배). 진짜 지능 필요한 단계만 sonnet.

### 인가 (chat_id allowlist)
- `*_ALLOWED_CHAT_IDS` env에 콤마 구분 chat_id 또는 `*` (open mode)
- closed mode (chat_id 명시) ↔ open mode (`*`) 토글은 env 1줄로 즉시 가능

---

## 5. 외부 의존성 (직접 만들면 필요한 것들)

| 자원 | 발급처 | 비용 | 용도 |
|---|---|---|---|
| **텔레그램 봇 토큰** 7개 | [BotFather](https://t.me/BotFather) `/newbot` | 무료 | 각 봇 인터페이스 |
| **OpenRouter API 키** | [openrouter.ai/keys](https://openrouter.ai/keys) | pay-as-you-go (`/research` 1회 ~$0.15) | LLM 호출 통합 게이트웨이 |
| **DART API 키** | [opendart.fss.or.kr](https://opendart.fss.or.kr) | 무료 | 공시·재무 데이터 |
| **wisereport ID/PW** | [wisereport.co.kr](https://wisereport.co.kr) 회원가입 | 무료 (개인 1계정) | 증권사 리포트 PDF |
| **Railway 계정** | [railway.app](https://railway.app) | $5/월 hobby plan | 24/7 호스팅 |
| (선택) FinanceDataReader | 자동 설치 | 무료 | 시총·섹터 폴백 |
| (선택) pykrx | 자동 설치 | 무료 | KRX 데이터 폴백 |

### 환경변수 (Railway에 inject할 것들)

```bash
# 봇 토큰 (BotFather에서 7개)
TELEGRAM_BOT_TOKEN=...      # CompanyBot
INDUSTRY_BOT_TOKEN=...
MARKET_BOT_TOKEN=...
GLOBAL_BOT_TOKEN=...
IDEA_BOT_TOKEN=...
DISCLOSURE_BOT_TOKEN=...
SCREENER_BOT_TOKEN=...

# 권한 (closed: 본인 chat_id, open: "*")
ALLOWED_CHAT_IDS=1234567890   # 또는 "*"
INDUSTRY_ALLOWED_CHAT_IDS=...   # 봇마다 1개씩, 총 7개
# ... (각 봇 분 7개)

# 자동 작업 push 대상 (closed 시 본인 chat_id 1개)
TELEGRAM_CHAT_ID=1234567890
INDUSTRY_CHAT_ID=...
# ... (각 봇 분 7개)

# LLM
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=moonshotai/kimi-k2.6
OPENROUTER_FALLBACK_MODEL=anthropic/claude-sonnet-4.5
IDEA_RESEARCH_MODEL=perplexity/sonar-pro
IDEA_NARROW_MODEL=anthropic/claude-haiku-4.5
IDEA_SYNTHESIS_MODEL=anthropic/claude-sonnet-4.5

# 데이터 소스
DART_API_KEY=...
WISEREPORT_ID=...
WISEREPORT_PW=...

# 저장
STATE_DIR=/data   # Railway 5GB 볼륨이 /data에 attach
```

---

## 6. 코드 구조 (어디에 뭐가 있는지)

```
basketball-rag-agent/
├── src/
│   ├── orchestrator.py         # 진입점. BOT_SPECS + asyncio loop + APScheduler
│   ├── bot_helpers.py          # 공유 헬퍼 (인가, send_text_chunked, send_pdf)
│   ├── pipeline_lock.py        # 글로벌 직렬화 락
│   ├── state_store.py          # 중복 발송 차단 (rpt_id + title)
│   │
│   ├── bot_worker.py           # CompanyBot (/research, /curate, /deepdive, /report)
│   ├── category_bots.py        # Industry/Market/GlobalBot
│   ├── idea_bot.py             # IdeaBot (테마 → 종목 발굴, 5단계)
│   ├── disclosure_bot.py       # DART 공시 폴링
│   ├── screener_bot.py         # KRX 기술적 신호
│   │
│   ├── deep_research.py        # ⭐ /research 5단계 파이프라인
│   ├── curator.py              # /curate 5축 PM-grade 평가
│   ├── summarizer.py           # OpenRouter wrapper + retry/fallback
│   ├── wisereport.py           # wisereport Playwright 자동화
│   │
│   ├── deepdive/               # /deepdive 모듈 (DART 공시 분석)
│   │   ├── dart_client.py
│   │   ├── chart.py            # 분기 재무 차트 PNG
│   │   ├── wisereport_context.py
│   │   └── forward_consensus.py
│   │
│   └── screener/               # ScreenerBot 모듈 (KRX OHLCV·신호)
│       ├── signals.py          # 4종 신호 (신고가, 거래량 돌파, VCP, 52주 직전)
│       ├── data_source.py      # Naver·pykrx·FDR 3단 폴백
│       ├── incremental.py      # 일일 업데이트 + retry
│       └── validator.py        # 발송 직전 cross-validation
│
├── prompts/
│   ├── deep_research.txt       # /research 합성 프롬프트
│   ├── deepdive_business.txt   # 업의 본질 요약
│   ├── deepdive_ir.txt         # IR 핵심 투자포인트
│   └── (...)
│
├── CLAUDE.md                   # AI 어시스턴트용 코드베이스 가이드
├── BOTS.md                     # 새 봇 추가 절차
├── Dockerfile                  # Python 3.10 + Playwright + Chromium
└── requirements.txt
```

---

## 7. 배포 (Railway)

1. GitHub repo fork/clone
2. Railway에서 새 프로젝트 → "Deploy from GitHub repo" 선택
3. 위 환경변수들 Variables 탭에 inject
4. 5GB Volume 1개 attach (`/data` mount)
5. Dockerfile이 자동 인식 → 빌드 → 24/7 polling 시작

**비용 추정** (1인 사용 기준):
- Railway hobby plan: $5/월
- OpenRouter: 사용량 비례
  - `/research` 1회 ~$0.15
  - `/curate` 1회 ~$0.05
  - `/idea` 1회 ~$0.10-0.20
  - 매일 자동 시황·산업 push (09:00): ~$0.20/일 → 월 ~$6
  - 가벼운 사용 월 $10-20 / 진지한 사용 월 $30-50

---

## 8. 만들면서 배운 것들 (혹시 친구가 참고하면 좋은)

1. **wisereport Playwright 세션 재사용** — 매번 새로 로그인하면 IP 차단 위험. `storage_state.json`으로 쿠키 영속 + `ensure_logged_in()` 패턴.
2. **PIPELINE_LOCK 1개로 직렬화** — 단순하지만 효과적. 동시성 늘리려다 wisereport 차단·DART rate limit·OpenRouter 동시 호출 폭증 다 잡았다.
3. **LLM 모델 티어 분리** — 요약은 kimi (~$0.001/회), 합성은 sonnet (~$0.10/회). 한 단계라도 잘못 올리면 비용 5-10배.
4. **state_store 영속화** — Railway 볼륨 없이 `/tmp` 쓰면 재배포마다 중복 알림. `/data` mount는 필수.
5. **3축 폴백** — Naver Finance 1순위, pykrx 2순위, FDR 3순위. 단일 소스 신뢰는 위험 (시뮬레이션 환경에서 lag 가능).
6. **각 단계 graceful degradation** — DART 실패해도 wisereport+web로 합성 진행. 한 축 망가져도 결과는 나옴.
7. **claude-sonnet 합성 프롬프트의 "출처 명시 강제"** — 환각 줄이는 가장 강력한 한 줄. `"각 주장은 [DART], [<broker>], [web] 중 출처 명시"` 룰.

---

## 9. 라이센스·면책

개인 사용 목적. wisereport는 회원가입 후 개인 1계정 사용 — 계정 공유는 ToS 위반 가능. DART API는 무료지만 분당 호출 제한 있음. 이 시스템은 투자 자문이 아니고 정보 처리 도구. 모든 투자 결정과 손익은 사용자 책임.

---

질문 있으면 형(코드 만든 사람)한테 텔레그램으로.
