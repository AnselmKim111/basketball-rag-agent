"""src/idea_cache.py — JSON 영속 캐시 회귀 테스트.

save → load round-trip, partial id 매칭, cleanup_old 정책 검증.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def temp_cache(monkeypatch):
    """idea_cache가 임시 디렉터리 사용하도록 STATE_DIR override."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("STATE_DIR", tmp)
        # 모듈 reimport — _cache_root() 가 cache root 매번 다시 계산하므로 동적으로 작동
        import src.idea_cache as ic
        yield ic, Path(tmp) / "idea_history"


def _sample_record(idea_text: str = "test idea") -> dict:
    """save() args 한 세트."""
    return {
        "idea_text": idea_text,
        "parsed": {"thesis": "T"},
        "research": {"logic_gradient_text": "..."},
        "industry_pdfs": [Path("/tmp/x.pdf")],
        "industry_texts": {"x.pdf": "industry text"},
        "narrow": {"top10": [{"name": "AAA", "ticker6": "001234"}]},
        "top10": [{"name": "AAA", "ticker6": "001234"}],
        "company_pdfs_by_ticker": {"001234": [Path("/tmp/c.pdf")]},
        "company_texts_by_ticker": {"001234": {"c.pdf": "company text"}},
        "synthesis": {
            "top5": [{"rank": 1, "name": "AAA", "ticker6": "001234"}],
            "ranking_methodology": "M",
        },
        "download_root": Path("/tmp/idea/test"),
    }


def test_save_creates_json(temp_cache):
    ic, cache_dir = temp_cache
    args = _sample_record()
    entry_id = ic.save(**args)
    assert entry_id
    assert (cache_dir / f"{entry_id}.json").exists()


def test_save_and_load_round_trip(temp_cache):
    ic, _ = temp_cache
    args = _sample_record("KKKK")
    entry_id = ic.save(**args)
    record = ic.load(entry_id)
    assert record is not None
    assert record["idea_text"] == "KKKK"
    assert record["narrow"]["top10"][0]["ticker6"] == "001234"
    assert record["synthesis"]["top5"][0]["name"] == "AAA"


def test_load_nonexistent_returns_none(temp_cache):
    ic, _ = temp_cache
    assert ic.load("does-not-exist") is None


def test_find_by_partial_id(temp_cache):
    ic, _ = temp_cache
    entry_id = ic.save(**_sample_record())
    # 끝 6자리만 입력해도 매칭
    partial = entry_id[-6:]
    found = ic.find_by_partial_id(partial)
    assert found is not None
    assert found["id"] == entry_id


def test_find_by_partial_id_no_match(temp_cache):
    ic, _ = temp_cache
    assert ic.find_by_partial_id("zzzzzz") is None


def test_latest_returns_most_recent(temp_cache):
    ic, _ = temp_cache
    first = ic.save(**_sample_record("first"))
    time.sleep(1.01)  # mtime 차이 보장
    second = ic.save(**_sample_record("second"))
    latest = ic.latest()
    assert latest is not None
    assert latest["id"] == second
    assert latest["idea_text"] == "second"


def test_list_recent_orders_by_mtime(temp_cache):
    ic, _ = temp_cache
    ids = []
    for i in range(3):
        ids.append(ic.save(**_sample_record(f"idea-{i}")))
        time.sleep(1.01)
    entries = ic.list_recent(limit=5)
    assert len(entries) == 3
    # 최신순
    assert entries[0]["id"] == ids[-1]
    assert entries[2]["id"] == ids[0]


def test_cleanup_old_keeps_only_n(temp_cache):
    """cleanup_old는 mtime 기준 최신 keep개 보존.

    _new_id()가 초 단위라 같은 초에 만들어지면 파일명 충돌(덮어쓰기) — sleep 1.01초로
    각 entry가 별개 ID 받게 함.
    """
    ic, _ = temp_cache
    for i in range(5):
        ic.save(**_sample_record(f"i-{i}"))
        time.sleep(1.01)
    deleted = ic.cleanup_old(keep=3)
    assert deleted == 2
    assert len(ic.list_recent(limit=10)) == 3


def test_cleanup_old_keeps_all_if_under_limit(temp_cache):
    ic, _ = temp_cache
    ic.save(**_sample_record("only"))
    deleted = ic.cleanup_old(keep=10)
    assert deleted == 0
