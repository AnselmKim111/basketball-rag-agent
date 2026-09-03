# IdeaBot 프로젝트 — Claude Code 작업 규약

## 1. 자동 검증 의무 (모든 작업)

사용자가 시키는 **모든 작업의 결과 품질에 대해 Claude가 직접 책임**진다. 다음 루프를 자동으로 돌려 완벽한 퀄리티를 달성할 때까지 사용자에게 검증 요청하지 말 것:

1. **구현** — 코드/프롬프트/설정 변경
2. **푸시 + 배포** — Railway 자동 빌드 트리거
3. **테스트 인풋 주입** — `IDEA_TEST_PROMPT` env로 self-test 자동 실행 (또는 동등한 검증 메커니즘)
4. **로그 직접 확인** — Railway GraphQL `deploymentLogs`로 단계별 로그 fetch & 분석
5. **문제 발견 → 자동 수정** — log에서 실패/이슈 발견 시 즉시 코드 수정 + 재푸시
6. **반복** — end-to-end 성공할 때까지 (Iteration 1, 2, 3, ... 자동)
7. **클린업** — 검증용 env vars 제거 + 사용자 정상 모드 복귀
8. **최종 보고만** — 사용자에게는 검증 끝난 후 요약·결과만 알림

### 진행 중에 사용자에게 묻지 말 것
- ❌ "이렇게 했는데 봐주세요"
- ❌ "다음 단계 진행할까요?"
- ❌ "결과 어떤가요?"
- ✅ 자동으로 다음 iteration 진행
- ✅ 모든 시도 후 최종 결과만 보고

### 인지적 오류 회피 (idea_bot 특화)
- **availability heuristic**: 검색에 자주 나오는 종목·리포트 많은 종목을 무의식적으로 우대 금지
- **size bias**: 시총 큰 종목 자동 우대 금지 — specialty pure-play(중·소형주) 의도적 발굴
- **purity 우선**: 사업부 비중·매출 노출도 정량 명시 안 되면 후순위

## 2. 모델 티어 (절대 무너뜨리지 말 것)

| 티어 | env var | 모델 (2026-09-03 라이브가격 재평가) | chain 2차 | 용도 |
|---|---|---|---|---|
| Summary | `OPENROUTER_MODEL` | **deepseek-v4-flash** ($0.089/$0.177 — kimi 대비 11-23x↓) | kimi-k2.6 | PDF 요약·DART·Forward·deepdive·idea parse |
| Research | `IDEA_RESEARCH_MODEL` | perplexity/sonar-pro (저볼륨 — 품질 유지) | — | 1단계 웹검색 |
| Narrow | `IDEA_NARROW_MODEL` | **deepseek-v4-flash** (출력단가 haiku 대비 28x↓, JSON OK) | qwen3.7-plus | 3단계 30→10 (큰 출력) + parse 폴백 |
| Synthesis | `IDEA_SYNTHESIS_MODEL` / `REPORT_SYNTHESIS_MODEL` | **deepseek-v4-pro** ($1.04/$2.08 — 비-Anthropic 지시, sonnet 대비 출력 7x↓) | gpt-5.1 (프론티어 백업) | importance + synthesis + 시황 narrative |
| Deep | `EARNINGS_SYNTHESIS_MODEL` | **gpt-5.2** ($1.75/$14 — 비-Anthropic 최상급, opus 대비 44%↓) | — | 어닝 비교합성 (최고 지능) |
| Fallback | `OPENROUTER_FALLBACK_MODEL` | kimi-k2.6 (1차와 프로바이더 분리) | — | chat_with_retry 3차 시도 안전망 |

요약·추출 작업을 상위 티어로 올리지 말 것 (비용 30-80배). **비-Anthropic 지시 (2026-09-03)** — Anthropic 모델은 기본값·chain에서 제외 (router 후보 풀에는 유지, 추천은 사용자 승인 게이트).
코드 기본값은 `src/llm_models.py` — **Railway env가 우선하므로 활성화하려면 env도 갱신**
(`/model_eval` → `/model_approve` 경로가 canary 검증 포함이라 안전, 또는 Variables 수동 수정).
가성비 티어 신모델 전환 후 1주는 model_router Layer D(시간당 rollback 검사)가 실패율 감시.

### Model Router (src/model_router/ — 주간 자동 재평가)
- 매주 일요일 21:00 KST cron: OpenRouter /models 가격 + /activity 사용량으로 가성비 재평가
  → 텔레그램 추천 (`/model_approve <id|all>` 승인 시 Railway env 자동 upsert).
- **티어별 가중** (scorer.TIER_WEIGHTS): 가성비 티어(Summary/Narrow/Fallback) cost 0.4 /
  품질 티어(Synthesis/Deep) evidence 0.45-0.50 — 미검증 신모델로 품질 티어 강등 추천 금지.
- **자동 발굴** (recommender._discover_candidates, 2026-09): 수동 후보 리스트 병목 제거 —
  라이브 카탈로그 전체(424+)에서 티어 제약(ctx·가격·JSON·reasoning) 통과 모델 자동 편입.
  품질 티어(D/E)는 min_out_price 하한 + flash/lite/mini/nano/code 이름 제외 (증거 없는
  신모델 스코어가 비용으로 붕괴 → 초소형 모델 유입 방지). 평가 메시지에 [티어 현황]
  섹션 추가 — 이미 최적/보류 사유 표시.
- **프로바이더 제외** (recommender): `MODEL_ROUTER_EXCLUDE_PROVIDERS` (기본 `anthropic`
  — 사용자 지시 2026-09-03). 랭킹 단계 필터라 automatic 경로 포함 어떤 추천에도 안 뜸.
  해제하려면 env를 빈 문자열로. X_fallback은 Summary 1차와 같은 프로바이더 자동 제외
  (분리 원칙 강제 — kimi→deepseek 같은 역추천 차단).
- **4 Layer 안전망**:
  A=canary smoke test (적용 전 한국어·JSON sentinel, 실패 시 변경 거부) ·
  B=pydantic schema (`src/llm_schemas.py` — **프롬프트 실제 출력과 1:1 대조 필수, 추정 금지**) ·
  C=force_json (response_format, 미지원 모델 400 자동 off) ·
  D=auto rollback (시간당 cron, 1h fail_rate >20% 또는 p95 >30s 시 이전 모델 복원).
- 명령: **종목봇**(TELEGRAM_BOT_TOKEN)에서 `/model_eval` `/model_approve` `/model_reject`
  `/model_status` — 버터대디봇 사망(2026-06-23~)으로 이관. cron 2개(주간 재평가 +
  Layer D)도 company spec으로 이동. 알림 대상: REPORT_CHAT_ID → MODEL_ROUTER_CHAT_ID
  → ALLOWED_CHAT_IDS 첫 id 폴백.

## 3. 배포 흐름

- **개발 브랜치**: `claude/idea-bot-stock-analysis-i6HuR`
- **Railway 실제 watch 브랜치 (2026-06-07 확인)**: `claude/stock-screening-feature-2Jo4X`
  → `deployment(id).meta.branch`로 확인. 이 브랜치에 push 안 하면 deploy 안 됨.
- **보조 브랜치**: `claude/wisereport-auto-downloader-C7C8l` (다른 세션 공유용)
- **푸시 패턴**: 세 브랜치 모두에 push 필수:
  ```bash
  git push origin claude/idea-bot-stock-analysis-i6HuR:claude/stock-screening-feature-2Jo4X  # deploy 트리거
  git push origin claude/idea-bot-stock-analysis-i6HuR:claude/wisereport-auto-downloader-C7C8l
  git push origin claude/idea-bot-stock-analysis-i6HuR
  ```
- **다른 세션 충돌 시**: `git fetch origin <branch> + git merge --no-edit FETCH_HEAD` 후 재푸시.
- **분리 배포 가드**: `ACTIVE_BOTS=screener,us_screener` env 설정 시 그 봇만 기동 (미설정이면
  토큰 있는 봇 전부). 두 Railway 서비스가 같은 봇 토큰을 공유하면 텔레그램 getUpdates
  409 Conflict — 서비스별로 ACTIVE_BOTS 명시해 차단 (`src/orchestrator.py`).

## 4. 자동 검증 도구

- **Self-test**: `IDEA_TEST_PROMPT` env 설정 → 부팅 후 `_self_test()` 자동 실행 (`src/idea_bot.py:build_idea_app`).
- **로그 fetch**: Railway GraphQL `deploymentLogs(deploymentId, limit, startDate)` — Project-Access-Token 헤더 사용.
- **테스트 종료 신호**: 로그에 `[send_results 완료]` / `최종 분석 실패` / `OpenRouter 한도 초과` 중 하나.
- **모니터 도구**: `Monitor` tool로 30초 간격 폴링 + 로그 신규 분석.

## 5. 토큰·자격증명

토큰들은 git에 커밋 안 됨 — `CLAUDE.local.md` 참조 (gitignored).

---

## 6. ScreenerBot — 한국 주식 기술적 신호 (별도 봇)

스크리닝봇은 IdeaBot/wisereport 와 격리. 자체 SQLite (`/data/screener.db`),
별도 cron(매일 16:00 KST), 자체 텔레그램 봇 토큰.

### 핵심 파일
- `src/screener_bot.py` — 봇 entrypoint, `screener_daily_job` (cron), `_self_test`
- `src/screener/signals.py` — 신호 계산 (base_date-anchored, **반드시** base_date 명시)
- `src/screener/incremental.py` — `update_today` (cron), `update_specific_date` (강제 fetch),
  `ensure_recent_business_day_data` (가장 최근 영업일 보장)
- `src/screener/data_source.py` — Naver/pykrx/FDR 통합. `fetch_ohlcv_by_ticker_via_naver`가 1순위
- `src/screener/validator.py` — 발송 직전 cross-validation (Naver 재 fetch ↔ DB)
- `src/screener/formatter.py` — 미미 스타일 섹터별 그룹핑 출력
- `src/screener/db.py` — SQLite (tickers, ohlcv, signals, meta), 280일 retention
- `src/screener/universe.py` — KOSPI/KOSDAQ 보통주 + 시총 + 섹터 갱신

### 데이터 소스 우선순위 (절대 순서 지킬 것)
1. **Naver Finance siseJson API** — 1순위 (시뮬레이션 환경에서도 정확)
2. **pykrx** date-batch — 폴백
3. **FDR** ticker-batch — 최후 폴백 (cap+timeout 보호)

시총·섹터:
- 시총: pykrx fetch_market_cap → FDR StockListing Marcap 폴백
- 섹터: pykrx 업종지수(1004~1026) → FDR Industry → 종목명 keyword 휴리스틱(28카테고리)

### 신호 (4종)
1. `high_all` — 보유 데이터(280일) 역사적 신고가
2. `high_52w` — 252영업일 신고가
3. `volume_breakout` — 오늘 거래량 ≥ 20일 평균 × 2.0 + 종가 상승
4. `near_breakout_52w` — 52주고점 95~99% + 5일 거래량 ≥ ×1.3
5. `vcp_breakout` — 50일 박스권(≤1.20) + ATR 30%+ 수축 + 거래량 dry-up + 거래량 동반 상방돌파

### 이중확인 구조 (절대 깨지 말 것)
신호의 정확성을 두 단계로 보장:
1. **base_date-anchored signals**: `compute_all(base_date=...)` — 모든 종목이 동일 날짜의
   close를 today로 사용. base_date 데이터 없는 종목은 자동 skip (`skipped_no_base`).
   `compute_signals_for_ticker(rows, base_date)`가 base_date row를 explicit lookup.
2. **cross-validation**: `validator.cross_validate(results, base_date)` — 신호 발생
   종목들의 close를 Naver historical API로 재 fetch → DB값과 비교. 불일치/fetch 실패 시
   그 종목 메시지에서 자동 제거 (`rejected`).

이 둘 중 하나라도 깨지면 잘못된 chg_pct가 메시지에 표시될 수 있음.

### 환경변수
- `SCREENER_BOT_TOKEN` — 텔레그램 봇 토큰
- `SCREENER_ALLOWED_CHAT_IDS` — 권한 chat id (콤마)
- `SCREENER_CHAT_ID` — 자동 발송 대상 chat id
- `SCREENER_TEST_MODE=1` — 부팅 시 self-test 자동 실행
- `SCREENER_MIN_MARKET_CAP=300_000_000_000` — 시총 필터 (3000억)
- `SCREENER_RETRY_INTERVAL_S=300`, `SCREENER_RETRY_MAX=6` — today fetch retry (16:00 cron이
  KRX 미발행 대비 16:30까지 5분 간격 재시도)
- `SCREENER_NAVER_CAP=1200`, `SCREENER_NAVER_TIMEOUT_S=600` — Naver fetch 보호
- `SCREENER_FORCE_REFETCH=1` — cached 무시하고 매번 Naver 재 fetch (정확성 위해 켬)
- `SCREENER_FORCE_BACKFILL=<토큰>` — 강제 백필 **one-shot**: 같은 토큰은 1회만 소비
  (meta `force_backfill_consumed`). 재실행은 값을 바꿔서(예: 날짜).
- **Railway env 스냅샷 주의 (실측 2회)**: `variableDelete`는 실행 중 컨테이너에 반영 안 됨
  (재배포도 자동 트리거 안 됨) → 검증용 env(TEST_MODE 등) 삭제 후 반드시
  `serviceInstanceRedeploy(environmentId, serviceId)` mutation 호출 + 새 deployment
  SUCCESS 확인까지가 정리 절차.
- `SCREENER_VALIDATE_TIMEOUT_S=60`, `SCREENER_VALIDATE_TOLERANCE=1`(원) — validator 보호
- `SCREENER_INCREMENTAL_FDR_FALLBACK=0` — FDR 폴백 비활성 (sequential hang 방지)

### Cron
매일 **16:00 KST** (15:30 장마감 + 30분 정산 버퍼). KRX 미발행이면 5분×6회 retry → 그래도
미수신이면 `ensure_recent_business_day_data`로 직전 영업일 fetch.

### 메시지 포맷 (미미 스타일)
- 섹션: 🚀 역사적 신고가 / 📈 52주 신고가 / 💎 VCP 돌파 / 🔥 거래량 돌파 / 🎯 52주 돌파 직전
- 섹션 안에서 섹터별 그룹핑: `(반도체) 삼성전자(+5.2%), SK하이닉스(+3.1%)`
- KOSPI 우선 정렬 (각 섹터 내부)
- 헤더에 base_date + 검증 종목 수 + 이중확인 통과 수 명시

### 알려진 환경 한계
시뮬레이션 환경에서 외부 KRX/Naver/FDR 데이터에 lag/forward-fill 가능성. 따라서
이중확인 구조가 핵심. 단일 소스만 신뢰하면 잘못된 chg_pct 발생 — 사용자에게 검증된 사례
존재 (예: 첫 self-test에서 삼성E&A +21.5% 잘못 표시 → fix 후 -3.11% 정확).

---

## 7. EarningsBot — 미국 기업 어닝콜 + 비교 PDF (전용 봇)

**전용 텔레그램 봇** (`EARNINGS_BOT_TOKEN`, @AnselmsSlave11bot). orchestrator
`BOT_SPECS`에 항목 1개로 등록 — 자체 polling. 진입: 자유 텍스트 또는 `/earnings`.
(이전엔 종목봇에 `register_handlers`로 통합했으나, 자연어 라우터 간섭·메뉴 혼선 때문에
독립 봇으로 분리. `build_earnings_app(token)`이 start·help·earnings·자유텍스트 핸들러 등록.)

미국 상장사 한정. **진짜 전문(full transcript) grounding → 종목별 심층추출 → 숫자 교차검증
→ Opus 비교합성**의 딥리서치급 파이프라인. deep_research.py 수준(인용·정량·시나리오, 8000자+) 목표.

### 품질 설계 (왜 이렇게)
이전 버전은 perplexity "찾아서 요약" 단일 호출이라 전문 비근거·환각·얕음. 그래서:
1. **grounding**: 진짜 전문 텍스트 확보 (FMP→API Ninjas→스크레이프→perplexity요약 폴백).
   `grounded` 플래그로 신뢰도 추적, 비근거 시 보고서에 ⚠️ 경고.
2. **심층추출**: 전문 전체를 sonnet이 읽어 verbatim 인용 + 전체 Q&A + segment 구조화.
3. **교차검증**: 추출 숫자(rev/capex) ↔ SEC 분기 데이터 대조, 불일치 flag (스크리너 철학).
4. **Opus 합성**: 인용강제 + 시나리오(bull/bear/base) + 표, 8섹션.

### 핵심 파일
- `src/earnings_bot.py` — 봇 entrypoint, `_run_pipeline` (8단계), `_self_test`
- `src/earnings/transcript_source.py` — **진짜 전문 확보** (프로바이더 체인 + `resolve_year_quarter`)
- `src/earnings/transcripts.py` — `fetch_and_extract` (grounded 전문 위 sonnet 심층추출) + `expand_criteria_to_tickers`
- `src/earnings/sec_edgar.py` — SEC EDGAR Company Facts (FY 6년 + 분기 flow). ticker→CIK 캐시, rate limit 6.6 req/s
- `src/earnings/verify.py` — 숫자 교차검증 (콜 ↔ SEC)
- `src/earnings/charts.py` — matplotlib 차트 6종
- `src/earnings/pdf_report.py` — PdfPages PDF (표지/비교합성/검증/차트/종목별/커스텀/부록)
- `prompts/earnings_parse.txt` — 입력 파싱 (mode/tickers/fiscal_year+quarter/custom)
- `prompts/earnings_extract.txt` — 전문 심층추출 (verbatim·전체 Q&A·segment)
- `prompts/earnings_synthesis.txt` — Opus 비교합성 (8섹션·인용강제·시나리오)
- `prompts/earnings_custom.txt` — 커스텀 질문 답변 (counter-thesis)

### 데이터 소스
- 전문: **Alpha Vantage(ALPHA_VANTAGE_KEY, 무료·1순위)** → FMP_API_KEY(유료) → API_NINJAS_KEY(유료)
  → 웹 스크레이프(httpx+BS4) → perplexity 요약(grounded=False)
  · FMP 무료 키는 transcript 엔드포인트 402/403 (유료 전용). AV 무료가 실측 동작 (25 req/day).
  · AV는 year+quarter 필수 ("YYYYQN"). 미명시 시 FMP/스크레이프/perplexity로 폴백.
- 재무: SEC EDGAR XBRL US-GAAP (FY 10-K 6년 + 단일분기 flow, 검증용)
  · CapEx: PaymentsToAcquirePropertyPlantAndEquipment / OCF: NetCashProvidedByUsedInOperatingActivities
  · FCF = OCF − CapEx, OCF/CapEx 비율 = 캐시 머신 vs 캐펙스 burden

### 파이프라인 (8단계)
0. parse (summary/kimi) → mode/tickers/fiscal_year+quarter/custom
1. (criteria) 조건 → 티커 확장 (research/perplexity)
2+3. 종목별: 진짜 전문 확보 → sonnet 심층추출 → 즉시 텔레그램 발송
4. SEC EDGAR 재무 (FY+분기) — 2단계 전에 먼저 수집
5. 숫자 교차검증 (콜 ↔ SEC), 종목별 flag
6. 비교 합성 (**Opus**, 인용·시나리오 8000자+)
7. 커스텀 질문 답변 (**Opus**, 있는 경우)
8. PDF 빌드 + 발송

### 모델 티어
- parse: `OPENROUTER_MODEL` (kimi)
- 전문 심층추출: `EARNINGS_EXTRACT_MODEL` > `IDEA_NARROW_MODEL` > sonnet
- 비교합성/커스텀: `EARNINGS_SYNTHESIS_MODEL` > `IDEA_SYNTHESIS_MODEL` > **opus**
- 모든 LLM 호출은 `summarizer.chat_with_retry` (재시도+폴백)

### 환경변수
- `EARNINGS_BOT_TOKEN` — 전용 봇 토큰 (@AnselmsSlave11bot). 미설정 시 봇 스킵.
- `EARNINGS_ALLOWED_CHAT_IDS` — 미설정 시 `ALLOWED_CHAT_IDS` 폴백. 전체 공개는 `*`.
- `SEC_EDGAR_USER_AGENT` — "name email@example.com" (SEC 필수)
- `ALPHA_VANTAGE_KEY` — 진짜 전문 (무료·권장). FMP/API Ninjas는 유료 전용.
- `EARNINGS_EXTRACT_MODEL` / `EARNINGS_SYNTHESIS_MODEL` — 모델 override
- `EARNINGS_TEST_PROMPT` — self-test

### 제약
- 미국 상장사 한정. 한국 종목 reject.
- 최대 8개 기업 (4-6 권장). 종목당 1-2분 (전문 확보+심층추출).
- transcript 키 없으면 grounding 실패 → 요약 폴백(환각 위험, ⚠️ 표기). 최소 FMP 키 권장.
- 어닝콜 직후 24-48h는 transcript 소스 미수록 가능.
