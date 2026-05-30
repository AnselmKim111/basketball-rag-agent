"""오늘의 시황 분석 + AI 큐레이션 — 단순 인기/최신을 넘는 똑똑한 리포트 선별.

3단계 파이프라인:
  Stage 0 (collect)  : wisereport 메타데이터 수집 (시황 + 산업 + 글로벌, ~80건, PDF 안 받음)
  Stage 1 (recon)    : 후보 메타로 오늘 시황 컨텍스트 추출 (sonnet) — 주도산업·주도주·학습가치
  Stage 2 (curate)   : 컨텍스트 + 메타 → Top N 선별 + 각 선별 이유 (sonnet)
  Stage 3 (deliver)  : 선별된 N개 PDF 다운/요약/발송

비용: Sonnet 호출 2회 ≈ $0.04 / 실행. 5-15분 (N=10).

격리:
  - wisereport 호출은 PIPELINE_LOCK 안. summarizer.chat_with_retry로 LLM 호출.
  - 모든 외부 호출 try/except — 한 단계 실패가 전체 죽이지 않음.
  - bot_helpers의 send_text_chunked / send_pdf 재사용.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Bot

from src.bot_helpers import (
    MissingWisereportCreds,
    safe_dirname,
    send_pdf,
    send_text_chunked,
    wisereport_creds,
)
from src.pipeline_lock import PIPELINE_LOCK
from src.summarizer import (
    OpenRouterCreditExhausted,
    chat_with_retry,
    get_client,
    summarize_pdf,
)
from src.wisereport import ReportItem, WisereportClient

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 모델 — IdeaBot의 synthesis tier 그대로 (claude-sonnet-4.5 등 진짜 지능 필요)
SYNTHESIS_MODEL_ENV = "IDEA_SYNTHESIS_MODEL"
DEFAULT_SYNTHESIS_MODEL = "anthropic/claude-sonnet-4.5"

DEFAULT_DAYS_BACK = 5  # 후보 풀: 최근 5일 (사용자 의도 — 즉시 거래 가능한 신선한 리서치)
SUMMARY_CHARS = 5000  # 5000자 요약 (long)


# ------------------------------------------------------------------
# 모드 정의 — 시황봇 / 산업봇 / 종목봇 각각 도메인 다름
# ------------------------------------------------------------------
@dataclass
class CuratorMode:
    """각 봇별 큐레이션 도메인 설정.

    sources    : wisereport 카테고리별 fetch 한도 ({카테고리: 한도}).
                 popular + latest 두 번씩 호출 → dedup → 합집합.
    recon_focus: Stage 1 프롬프트의 어디 집중해야 하는지 안내문.
    curate_focus: Stage 2 선별 기준에 추가할 도메인별 강조점.
    label      : 사용자에게 표시할 봇 도메인 라벨 ("시황 종합", "산업", "종목").
    """
    name: str
    label: str
    sources: dict[str, int]
    recon_focus: str
    curate_focus: str


MARKET_MODE = CuratorMode(
    name="market",
    label="시황 종합",
    sources={"strategy": 30, "industry": 30, "global": 20},
    recon_focus=(
        "시황·전략·산업·글로벌을 모두 종합. 오늘 시장의 큰 흐름·주도산업·주도종목, "
        "그리고 새로 공부할 가치 있는 산업·테마까지 균형있게."
    ),
    curate_focus="산업/시황/글로벌 균형 (한 카테고리 60% 초과 금지). 시장 큰 그림 우선.",
)

INDUSTRY_MODE = CuratorMode(
    name="industry",
    label="산업",
    sources={"industry": 60},
    recon_focus=(
        "산업 사이클·테마 트렌드 분석에 집중. 종목 단편 분석보다 산업 전체 사이클·"
        "공급망·정책 방향이 핵심. 새로 공부할 가치 있는 산업도 적극 발굴."
    ),
    curate_focus=(
        "다양한 산업 분포 우선 (한 산업 30% 초과 금지). 단편 분석보다 deep-dive 산업 "
        "리포트, 사이클 진단·정책 임팩트 우선. 공부 가치 강조."
    ),
)

COMPANY_MODE = CuratorMode(
    name="company",
    label="종목",
    sources={"company": 60},
    recon_focus=(
        "오늘의 주도주·테마주를 식별. 어떤 종목들이 시장 주목 받는지, 새로 공부할 "
        "가치 있는 (덜 알려진) 종목도 함께. 산업 흐름은 종목 컨텍스트로만."
    ),
    curate_focus=(
        "다양한 산업·증권사 분포 (한 산업 40% 초과 금지). 시총 큰 종목 자동 우대 "
        "금지 — 깊이 있는 분석·새로 공부할 가치 우선. 단순 매수의견 단편 보고서는 후순위."
    ),
)


CATEGORY_LABEL = {
    "strategy": "시황",
    "industry": "산업",
    "global":   "글로벌",
    "company":  "종목",
}


@dataclass
class CandidateItem:
    """ReportItem + 카테고리 라벨 (LLM에 전달용)."""
    item: ReportItem
    category: str  # "시황" / "산업" / "글로벌" / "종목"

    @property
    def date_short(self) -> str:
        return self.item.date_short

    def meta_line(self, idx: int) -> str:
        """LLM 프롬프트에 넣을 한 줄. idx는 1-based 선택용 번호."""
        title = self.item.title or "(제목 없음)"
        return f"{idx}. [{self.category}] {self.date_short} | {title} (rpt_id={self.item.rpt_id})"


# ------------------------------------------------------------------
# Stage 0: 후보 풀 수집 (wisereport list-only)
# ------------------------------------------------------------------
def _collect_pool_blocking(
    mode: CuratorMode,
    days_back: int,
    industry_gics: str | None = None,
) -> list[CandidateItem]:
    """blocking — wisereport 세션 한 번 열어 mode.sources 카테고리 메타 수집.

    각 카테고리에서 popular + latest 두 번 호출 → dedup → 합침. PDF 다운 안 함.
    industry_gics가 주어지면 list_top_reports에 그대로 전달 — wisereport 서버가
    해당 산업 리포트만 돌려줘 풀이 깨끗해진다 (종목 큐레이션에서 "/curate 소부장"
    같은 산업 한정 호출).
    """
    wr_id, wr_pw = wisereport_creds()
    pool: list[CandidateItem] = []
    seen_ids: set[str] = set()

    with WisereportClient(
        user_id=wr_id,
        password=wr_pw,
        download_root=Path("/tmp"),  # 메타만 fetch — 다운로드 위치 무관
        headless=True,
        ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
        state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
    ) as cli:
        cli.ensure_logged_in()
        for category, base_limit in mode.sources.items():
            for sort_by in ("popular", "latest"):
                try:
                    raw = cli.list_top_reports(
                        category=category,  # type: ignore[arg-type]
                        sort_by=sort_by,    # type: ignore[arg-type]
                        limit=base_limit,
                        days_back=days_back,
                        industry_gics=industry_gics,
                    )
                except Exception:
                    log.exception("list_top_reports 실패 (%s/%s)", category, sort_by)
                    continue
                for it in raw:
                    if it.rpt_id in seen_ids:
                        continue
                    seen_ids.add(it.rpt_id)
                    pool.append(CandidateItem(item=it, category=CATEGORY_LABEL.get(category, category)))
    cat_breakdown = ", ".join(
        f"{label}={sum(1 for c in pool if c.category == label)}"
        for label in CATEGORY_LABEL.values()
        if sum(1 for c in pool if c.category == label)
    )
    # 신선도 우선 — 날짜 desc 정렬 후 LLM에 노출 (메타 블록 상단=최신)
    def _sort_key(c: CandidateItem) -> str:
        return getattr(c.item, "date", "") or getattr(c.item, "date_short", "") or ""
    pool.sort(key=_sort_key, reverse=True)
    log.info(
        "curator pool 수집 완료 [%s]: %d items (%s)%s",
        mode.name, len(pool), cat_breakdown,
        f" [industry_gics={industry_gics}]" if industry_gics else "",
    )
    return pool


async def collect_candidate_pool(
    mode: CuratorMode,
    days_back: int = DEFAULT_DAYS_BACK,
    industry_gics: str | None = None,
) -> list[CandidateItem]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _collect_pool_blocking, mode, days_back, industry_gics,
    )


# ------------------------------------------------------------------
# Stage 1: recon — 오늘의 시황 컨텍스트 (sonnet)
# ------------------------------------------------------------------
RECON_PROMPT = """오늘은 {date} (KST). 한국·글로벌 증권사들이 발행한 최근 {days}일 리포트 {n}건의 메타데이터다.

너는 헤지펀드 PM 입장에서 이 메타를 읽는다. 목표는 **다른 최고 투자자들보다
한 발 빠르게 돈 될 기회를 포착**하는 것. 단순 흐름 정리가 아니라 5축으로 평가해
**정보우위(edge)** 가 있는 영역을 짚어내라.

**도메인 집중 영역**: {recon_focus}

[리포트 메타]
{meta_block}

평가 5축 (이 5축으로 모든 영역·종목을 머릿속에 점수화):
- ⏱️  타이밍 — 방금 발생/발생 임박 (수주, 실적, 정책 발표 D-N) vs 이미 다 가격에 반영
- 🎯 주도섹터 — 시장 흐름 전체를 끌고 가는 영역인가 (예: AI 인프라, 전력, 방산)
- 🆕 새로움 — 이미 모두가 아는 thesis인가, 새로 부상한 thesis인가 (정보우위)
- 💥 크기 — 파급 효과 큰 일 (구조 변화·정책 전환·대형 수주)인가, 자잘한 업데이트인가
- ⚡ 시너지 — 타이밍×주도×새로움×크기가 동시에 충족되는 "킬러 셋업"이 있는가

분석 항목 (마크다운 ## 헤더 + bullet, 600자 이내):
1. **오늘의 킬러 셋업 (1-3개)** — 5축 모두 강한 영역. 각 1-2줄, "왜 지금이 진입 타이밍인지"
2. **주도 영역 (2-4개)** — 시장이 따라가는 흐름. 각 1줄, 어느 종목·테마가 axis인지
3. **새로 부상 (1-2개)** — 1-2주 전엔 없던 thesis. 정보우위 가능 영역
4. **이미 가격에 반영 (선택, 1줄)** — 후보에 보이지만 늦은 영역 (warning)

규칙:
- 메타데이터(제목·발행일·증권사)에 명시된 정보만 사용. 임의 추정 금지.
- 추상 표현 금지 ("성장 기대"·"긍정적"). 구체 사건·종목·날짜로 말할 것.
- 발행 날짜가 오늘({date})에 가까울수록 가중치 강하게.
- 한 증권사가 여러 발행 시 합의 형성 신호 — 명시.
"""


def _build_meta_block(pool: list[CandidateItem]) -> str:
    return "\n".join(c.meta_line(i + 1) for i, c in enumerate(pool))


async def recon_today(
    pool: list[CandidateItem], mode: CuratorMode,
    industry_name: str | None = None,
) -> str:
    """후보 메타로 오늘 시황 컨텍스트 추출. 실패 시 빈 string.

    industry_name이 주어지면 recon_focus 앞단에 산업 한정 지시 prepend — sonnet이
    그 산업 내부 동향에 집중한 시황 분석을 내놓도록.
    """
    if not pool:
        return ""
    loop = asyncio.get_running_loop()
    client = get_client()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    effective_focus = mode.recon_focus
    if industry_name:
        effective_focus = (
            f"**산업 한정**: '{industry_name}' 산업에 속하는 종목·테마·정책만 분석. "
            "다른 산업은 사이드 멘트로도 다루지 말 것. "
            + mode.recon_focus
        )
    prompt = RECON_PROMPT.format(
        date=today,
        days=DEFAULT_DAYS_BACK,
        n=len(pool),
        recon_focus=effective_focus,
        meta_block=_build_meta_block(pool),
    )
    model = os.getenv(SYNTHESIS_MODEL_ENV) or DEFAULT_SYNTHESIS_MODEL
    log.info(
        "curator recon 호출 [%s]: model=%s, n=%d%s",
        mode.name, model, len(pool),
        f", industry={industry_name}" if industry_name else "",
    )
    text = await loop.run_in_executor(
        None,
        lambda: chat_with_retry(
            client,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            model=model,
            temperature=0.3,
            context=f"curator recon ({mode.name})",
        ),
    )
    return (text or "").strip()


# ------------------------------------------------------------------
# Stage 2: curate — Top N 선별 (sonnet, JSON 응답)
# ------------------------------------------------------------------
CURATE_PROMPT = """위 시황 컨텍스트를 기반으로, 사용자에게 보낼 Top {n} 리포트를 선별하라.

너는 헤지펀드 PM. 경쟁 상대는 한국·미국 최고의 액티브 PM·증권사 RA·퀀트.
**그들이 이미 다 보고 가격에 반영시킨 종목은 가치 0**. 너의 알파는 (a) 그들도
보긴 했지만 아직 진입 타이밍이 살아있는 셋업 + (b) 그들이 아직 충분히
weighting 안 한 새 thesis. 이걸 잡는 게 사용자에게 진짜 돈 될 기회를 주는 것.

[시황 컨텍스트]
{recon}

[리포트 메타]
{meta_block}

각 리포트를 5축으로 머릿속에 점수화 (각 0-3점):
  ⏱️  타이밍 — 방금 발생/D-N 임박 catalyst (수주·실적·정책)인가? (3) vs 이미
                 발생해 가격 반영 끝났나? (0)
  🎯 주도섹터 — 위 시황 컨텍스트에서 짚은 주도 영역 핵심에 위치? (3) vs 변두리? (0)
  🆕 새로움 — 1-2주 내 부상한 신규 thesis인가? (3) vs 모두가 아는 컨센서스? (0)
  💥 크기 — 구조 변화·대형 수주·정책 전환급 임팩트? (3) vs 자잘한 업데이트? (0)
  ⚡ 시너지 — 위 4축이 동시 충족되는 킬러 셋업? (3) vs 단일 축만 점수? (0)

선별 알고리즘 (우선순위 순):
1. **신선도 컷오프** — 발행일이 오늘({date_today}) 또는 최근 1-2일이어야 후보.
   3일 이상 지난 리포트는 시장 변화 반영 못 해 컷. 부족 시에만 어제·그제까지 확장.
2. **시너지 점수 가장 높은 것 우선** — 5축 합산 + 시너지 가중. 단일 축만 강한
   리포트보다 4-5축 모두 강한 "킬러 셋업"이 위.
3. **정보우위 우선** — 새로움 점수 높은 영역(아직 모두가 안 본 thesis) 가산점.
4. **클러스터링 활용** — 같은 thesis에 여러 증권사가 짧은 시간 내 발행하면
   합의 형성 신호 → 가산점. 단, 같은 종목 중복 선별은 1-2건까지만.
5. **도메인별 추가 강조**: {curate_focus}
6. **카테고리 다양성 유지** — 시황·산업·종목 어느 하나로 쏠리지 않게.
7. **인기 단독 점수 우대 금지** — 발행 빈도만 높은 종목은 컨센서스 형성 끝났을
   가능성. 새로움·시너지로 cross-check 했을 때 살아남는 것만.

출력 (반드시 valid JSON만):
{{
  "picks": [
    {{
      "idx": <1-based 메타 번호>,
      "reason": "1-2줄. 5축 중 가장 강한 점수 2-3개를 자연어로 강조.
                 예: '⚡ T+0 발행, 🆕 PAFC 데이터센터 신규 부상, 💥 수주 임박 (D-7).
                 시너지 강 — 정보우위 가능.' 추상 표현 금지, 구체 catalyst 명시."
    }},
    ...
  ]
}}

규칙: 정확히 {n}개 선별. idx는 위 메타 번호 그대로. JSON 외 다른 텍스트 출력 금지."""


def _extract_json(text: str) -> dict | None:
    """LLM 응답에서 JSON 블록 추출. ```json fence + 직접 JSON 둘 다 처리."""
    if not text:
        return None
    # ```json ... ``` fence 우선
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        candidate = m.group(1)
    else:
        # 첫 { 부터 마지막 } 까지
        first = text.find("{")
        last = text.rfind("}")
        if first < 0 or last < first:
            return None
        candidate = text[first : last + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        log.warning("curator JSON parse 실패: %s", candidate[:200])
        return None


async def curate_picks(
    pool: list[CandidateItem], recon_text: str, n: int, mode: CuratorMode,
    industry_name: str | None = None,
) -> list[tuple[CandidateItem, str]]:
    """Top N 선별 + 각 이유. 실패 시 빈 리스트 → 호출자가 fallback (인기순).

    industry_name이 주어지면 curate_focus 앞단에 산업 한정 강제 — 풀은 이미 GICS
    필터로 깨끗하지만, 일부 종합 리포트가 다른 산업 종목을 함께 다룰 수 있어
    이중 안전장치.
    """
    if not pool:
        return []
    n = max(1, min(n, len(pool)))
    loop = asyncio.get_running_loop()
    client = get_client()
    effective_focus = mode.curate_focus
    if industry_name:
        effective_focus = (
            f"**필수 — 산업 한정**: 반드시 '{industry_name}' 산업에 속하는 종목 리포트만 "
            "선별. 풀에 다른 산업이 섞여 있어도 무시. "
            "전혀 매칭 안 되면 picks 빈 리스트 반환. "
            + mode.curate_focus
        )
    prompt = CURATE_PROMPT.format(
        n=n,
        recon=recon_text or "(시황 컨텍스트 없음)",
        meta_block=_build_meta_block(pool),
        curate_focus=effective_focus,
        date_today=datetime.now(KST).strftime("%Y-%m-%d"),
    )
    model = os.getenv(SYNTHESIS_MODEL_ENV) or DEFAULT_SYNTHESIS_MODEL
    log.info("curator curate 호출 [%s]: model=%s, n=%d/%d", mode.name, model, n, len(pool))
    raw = await loop.run_in_executor(
        None,
        lambda: chat_with_retry(
            client,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            model=model,
            temperature=0.2,
            context="curator curate",
        ),
    )
    parsed = _extract_json(raw or "")
    if not parsed or "picks" not in parsed:
        log.warning("curator JSON 추출 실패 — 인기 N건 폴백")
        return [(c, "(LLM 큐레이션 실패 — 인기순 폴백)") for c in pool[:n]]
    picks = parsed["picks"]
    out: list[tuple[CandidateItem, str]] = []
    for p in picks:
        try:
            idx = int(p.get("idx", 0))
            reason = str(p.get("reason", "")).strip()
            if 1 <= idx <= len(pool):
                out.append((pool[idx - 1], reason or "(이유 미제공)"))
        except (ValueError, TypeError):
            continue
    if not out:
        return [(c, "(LLM 응답 비정상 — 인기순 폴백)") for c in pool[:n]]
    log.info("curator picks: %d (요청 %d)", len(out), n)
    return out[:n]  # 안전 cap


# ------------------------------------------------------------------
# Stage 3: deliver — 선별된 리포트 다운로드 + 요약 + 발송
# ------------------------------------------------------------------
def _download_and_summarize_blocking(
    picks: list[tuple[CandidateItem, str]],
    download_root: Path,
) -> list[tuple[CandidateItem, str, Path | None, str]]:
    """blocking — 선별 항목만 PDF 다운 + 요약. 빈 결과 가능."""
    wr_id, wr_pw = wisereport_creds()
    target_dir = download_root / safe_dirname("curated")
    target_dir.mkdir(parents=True, exist_ok=True)

    out: list[tuple[CandidateItem, str, Path | None, str]] = []
    sum_client = get_client()

    with WisereportClient(
        user_id=wr_id,
        password=wr_pw,
        download_root=download_root,
        headless=True,
        ignore_https_errors=os.environ.get("IGNORE_HTTPS_ERRORS", "false").lower() == "true",
        state_file=Path(os.environ.get("STORAGE_STATE", "./.wisereport_state.json")),
    ) as cli:
        cli.ensure_logged_in()
        items = [c.item for c, _ in picks]
        try:
            saved = cli.download_reports(items, target_dir)
        except Exception:
            log.exception("curator 다운로드 실패")
            saved = []

    # 요약
    saved_by_id = {p.stem.split("_")[0] if "_" in p.stem else p.stem: p for p in saved}
    # rpt_id로 매핑이 안전하지 않을 수 있어서 — items 순서 보장된 saved 가정
    for (c, reason), p in zip(picks, saved + [None] * (len(picks) - len(saved))):
        if not p:
            out.append((c, reason, None, "(다운로드 실패)"))
            continue
        try:
            s = summarize_pdf(sum_client, p)
            out.append((c, reason, p, s.summary_text))
        except OpenRouterCreditExhausted:
            out.append((c, reason, p, "(요약 실패: OpenRouter 토큰 부족)"))
        except Exception as e:
            out.append((c, reason, p, f"(요약 실패: {e!r})"))
    return out


async def run_curated(
    bot: Bot, chat_id: str, n: int = 10,
    mode: CuratorMode = MARKET_MODE,
    industry_name: str | None = None,
    industry_gics: str | None = None,
) -> None:
    """전체 큐레이션 파이프라인 + 발송.

    PIPELINE_LOCK 안에서 wisereport 세션 사용. LLM 호출은 lock 밖에서 가능하지만
    단순화 위해 모든 단계를 lock 안에서 진행.

    industry_name + industry_gics 가 주어지면 wisereport 풀 수집 자체를 해당 산업
    GICS 로 제한 → 풀이 깨끗해져 sonnet 선별 결과도 그 산업 종목만 나온다.
    """
    if PIPELINE_LOCK.locked():
        await send_text_chunked(
            bot, chat_id,
            "⏳ 다른 작업 진행 중 — 끝나면 큐레이션 시작합니다",
        )

    try:
        wisereport_creds()  # 자격증명 사전 체크 — lock 안에서 죽으면 lock 시간 낭비
    except MissingWisereportCreds:
        await send_text_chunked(bot, chat_id, "⚠️ wisereport 자격 증명 미설정 — 관리자에게 문의")
        return

    started = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    src_label = " · ".join(mode.sources.keys())
    scope_label = f"{mode.label} · *{industry_name}* 산업 한정" if industry_name else mode.label
    await send_text_chunked(
        bot, chat_id,
        f"🔍 *오늘의 {scope_label} 큐레이션 시작* ({started})\n"
        f"3단계 분석 진행 — Top {n} 선별까지 5-15분 소요\n"
        f"  1️⃣ 후보 풀 수집 ({src_label})"
        + (f" — GICS={industry_gics}" if industry_gics else "") + "\n"
        f"  2️⃣ AI 분석 → 주도 영역\n"
        f"  3️⃣ AI 큐레이션 → Top {n} + 선별 이유",
        parse_mode="Markdown",
    )

    async with PIPELINE_LOCK:
        # Stage 0
        try:
            pool = await collect_candidate_pool(mode, industry_gics=industry_gics)
        except Exception:
            log.exception("curator Stage 0 실패 [%s]", mode.name)
            await send_text_chunked(bot, chat_id, "❌ wisereport 후보 수집 실패")
            return
        if not pool:
            msg = (
                f"❌ '{industry_name}' 산업 후보 풀이 비어있습니다 — "
                "지난 7일 내 발행 리포트가 없습니다. 더 큰 산업명으로 재시도해 보세요."
            ) if industry_name else "❌ 후보 풀 비어있음 — 다시 시도하세요"
            await send_text_chunked(bot, chat_id, msg)
            return
        ind_tag = f" ({industry_name})" if industry_name else ""
        await send_text_chunked(
            bot, chat_id,
            f"✅ 후보 풀 {len(pool)}건 수집 완료{ind_tag} — {mode.label} 분석 중...",
        )

        # Stage 1
        try:
            recon_text = await recon_today(pool, mode, industry_name=industry_name)
        except Exception:
            log.exception("curator Stage 1 실패 [%s]", mode.name)
            recon_text = ""
        if recon_text:
            await send_text_chunked(
                bot, chat_id,
                f"📊 *오늘의 {mode.label} 분석*\n\n{recon_text}",
                parse_mode="Markdown",
            )
        else:
            await send_text_chunked(
                bot, chat_id,
                "⚠️ 분석 실패 — 큐레이션은 계속 진행 (인기순 폴백 가능)",
            )

        # Stage 2
        try:
            picks = await curate_picks(
                pool, recon_text, n, mode, industry_name=industry_name,
            )
        except Exception:
            log.exception("curator Stage 2 실패 [%s]", mode.name)
            picks = [(c, "(큐레이션 실패 폴백)") for c in pool[:n]]
        if not picks:
            if industry_name:
                await send_text_chunked(
                    bot, chat_id,
                    f"❌ '{industry_name}' 산업 후보 0건 — sonnet이 매칭 종목을 못 찾았습니다.\n"
                    "더 큰 산업명으로 재시도하거나 며칠 후 다시 시도해 주세요.",
                )
            else:
                await send_text_chunked(bot, chat_id, "❌ 큐레이션 실패 — 후보 0")
            return

        # 큐레이션 헤더
        header_lines = [f"🎯 *Top {len(picks)} 큐레이션*\n"]
        for i, (c, reason) in enumerate(picks, start=1):
            header_lines.append(
                f"*{i}. [{c.category}] {c.item.title}*\n"
                f"  📅 {c.date_short}  💡 {reason}"
            )
        await send_text_chunked(
            bot, chat_id,
            "\n\n".join(header_lines),
            parse_mode="Markdown",
        )

        # Stage 3 — 다운로드 + 요약 + PDF
        download_root = Path(os.environ.get("DOWNLOAD_DIR", "./downloads"))
        await send_text_chunked(
            bot, chat_id,
            f"📥 선별 {len(picks)}건 PDF 다운로드 + 요약 시작 — 각 1분 소요",
        )

        loop = asyncio.get_running_loop()
        try:
            results = await loop.run_in_executor(
                None, _download_and_summarize_blocking, picks, download_root,
            )
        except Exception:
            log.exception("curator Stage 3 실패")
            await send_text_chunked(bot, chat_id, "❌ 다운로드/요약 단계 실패 — 로그 확인")
            return

        # 발송 — 각 항목 요약 + PDF
        for i, (c, reason, path, summary) in enumerate(results, start=1):
            header = (
                f"🔖 *{i}/{len(results)}* [{c.category}] {c.item.title}\n"
                f"📅 {c.date_short}  💡 {reason}"
            )
            await send_text_chunked(bot, chat_id, header, parse_mode="Markdown")
            await send_text_chunked(bot, chat_id, summary)
            if path:
                await send_pdf(bot, chat_id, path, caption=c.item.title)

        finished = datetime.now(KST).strftime("%H:%M")
        await send_text_chunked(
            bot, chat_id,
            f"✅ 큐레이션 완료 ({finished}) — Top {len(results)}건 발송",
        )
