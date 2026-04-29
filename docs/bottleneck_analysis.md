# Idea Bot 병목 진단 보고서

> **목적**: 현재 운영 중인 basketball-rag-agent (a.k.a. wisereport-auto-downloader) 파이프라인의 비용·시간·에러 병목을 정량 측정한다. 본 보고서는 **진단만** 포함하며, 개선 제안·우선순위·예상 절감액은 의도적으로 제외한다.

---

## 1. 개요

### 1.1 데이터 출처

| 출처 | 범위 | 규모 |
|---|---|---|
| OpenRouter Activity CSV (`98ffe94e-openrouter_activity_20260429.csv`) | 2026-04-27 00:05 → 2026-04-29 09:18 UTC (2.5일) | 146 호출 |
| Railway 배포 로그 (deployment `86cf06ab`, project `dynamic-embrace`, service `basketball-rag-agent`) | 2026-04-29 09:09:48 → 09:23:00 UTC | 442 라인 (실제 INFO 120 / WARNING 2 / ERROR 11) |
| 코드베이스 | branch `claude/analyze-api-bottlenecks-zTs6W` | `src/`, `prompts/` |

### 1.2 분석 한계

- Railway 로그 캡처 윈도(13분 11초)는 self-test 1회 실행만 포함 — 다른 idea/deepdive/category 실행은 코드 매핑 추정으로 보완.
- OpenRouter `app_name`은 `wisereport-auto-downloader` 단일 — 봇별 트래픽 분리는 불가.
- CSV에 호출 prompt 본문이 없어 `tokens_prompt` 크기로만 단계 추정.

---

## 2. 모델별 비용·시간 분포 (2.5일 누적)

| 모델 | 호출수 | 총 비용 | 비중 | 평균/호출 | prompt tok | completion tok | 평균 시간 | length_hit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `anthropic/claude-4.5-sonnet` | 46 | **$6.628** | **67.5%** | $0.144 | 1,523,740 | 137,150 | 49.9s | 3 |
| `moonshotai/kimi-k2.6` | 67 | $1.310 | 13.3% | $0.020 | 1,451,028 | 105,323 | **52.4s** | **59 (88%)** |
| `perplexity/sonar-pro` | 15 | $1.000 | 10.2% | $0.067 | 28,528 | 54,976 | 25.7s | 1 |
| `anthropic/claude-4.5-haiku` | 18 | $0.879 | 9.0% | $0.049 | 529,440 | 69,975 | 23.2s | 2 |
| **합계** | **146** | **$9.818** | 100% | | 3,532,736 | 367,424 | | 65 |

핵심 관찰:

- **Sonnet이 비용의 67.5% 단일 차지**. 호출수는 31%에 불과하지만, prompt 크기가 큼.
- **Kimi의 length_hit 88%** (59/67) — 출력이 max_tokens 한도에서 잘림.
- Sonnet의 누적 wall-clock(2,293초)과 Kimi의 누적 wall-clock(3,511초)을 합치면 **97분** — 2.5일 중 LLM 호출만의 누적 시간.

---

## 3. 단계별 비용 분해

### 3.1 OpenRouter 호출 ↔ 코드 단계 ↔ 모델 매핑

| 파이프라인 단계 | 함수 (file:line) | 모델 | max_tokens | OpenRouter 로그 식별 단서 |
|---|---|---|---:|---|
| **0.5 parse** | `src/idea_bot.py:346-357` `_parse_idea` | kimi → haiku 폴백 | 1000 | kimi 1.3K~1.9K prompt, length_hit |
| **1 research** | `src/idea_bot.py:451-459` `_research_idea` | perplexity sonar-pro | 6000 | 1.4K~2.7K prompt, completion 2.3K~5K |
| **1.5 importance** | `src/idea_bot.py:539-547` `_evaluate_importance` | sonnet | 2000 | 4.5K~16K prompt, completion ≤2K |
| **3 narrow** | `src/idea_bot.py:743-751` `_narrow_candidates` | haiku (default) / sonnet | **12000** | haiku 50K~58K prompt, completion 7K~8K |
| **5 synthesis** | `src/idea_bot.py:1006-1014` `_synthesize_top5` | sonnet | **16000** | sonnet 80K~96K prompt, completion 8K~16K |
| 재시도 인프라 | `src/summarizer.py:211-297` `chat_with_retry` | — | — | 동일 prompt 2회 + fallback 1회 패턴 |

### 3.2 큰 호출(병목 후보) 분포

**Synthesis 단계 (sonnet, prompt ≥ 80K)**
- **9건 / $4.157** — 전체 비용의 **42.3%**
- 평균 175초, 최대 216초
- 호출당 prompt 92K~96K, completion 8K~16K, 비용 $0.40~$0.52

**Narrow 단계 (haiku, prompt ≥ 50K)**
- 9건 / $0.854
- 평균 44초

→ **단 18건의 호출(전체 12%)이 비용의 51%를 차지**. 두 단계 모두 idea bot의 후반 단계로, 산업·종목 리포트 텍스트가 prompt에 합쳐지면서 컨텍스트가 폭증.

### 3.3 컨텍스트 크기 추정 (코드 기반)

| 단계 | prompt 동적 컴포넌트 | 추정 최대 크기 |
|---|---|---|
| narrow | research JSON + 산업 리포트 텍스트 | ~75KB |
| synthesis | research + importance + top10 JSON + 산업 50KB + 종목 60KB | **~130KB** |

`idea_bot.py:734-735, 987-994`에서 컨텍스트 합성 위치 확인.

---

## 4. 시간 병목 (Railway self-test timeline)

### 4.1 단일 idea pipeline 실행 — 4/29 09:09:49 → 09:23:00 UTC, **총 13분 11초**

```
09:09:49  self-test 시작 (prompt: TSMC 'SoW' 웨이퍼)
─────────── parse ───────────
09:11:20  [WARN] LLM empty content [idea_parse] attempt 1/3 (kimi)   ← 91초 낭비
09:12:16  [WARN] LLM empty content [idea_parse] attempt 2/3 (kimi)   ← 56초 낭비
09:12:26  [INFO] LLM recovered [idea_parse] attempt 3 (haiku, 427자) ← 폴백 성공
─────────── research ───────────
09:12:56  research 완료 (perplexity, 10836자)                        ← 30초
─────────── importance ───────────
09:13:26  importance 완료 (sonnet, 1256자)                            ← 30초
─────────── narrow ───────────
09:15:46  narrow 완료 (haiku, 13178자)                                ← 140초
─────────── wisereport (병렬 X, 직렬) ───────────
09:16:43  [ERR] wisereport 다운로드 실패 rpt_id=1087200
09:17:24  [ERR] wisereport 다운로드 실패 rpt_id=1092382
09:18:00  [ERR] wisereport 다운로드 실패 rpt_id=1092373
09:18:51  [ERR] wisereport 다운로드 실패 rpt_id=1092378
─────────── synthesis ───────────
09:21:50  synthesis 완료 (sonnet, 14079자)                            ← (wisereport+synthesis 합 360초)
─────────── send_results ───────────
09:22:03  [ERR] send_document 실패: 01_2026-01-27_HBM PDF
09:22:36  [ERR] send_document 실패: 01_2026-04-29_견조한 실적 PDF
09:23:00  send_results 완료 (top5 + PDF 14건)
```

### 4.2 단계별 wall-clock 시간 (이번 1회 실행 기준)

| 단계 | 시작 → 종료 | 소요 | 비고 |
|---|---|---:|---|
| parse | 09:09:49 → 09:12:26 | **2분 37초** | kimi 빈응답 2회로 145초 낭비 (실제 작업은 10초) |
| research | 09:12:26 → 09:12:56 | 30초 | |
| importance | 09:12:56 → 09:13:26 | 30초 | |
| narrow | 09:13:26 → 09:15:46 | 2분 20초 | haiku 50K+ prompt |
| wisereport + synthesis | 09:15:46 → 09:21:50 | **6분 4초** | 다운로드 실패 4건 포함 |
| send_results | 09:21:50 → 09:23:00 | 1분 10초 | PDF 14건 첨부, 2건 실패 |

→ 가장 큰 wall-clock 소비는 **wisereport+synthesis 묶음(46%)**, 다음이 **parse 단계의 빈응답 낭비(20%)**.

### 4.3 직렬화 구조

- `src/idea_bot.py:150` `_run_pipeline` — 사용자당 1개 task로 모든 단계 직렬 실행.
- `src/pipeline_lock.py` `PIPELINE_LOCK` — wisereport 산업/종목 다운로드 2회를 직렬화 (중복 로그인 방지).
- 명시적 병렬화(`asyncio.gather`) 호출 없음 — 모든 단계가 앞 단계 완료 대기.

---

## 5. 에러·실패 카탈로그

### 5.1 Kimi length_hit (88%) — provider별 분포

`moonshotai/kimi-k2.6`은 OpenRouter가 12개 provider로 자동 라우팅. 같은 prompt가 빈응답·length_hit 시 다른 provider로 재시도되는 패턴이 CSV에서 명확.

| Provider | 호출수 | length_hit | 평균 시간 |
|---|---:|---:|---:|
| Parasail | 11 | 100% | 68.5s |
| Inceptron | 9 | 100% | 46.7s |
| AkashML | 8 | 100% | 35.3s |
| Venice | 8 | 100% | 34.4s |
| Together | 6 | 100% | 37.1s |
| Io Net | 6 | 100% | 22.6s |
| **AtlasCloud** | 5 | **0%** (정상 stop) | 75.7s |
| DeepInfra | 5 | 100% | **109.9s** |
| **SiliconFlow** | 3 | **0%** (정상 stop) | 100.5s |
| Moonshot AI | 2 | 100% | 36.1s |
| Phala | 2 | 100% | 21.9s |
| Novita | 2 | 100% | 38.1s |

→ AtlasCloud·SiliconFlow를 제외한 **모든 provider가 max_tokens=1200 한도에서 잘림**. 정상 stop 8건 중 7건이 이 둘에서 발생.

### 5.2 Parse 단계 kimi 빈응답 → haiku 폴백

- Railway log 9:11:20, 9:12:16에서 동일 prompt에 kimi 2회 빈응답 발생.
- `src/summarizer.py:243` 3차 시도에서 `OPENROUTER_FALLBACK_MODEL` (haiku) 사용 — 정상 복구.
- 재현 비용: 2회 × kimi 호출 + 1회 haiku 호출 = 약 145초 + $0.005×3 ≈ $0.015 낭비.

### 5.3 Wisereport 다운로드 실패 (4건)

| 시각(UTC) | rpt_id |
|---|---|
| 09:16:43 | 1087200 |
| 09:17:24 | 1092382 |
| 09:18:00 | 1092373 |
| 09:18:51 | 1092378 |

`src/wisereport.py` Playwright 기반 다운로드 — 실패 후 파이프라인은 계속 진행(예외 집어삼킴 패턴). 실패 원인은 로그에 기록되지 않음(traceback 없이 단순 "다운로드 실패").

### 5.4 Telegram send_document 실패 (2건)

| 시각(UTC) | 파일명 |
|---|---|
| 09:22:03 | `01_2026-01-27_커지는 HBM, 여전한 존재감.pdf` |
| 09:22:36 | `01_2026-04-29_견조한 실적으로 증명 중.pdf` |

`src/bot_helpers.py:send_document` 호출 위치. Telegram API 타임아웃 또는 파일 크기(`telegram_sender.py:32-96` 49MB 필터) 초과 가능성. 실패 후에도 send_results 완료 메시지는 정상 발송됨.

### 5.5 Telegram polling exception (시작 직후 5건)

```
09:09:54  [ERROR] telegram.ext.Updater: Exception happened while polling for updates.
09:09:54  [ERROR] telegram.ext.Updater: Exception happened while polling for updates.
09:09:55  [ERROR] telegram.ext.Updater: Exception happened while polling for updates.
09:09:56  [ERROR] telegram.ext.Updater: Exception happened while polling for updates.
09:09:56  [ERROR] telegram.ext.Updater: Exception happened while polling for updates.
```

`src/orchestrator.py:69-121`은 5개 봇(company/industry/market/global/idea)을 동시 폴링. 5번 발생 = 봇 수와 일치 → 다른 인스턴스(이전 deployment)의 polling과 conflict 의심. 자동 복구된 듯 — 이후 정상 동작.

### 5.6 모델별 length finish 합계

- kimi: 59건 (provider 자동 라우팅으로 부분 흡수)
- sonnet: 3건 (max_tokens=12000/16000에서도 잘림)
- haiku: 2건 (max_tokens=8000)
- perplexity: 1건

---

## 6. 시간 클러스터 분석 (gap > 10분 기준)

CSV 146 호출을 시간 gap 기준으로 클러스터링 — 큰 클러스터는 idea pipeline 1회 풀 실행으로 추정.

| # | 시각(UTC) | 길이 | 호출수 | 비용 | sonnet/haiku/kimi/perp | length_hit | 비고 |
|---:|---|---:|---:|---:|---|---:|---|
| 1 | 4/27 00:05~00:07 | 2.0분 | 4 | $0.272 | 1/0/3/0 | 3 | |
| 2 | 4/27 09:46~09:57 | 10.9분 | 5 | $0.528 | 2/0/2/1 | 3 | |
| 3 | 4/27 10:10~10:11 | 0.9분 | 3 | $0.080 | 1/0/1/1 | 1 | |
| 4 | 4/27 10:23~10:50 | 26.6분 | 14 | $1.351 | 5/3/3/3 | 3 | idea 1회 + 일부 |
| 5 | 4/27 11:23~11:30 | 7.2분 | 5 | $0.640 | 2/2/0/1 | 0 | |
| 6 | 4/27 11:47~12:10 | 22.4분 | 13 | $1.456 | 5/5/0/3 | 0 | idea 1회 (kimi 0) |
| 7 | 4/28 00:01~00:12 | 11.8분 | 26 | $0.681 | 8/0/18/0 | 16 | kimi 다수 |
| **8** | **4/29 00:02~00:34** | **32.4분** | **46** | **$2.425** | **14/0/32/29** | **29** | **이상치 — 6.4 참고** |
| 9 | 4/29 08:22~08:37 | 15.3분 | 13 | $1.035 | 3/4/3/3 | 5 | idea 1회 |
| 10 | 4/29 08:47~09:18 | 31.1분 | 17 | $1.522 | 5/4/5/3 | 5 | idea 1회 (self-test와 다른 실행) |

### 6.1 Cluster 8 이상치 (4/29 00:02~00:34)

- 32분 동안 46회 호출, 비용 $2.425 — 보고서 윈도(2.5일) 비용의 24.7%가 이 30분에 집중.
- kimi 32회 중 29회 length_hit. 같은 prompt가 다른 provider로 2회씩 재호출되는 명확한 패턴.
- sonnet 14회 (synthesis 1회당 90~96K prompt 큰 호출이 다수 포함).
- 동일 시간대 Railway 로그 미보유로 어떤 작업인지 단정 불가. 코드 매핑상 후보:
  - `src/category_bots.py:134-144` `_process_and_send_category` (스케줄 작업 — 그러나 이 클러스터 시각은 KST 09:00 스케줄과 불일치, UTC 0시는 KST 9시이므로 일부 일치 가능)
  - `src/deepdive/handler.py:427-435` `_summarize_and_send` (사용자 명령 기반)
  - `src/idea_bot.py` 다중 idea 실행

### 6.2 Cluster 6 vs Cluster 8 대조

| 항목 | Cluster 6 (4/27 11:47) | Cluster 8 (4/29 00:02) |
|---|---|---|
| 호출수 | 13 | 46 |
| 비용 | $1.456 | $2.425 |
| kimi 호출 | 0 | 32 |
| length_hit | 0 | 29 |
| sonnet | 5 | 14 |

→ Cluster 6은 idea bot 정상 1회 실행으로 보이며 kimi 호출이 없음 (parse 단계가 kimi → 빈응답 → haiku로 즉시 폴백되었거나 OPENROUTER_MODEL 환경변수가 다른 시점). Cluster 8은 같은 sonnet 패턴에 kimi 32회가 추가 — **반복 실행 또는 다른 봇의 동시 작업**으로 추정.

---

## 7. 부록 — Raw 측정값

### 7.1 비용 상위 10건

| # | 시각(UTC) | 모델 | prompt | completion | 비용 | 시간 | finish |
|---:|---|---|---:|---:|---:|---:|---|
| 1 | 4/29 08:37:34 | sonnet | 94,866 | 16,000 | $0.5246 | 205s | length |
| 2 | 4/27 11:54:30 | sonnet | 96,920 | 14,396 | $0.5067 | 216s | stop |
| 3 | 4/27 11:30:31 | sonnet | 95,869 | 12,240 | $0.4712 | 183s | stop |
| 4 | 4/27 12:10:07 | sonnet | 96,178 | 11,804 | $0.4656 | 167s | stop |
| 5 | 4/27 10:50:07 | sonnet | 96,503 | 11,540 | $0.4626 | 173s | stop |
| 6 | 4/29 09:18:56 | sonnet | 96,091 | 10,721 | $0.4491 | 165s | stop |
| 7 | 4/29 08:55:40 | sonnet | 92,931 | 9,428 | $0.4202 | 145s | stop |
| 8 | 4/27 09:57:20 | sonnet | 92,684 | 8,000 | $0.3981 | 131s | length |
| 9 | 4/27 10:35:21 | sonnet | 92,871 | 12,000 | $0.4586 | 193s | length |
| 10 | 4/29 00:09:49 | sonnet | 56,855 | 825 | $0.1829 | 22s | stop |

→ 상위 9건이 모두 synthesis 단계로 추정되며 (`prompt ≥ 92K`), 합계 $4.16 = 보고서 윈도 비용의 **42.4%**.

### 7.2 시간 상위 10건

| # | 시각(UTC) | 모델 | provider | prompt | completion | 시간 | finish |
|---:|---|---|---|---:|---:|---:|---|
| 1 | 4/27 11:54:30 | sonnet | Bedrock | 96,920 | 14,396 | **216s** | stop |
| 2 | 4/29 08:37:34 | sonnet | Bedrock | 94,866 | 16,000 | 205s | length |
| 3 | 4/27 10:35:21 | sonnet | Bedrock | 92,871 | 12,000 | 193s | length |
| 4 | 4/27 11:30:31 | sonnet | Bedrock | 95,869 | 12,240 | 183s | stop |
| 5 | 4/27 10:29:30 | kimi | DeepInfra | 44,983 | 8,000 | 198s | length |
| 6 | 4/27 10:26:14 | kimi | DeepInfra | 44,983 | 8,000 | 195s | length |
| 7 | 4/27 10:50:07 | sonnet | Bedrock | 96,503 | 11,540 | 173s | stop |
| 8 | 4/27 12:10:07 | sonnet | Bedrock | 96,178 | 11,804 | 167s | stop |
| 9 | 4/29 09:18:56 | sonnet | Bedrock | 96,091 | 10,721 | 165s | stop |
| 10 | 4/29 00:11:30 | kimi | SiliconFlow | 43,043 | 3,101 | 142s | stop |

→ 100초 이상 호출이 16건. Sonnet 큰 prompt와 kimi DeepInfra/SiliconFlow가 주요 시간 소비처.

### 7.3 Cache 사용 (Moonshot/DeepInfra/Parasail/Inceptron prompt caching)

CSV `cost_cache` 음수 = 캐시 환급. 11건에서 발생:

| 시각 | provider | tokens_cached | cache 환급 |
|---|---|---:|---:|
| 4/27 10:29:30 | DeepInfra | 44,960 | -$0.0270 |
| 4/29 00:30:11 | Moonshot AI | 41,116 | -$0.0325 |
| 4/29 00:08:41 | DeepInfra | 47,936 | -$0.0288 |
| 4/29 00:05:54 | Parasail | 46,208 | -$0.0277 |
| 4/27 10:29:29 | Moonshot AI | 256 | -$0.0002 |
| (외 6건) | | | |

→ 큰 prompt 캐싱이 일부 작동 중. Anthropic(sonnet/haiku) 호출에는 cache 환급 0건.

### 7.4 모델 finish_reason 분포

| 모델 | stop | length | 합계 | length 비율 |
|---|---:|---:|---:|---:|
| sonnet | 43 | 3 | 46 | 6.5% |
| haiku | 16 | 2 | 18 | 11.1% |
| perplexity | 14 | 1 | 15 | 6.7% |
| kimi | 8 | 59 | 67 | **88.1%** |

---

## 8. 측정 요약

- **총 비용 (2.5일)**: $9.82
- **호출 1회당 평균**: $0.067
- **Idea pipeline 1회 wall-clock**: 약 13~32분 (Railway 1회 측정 + CSV 클러스터)
- **단일 호출 최대 비용**: $0.5246 (sonnet, 94K prompt + 16K completion, 205s)
- **단일 호출 최대 시간**: 216초 (sonnet, 96K prompt)
- **에러 발생 카테고리**: kimi length_hit / parse 빈응답 / wisereport 다운로드 / Telegram send_document / Telegram polling

---

*보고서 생성: 2026-04-29 / 데이터 윈도: 2026-04-27 ~ 2026-04-29 UTC*
