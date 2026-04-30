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

| 티어 | env var | 모델 | 용도 |
|---|---|---|---|
| Summary | `OPENROUTER_MODEL` | kimi-k2.6 (갓성비) | PDF 요약·DART·Forward·deepdive·idea parse |
| Research | `IDEA_RESEARCH_MODEL` | perplexity/sonar-pro | 1단계 웹검색 |
| Narrow | `IDEA_NARROW_MODEL` | claude-haiku-4.5 | 3단계 30→10 (큰 출력) + parse 폴백 |
| Synthesis | `IDEA_SYNTHESIS_MODEL` | claude-sonnet-4.5 | 1.5 importance + 5 synthesis (진짜 지능) |

요약·추출 작업은 kimi에서 절대 sonnet으로 올리지 말 것 (비용 6-8배). 진짜 지능 필요한 단계만 sonnet.

## 3. 배포 흐름

- **개발 브랜치**: `claude/idea-bot-stock-analysis-i6HuR`
- **Railway 추적 브랜치**: `claude/wisereport-auto-downloader-C7C8l` (다른 세션과 공유)
- **푸시 패턴**: 두 브랜치 모두에 푸시
  ```
  git push origin claude/idea-bot-stock-analysis-i6HuR:claude/wisereport-auto-downloader-C7C8l
  git push origin claude/idea-bot-stock-analysis-i6HuR
  ```
- **다른 세션 충돌 시**: `git fetch + git merge --no-edit` 후 재푸시

## 4. 자동 검증 도구

- **Self-test**: `IDEA_TEST_PROMPT` env 설정 → 부팅 후 `_self_test()` 자동 실행 (`src/idea_bot.py:build_idea_app`).
- **로그 fetch**: Railway GraphQL `deploymentLogs(deploymentId, limit, startDate)` — Project-Access-Token 헤더 사용.
- **테스트 종료 신호**: 로그에 `[send_results 완료]` / `최종 분석 실패` / `OpenRouter 한도 초과` 중 하나.
- **모니터 도구**: `Monitor` tool로 30초 간격 폴링 + 로그 신규 분석.

## 5. 토큰·자격증명

토큰들은 git에 커밋 안 됨 — `CLAUDE.local.md` 참조 (gitignored).
