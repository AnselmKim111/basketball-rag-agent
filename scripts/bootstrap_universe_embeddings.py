#!/usr/bin/env python3
"""Universe-wide business_text + embedding 1회 bootstrap (또는 분기별 refresh).

흐름:
  1) screener.universe.refresh_universe() — ticker · 시총 · 섹터 최신화
  2) DART bulk fetch — 2000종목 사업의 내용 추출 → screener.db.business_text
  3) text-embedding-3-small batch embed → BLOB 저장
  4) 통계 + 비용 보고

사용:
  # Railway에서 1회 수동 실행
  python scripts/bootstrap_universe_embeddings.py

  # 특정 ticker만 (테스트용)
  python scripts/bootstrap_universe_embeddings.py --limit 50

  # 90일 내 갱신 무시하고 강제 재추출
  python scripts/bootstrap_universe_embeddings.py --force-refetch

환경변수:
  DART_API_KEY            (필수) — DART OpenDART
  OPENAI_API_KEY          (권장) — text-embedding-3-small
  OPENROUTER_API_KEY      (폴백 — 현재 embeddings 미지원 가능)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# project root을 path에 추가
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bootstrap")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="처음 N종목만 (0=전체)")
    p.add_argument("--skip-universe-refresh", action="store_true",
                   help="ticker·시총·섹터 refresh 건너뛰기 (이미 최신이라면)")
    p.add_argument("--force-refetch", action="store_true",
                   help="90일 내 추출됐어도 DART에서 재추출")
    p.add_argument("--skip-embedding", action="store_true",
                   help="business_text만 fetch, embedding은 스킵")
    p.add_argument("--concurrency", type=int, default=4,
                   help="DART 동시 호출 수 (default 4)")
    p.add_argument("--embed-batch-size", type=int, default=100,
                   help="OpenAI embeddings batch size (default 100)")
    return p.parse_args()


async def step_refresh_universe() -> int:
    from src.screener import universe
    log.info("=" * 60)
    log.info("Step 1: screener universe refresh (ticker·시총·섹터)")
    log.info("=" * 60)
    t0 = time.time()
    try:
        universe.refresh_universe()
        from src.screener import db as screener_db
        active = len(screener_db.get_active_tickers())
        log.info("Step 1 완료: active %d종목 (%.1fs)", active, time.time() - t0)
        return active
    except Exception:
        log.exception("Step 1 실패 — universe refresh 예외")
        return 0


async def step_bulk_business_text(
    limit: int,
    force_refetch: bool,
    concurrency: int,
) -> dict:
    from src.idea import universe_text
    log.info("=" * 60)
    log.info("Step 2: DART 사업의 내용 bulk fetch")
    log.info("=" * 60)

    targets = None
    if limit > 0:
        from src.screener import db as screener_db
        rows = screener_db.get_active_tickers()
        # 시총 큰 순으로 limit (테스트용)
        rows.sort(key=lambda r: (r.get("market_cap") or 0), reverse=True)
        targets = [r["ticker"] for r in rows[:limit]]
        log.info("limit=%d — 시총 상위 %d종목 대상", limit, len(targets))

    skip_days = 0 if force_refetch else 90
    t0 = time.time()
    stats = await universe_text.bulk_refresh_business_text(
        tickers=targets,
        max_concurrency=concurrency,
        skip_if_recent_days=skip_days,
    )
    log.info("Step 2 완료 (%.1f분): %s", (time.time() - t0) / 60, stats)
    return stats


def step_embed_universe(batch_size: int) -> dict:
    from src.idea import embedding
    from src.screener import db as screener_db
    log.info("=" * 60)
    log.info("Step 3: 임베딩 batch (text-embedding-3-small)")
    log.info("=" * 60)

    rows = screener_db.get_universe_for_scan(require_embedding=False)
    # embedding이 None인 종목만
    pending = [r for r in rows if not r.get("embedding")]
    log.info("임베딩 필요 종목: %d (전체 business_text 보유 %d)",
             len(pending), len(rows))
    if not pending:
        return {"embedded": 0, "skipped_existing": len(rows)}

    texts = [r["business_text"] or "" for r in pending]
    t0 = time.time()
    try:
        vecs = embedding.embed_texts(texts, batch_size=batch_size)
    except embedding.EmbeddingError as e:
        log.error("임베딩 호출 실패: %s", e)
        return {"embedded": 0, "error": str(e)}

    today = embedding.now_kst_date()
    embedded = 0
    for r, v in zip(pending, vecs):
        if v is None or v.size == 0:
            continue
        screener_db.upsert_embedding(
            ticker=r["ticker"],
            embedding=embedding.vec_to_bytes(v),
            model=embedding.DEFAULT_EMBED_MODEL,
            embedding_dt=today,
        )
        embedded += 1
    log.info("Step 3 완료 (%.1fs): %d종목 embedded", time.time() - t0, embedded)

    # 비용 추정 (text-embedding-3-small = $0.02/1M tokens)
    # 한국어 ~3 char/token, business_text 평균 ~4000 chars → ~1300 tokens
    est_tokens = sum(min(len(t), 12000) for t in texts) // 3
    est_cost = est_tokens * 0.02 / 1_000_000
    log.info("비용 추정: ~%d tokens, ~$%.4f", est_tokens, est_cost)

    return {"embedded": embedded, "est_tokens": est_tokens, "est_cost_usd": round(est_cost, 4)}


async def main_async(args: argparse.Namespace) -> int:
    from src.screener import db as screener_db

    pre = screener_db.universe_scan_stats()
    log.info("시작 시점 stats: %s", pre)

    if not args.skip_universe_refresh:
        await step_refresh_universe()

    text_stats = await step_bulk_business_text(
        limit=args.limit,
        force_refetch=args.force_refetch,
        concurrency=args.concurrency,
    )

    embed_stats = {}
    if not args.skip_embedding:
        embed_stats = step_embed_universe(batch_size=args.embed_batch_size)

    post = screener_db.universe_scan_stats()
    log.info("=" * 60)
    log.info("FINAL stats: %s", post)
    log.info("  - active 종목: %d", post["active"])
    log.info("  - business_text 보유: %d (%+d)", post["with_business_text"],
             post["with_business_text"] - pre["with_business_text"])
    log.info("  - embedding 보유: %d (%+d)", post["with_embedding"],
             post["with_embedding"] - pre["with_embedding"])
    log.info("Text fetch: %s", text_stats)
    log.info("Embedding: %s", embed_stats)
    log.info("=" * 60)
    return 0


def main() -> int:
    args = parse_args()
    if not os.getenv("DART_API_KEY"):
        log.error("DART_API_KEY 환경변수 필요")
        return 1
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
