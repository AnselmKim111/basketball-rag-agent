"""보존 기간 ≥ 백필 범위 — 정리(prune)가 백필 트리거(max_len<1000)를 다시 켜는 루프 방지."""
from __future__ import annotations

import pytest

pytest.importorskip("pandas")


def test_retention_covers_backfill_horizon():
    from src.screener import backfill, incremental
    horizon_days = int(backfill.DEFAULT_DAYS * 1.5) + 15      # backfill start 계산식과 동일
    assert incremental.RETENTION_DAYS >= horizon_days
    # 1000 거래일(백필 트리거 max_len 기준)을 달력일로 넉넉히 커버 (×1.45)
    assert incremental.RETENTION_DAYS >= 1000 * 1.45
