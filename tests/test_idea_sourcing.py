"""src/idea/sourcing.py 회귀 테스트.

screener.db 미가용 환경에서도 graceful해야 함. 실제 DB 통합 호출은 monkeypatch로 모킹.
"""

from __future__ import annotations

from unittest.mock import patch


def test_dedup_candidates_basic():
    """동일 (name, ticker) → 1개로 통합. 빈 필드는 후순위 항목으로 보완."""
    from src.idea.sourcing import _dedup_candidates
    items = [
        {"name": "삼성전자", "ticker6": "005930", "industry": "반도체", "mcap_estimate_krw_eok": 5000000},
        {"name": "삼성전자", "ticker6": "005930", "industry": "", "mechanism": "메모리 회복"},
    ]
    out = _dedup_candidates(items)
    assert len(out) == 1
    # 첫 항목 정보 유지 + 두 번째 항목 mechanism 보강
    assert out[0]["industry"] == "반도체"
    assert out[0]["mechanism"] == "메모리 회복"


def test_dedup_candidates_normalizes_name():
    """`(주)카카오`와 `카카오`는 동일 회사로 dedup."""
    from src.idea.sourcing import _dedup_candidates
    items = [
        {"name": "(주)카카오", "ticker6": "035720"},
        {"name": "카카오", "ticker6": "035720"},
    ]
    out = _dedup_candidates(items)
    assert len(out) == 1


def test_dedup_candidates_skips_empty_name():
    from src.idea.sourcing import _dedup_candidates
    items = [
        {"name": "", "ticker6": "005930"},
        {"name": "삼성전자", "ticker6": "005930"},
    ]
    out = _dedup_candidates(items)
    assert len(out) == 1
    assert out[0]["name"] == "삼성전자"


def test_dedup_candidates_invalid_ticker_treated_as_blank():
    """ticker6 형식 안 맞으면 dedup 키에서 빈 문자열로 — name만으로 dedup."""
    from src.idea.sourcing import _dedup_candidates
    items = [
        {"name": "삼성전자", "ticker6": "ABCDEF"},
        {"name": "삼성전자", "ticker6": "005930"},
    ]
    out = _dedup_candidates(items)
    # 둘 다 normalize_name('삼성전자')는 같으나 ticker6 키가 다름
    # 첫 항목 ticker6은 invalid → '' / 두 번째는 '005930' → 다른 키
    assert len(out) == 2


def test_dedup_candidates_handles_none_and_empty():
    from src.idea.sourcing import _dedup_candidates
    assert _dedup_candidates([]) == []
    assert _dedup_candidates([None, {}, {"name": "삼성", "ticker6": "005930"}]) == [
        {"name": "삼성", "ticker6": "005930"},
    ]


def test_from_screener_universe_db_unavailable():
    """DB 없으면 빈 리스트."""
    from src.idea import sourcing
    with patch("src.idea.sourcing.screener_query.is_available", return_value=False):
        out = sourcing.from_screener_universe({}, [])
        assert out == []


def test_from_screener_universe_basic():
    """is_available=True + sectors 키워드 → filtered universe → research candidate 형태 변환."""
    from src.idea import sourcing
    fake_rows = [
        {"ticker": "042700", "name": "한미반도체", "market_cap": 5_000_000_000_000,
         "sector": "반도체장비", "market": "KOSPI"},
        {"ticker": "058470", "name": "리노공업", "market_cap": 3_000_000_000_000,
         "sector": "반도체장비", "market": "KOSDAQ"},
    ]
    with patch("src.idea.sourcing.screener_query.is_available", return_value=True), \
         patch("src.idea.sourcing.screener_query.get_universe_filtered", return_value=fake_rows):
        out = sourcing.from_screener_universe(
            constraints={"exchange": "KOSPI", "industry_filter": ["반도체"]},
            industries=[{"name": "반도체 후공정", "gics_hint": "Semiconductors"}],
        )
    assert len(out) == 2
    # 시총 desc로 정렬
    assert out[0]["name"] == "한미반도체"
    assert out[1]["name"] == "리노공업"
    # research candidate 포맷 검증
    for c in out:
        assert "name" in c and "ticker6" in c and "size_tier" in c
        assert "mcap_estimate_krw_eok" in c
        assert c["source"] == "screener"
    # size_tier 분류 (5조 → mid, 3조 → mid)
    assert out[0]["size_tier"] == "mid"
    assert out[1]["size_tier"] == "mid"
    # 시총 정확 변환 (5조 = 50000억)
    assert out[0]["mcap_estimate_krw_eok"] == 50000


def test_from_screener_universe_size_tier_classification():
    """시총별 size_tier 자동 분류 검증."""
    from src.idea import sourcing
    fake_rows = [
        {"ticker": "005930", "name": "삼성전자", "market_cap": 400_000_000_000_000,
         "sector": "반도체", "market": "KOSPI"},  # 400조 → large
        {"ticker": "042700", "name": "한미반도체", "market_cap": 5_000_000_000_000,
         "sector": "반도체장비", "market": "KOSPI"},  # 5조 → mid
        {"ticker": "999999", "name": "소형주", "market_cap": 500_000_000_000,
         "sector": "반도체", "market": "KOSDAQ"},  # 5천억 → small
    ]
    with patch("src.idea.sourcing.screener_query.is_available", return_value=True), \
         patch("src.idea.sourcing.screener_query.get_universe_filtered", return_value=fake_rows):
        out = sourcing.from_screener_universe({}, [{"name": "반도체"}])
    by_name = {c["name"]: c for c in out}
    assert by_name["삼성전자"]["size_tier"] == "large"
    assert by_name["한미반도체"]["size_tier"] == "mid"
    assert by_name["소형주"]["size_tier"] == "small"


def test_from_screener_universe_max_picks():
    """max_picks 상한 적용."""
    from src.idea import sourcing
    fake_rows = [
        {"ticker": f"00{i:04d}", "name": f"종목{i}", "market_cap": (100 - i) * 10**11,
         "sector": "반도체", "market": "KOSPI"}
        for i in range(50)
    ]
    with patch("src.idea.sourcing.screener_query.is_available", return_value=True), \
         patch("src.idea.sourcing.screener_query.get_universe_filtered", return_value=fake_rows):
        out = sourcing.from_screener_universe({}, [{"name": "반도체"}], max_picks=10)
    assert len(out) == 10
    # 시총 desc → 종목0이 가장 큼
    assert out[0]["name"] == "종목0"


def test_enrich_with_screener_data_replaces_estimates():
    """LLM 추측 시총을 screener.db 정확 값으로 교체."""
    from src.idea import sourcing
    candidates = [
        {"name": "한미반도체", "ticker6": "042700",
         "mcap_estimate_krw_eok": 30000, "size_tier": "small"},  # 추측: 3조 / 실제: 5조
    ]
    fake_universe = [
        {"ticker": "042700", "name": "한미반도체", "market_cap": 5_000_000_000_000,
         "sector": "반도체장비", "market": "KOSPI"},
    ]
    with patch("src.idea.sourcing.screener_query.is_available", return_value=True), \
         patch("src.idea.sourcing.screener_query.get_universe", return_value=fake_universe):
        out = sourcing.enrich_with_screener_data(candidates)
    assert out[0]["mcap_estimate_krw_eok"] == 50000  # 5조 → 5만 억
    assert out[0]["size_tier"] == "mid"  # 재분류
    assert out[0]["screener_sector"] == "반도체장비"
    assert out[0]["screener_market"] == "KOSPI"


def test_enrich_with_screener_data_db_unavailable():
    """DB 미가용 → 입력 그대로 반환."""
    from src.idea import sourcing
    candidates = [{"name": "삼성전자", "ticker6": "005930", "mcap_estimate_krw_eok": 4_000_000}]
    with patch("src.idea.sourcing.screener_query.is_available", return_value=False):
        out = sourcing.enrich_with_screener_data(candidates)
    assert out == candidates


def test_enrich_with_screener_data_empty():
    from src.idea import sourcing
    assert sourcing.enrich_with_screener_data([]) == []


def test_enrich_with_screener_data_invalid_ticker():
    """ticker6이 6자리 아니면 enrich 스킵 (LLM 비상장 hallucination 케이스)."""
    from src.idea import sourcing
    candidates = [{"name": "비상장", "ticker6": "ABCDEF", "mcap_estimate_krw_eok": 100}]
    with patch("src.idea.sourcing.screener_query.is_available", return_value=True), \
         patch("src.idea.sourcing.screener_query.get_universe", return_value=[]):
        out = sourcing.enrich_with_screener_data(candidates)
    assert out[0]["mcap_estimate_krw_eok"] == 100  # 변경 없음


def test_build_candidate_pool_merges_sources():
    """research + screener 후보 merge + dedup + 시총 desc 정렬 + target_size 상한."""
    from src.idea import sourcing
    research = {
        "candidates": [
            {"name": "한미반도체", "ticker6": "042700", "mcap_estimate_krw_eok": 50000},
            {"name": "리노공업", "ticker6": "058470", "mcap_estimate_krw_eok": 30000},
        ],
        "industries": [{"name": "반도체"}],
    }
    parsed = {"constraints": {}}
    fake_universe = [
        {"ticker": "042700", "name": "한미반도체", "market_cap": 5_000_000_000_000,
         "sector": "반도체장비", "market": "KOSPI"},
        {"ticker": "058470", "name": "리노공업", "market_cap": 3_000_000_000_000,
         "sector": "반도체장비", "market": "KOSDAQ"},
        # screener에만 있는 새 종목
        {"ticker": "095610", "name": "테스나", "market_cap": 800_000_000_000,
         "sector": "반도체장비", "market": "KOSDAQ"},
    ]
    with patch("src.idea.sourcing.screener_query.is_available", return_value=True), \
         patch("src.idea.sourcing.screener_query.get_universe", return_value=fake_universe), \
         patch("src.idea.sourcing.screener_query.get_universe_filtered", return_value=fake_universe):
        pool = sourcing.build_candidate_pool(research, parsed, target_size=10)
    names = [c["name"] for c in pool]
    # 시총 desc → 한미반도체 > 리노공업 > 테스나
    assert names[0] == "한미반도체"
    assert "테스나" in names  # screener 발굴 추가
    # dedup → 한미반도체 1개만
    assert names.count("한미반도체") == 1


def test_build_candidate_pool_target_size_cap():
    """target_size 상한 적용."""
    from src.idea import sourcing
    research = {
        "candidates": [
            {"name": f"종목{i}", "ticker6": f"00{i:04d}", "mcap_estimate_krw_eok": (100 - i) * 100}
            for i in range(20)
        ],
        "industries": [{"name": "반도체"}],
    }
    with patch("src.idea.sourcing.screener_query.is_available", return_value=False):
        pool = sourcing.build_candidate_pool(research, {"constraints": {}}, target_size=5)
    assert len(pool) == 5
