"""src/idea/scoring.py 회귀 테스트."""

from __future__ import annotations

from unittest.mock import patch


def test_extract_score_int():
    from src.idea.scoring import _extract_score
    assert _extract_score(8) == 8
    assert _extract_score(15) == 10  # cap
    assert _extract_score(0) == 1   # floor


def test_extract_score_string_with_explanation():
    """'8 — 사유' 같은 LLM 응답에서 첫 숫자 추출."""
    from src.idea.scoring import _extract_score
    assert _extract_score("8 — TC 본더 매출 70%") == 8
    assert _extract_score("점수: 9, 매우 강함") == 9


def test_extract_score_no_number_defaults_to_5():
    from src.idea.scoring import _extract_score
    assert _extract_score("알 수 없음") == 5
    assert _extract_score(None) == 5


def test_purity_from_breakdown_uses_breakdown():
    from src.idea.scoring import _purity_from_breakdown
    c = {"breakdown": {"idea_purity": "9 — pure-play"}, "purity_score": 5}
    assert _purity_from_breakdown(c) == 9


def test_purity_from_breakdown_falls_back():
    from src.idea.scoring import _purity_from_breakdown
    c = {"purity_score": 7}
    assert _purity_from_breakdown(c) == 7


def test_momentum_score_no_db():
    """signal_summary가 hit_count=0이면 0점."""
    from src.idea import scoring
    with patch("src.idea.scoring.screener_query.get_signal_summary",
               return_value={"hit_count": 0, "signals": [], "latest_date": None, "latest_signal": None}):
        score, meta = scoring._momentum_score("042700")
    assert score == 0
    assert meta["hit_count"] == 0


def test_momentum_score_invalid_ticker():
    from src.idea.scoring import _momentum_score
    score, meta = _momentum_score("ABCDEF")
    assert score == 0


def test_momentum_score_vcp_breakout():
    """vcp_breakout은 가중치 4.0 — 강한 신호."""
    from src.idea import scoring
    fake = {
        "hit_count": 1, "signals": ["vcp_breakout"],
        "latest_date": "2026-05-30", "latest_signal": "vcp_breakout",
    }
    with patch("src.idea.scoring.screener_query.get_signal_summary", return_value=fake):
        score, meta = scoring._momentum_score("042700")
    # raw = 4.0 (vcp) + 0.5 (1 hit) = 4.5 → round = 4 or 5
    assert score >= 4
    assert meta["signals"] == ["vcp_breakout"]


def test_momentum_score_multiple_signals():
    """52주 신고 + VCP + 거래량 다 hit → 강한 점수."""
    from src.idea import scoring
    fake = {
        "hit_count": 5,
        "signals": ["high_52w", "vcp_breakout", "volume_breakout"],
        "latest_date": "2026-06-01", "latest_signal": "vcp_breakout",
    }
    with patch("src.idea.scoring.screener_query.get_signal_summary", return_value=fake):
        score, _ = scoring._momentum_score("042700")
    # 3 + 4 + 2 + min(5,5)*0.5 = 11.5 → cap 10
    assert score == 10


def test_score_top10_db_unavailable():
    """screener 미가용 → momentum=0이지만 risk_reward는 op_lev × purity로 계산."""
    from src.idea import scoring
    top10 = [
        {"name": "한미반도체", "ticker6": "042700",
         "operating_leverage_score": 9, "breakdown": {"idea_purity": 9}},
    ]
    with patch("src.idea.scoring.screener_query.is_available", return_value=False):
        out = scoring.score_top10(top10)
    assert out[0]["momentum_score"] == 0
    # 9 * 0.9 + 0 * 0.3 = 8.1
    assert abs(out[0]["risk_reward_score"] - 8.1) < 0.001


def test_score_top10_with_momentum():
    """momentum 신호 있으면 risk_reward 가산."""
    from src.idea import scoring
    top10 = [
        {"name": "한미반도체", "ticker6": "042700",
         "operating_leverage_score": 9, "breakdown": {"idea_purity": 9}},
    ]
    fake_summary = {
        "hit_count": 1, "signals": ["high_52w"],
        "latest_date": "2026-05-30", "latest_signal": "high_52w",
    }
    with patch("src.idea.scoring.screener_query.is_available", return_value=True), \
         patch("src.idea.scoring.screener_query.get_signal_summary", return_value=fake_summary):
        out = scoring.score_top10(top10)
    # mom raw = 3.0 + 0.5 = 3.5 → round = 4
    assert out[0]["momentum_score"] == 4
    # 9 * 0.9 + 4 * 0.3 = 9.3
    assert abs(out[0]["risk_reward_score"] - 9.3) < 0.001
    assert out[0]["momentum_meta"]["signals"] == ["high_52w"]


def test_sort_by_risk_reward_desc():
    """risk_reward_score desc 정렬."""
    from src.idea.scoring import sort_by_risk_reward
    top10 = [
        {"name": "A", "risk_reward_score": 5.0, "purity_score": 8, "operating_leverage_score": 7},
        {"name": "B", "risk_reward_score": 9.0, "purity_score": 9, "operating_leverage_score": 9},
        {"name": "C", "risk_reward_score": 7.0, "purity_score": 7, "operating_leverage_score": 8},
    ]
    out = sort_by_risk_reward(top10)
    assert [c["name"] for c in out] == ["B", "C", "A"]


def test_select_top5_filters_to_5():
    from src.idea import scoring
    top10 = [
        {"name": f"종목{i}", "ticker6": f"00{i:04d}",
         "operating_leverage_score": 10 - i, "breakdown": {"idea_purity": 10 - i}}
        for i in range(10)
    ]
    with patch("src.idea.scoring.screener_query.is_available", return_value=False):
        out = scoring.select_top5(top10)
    assert len(out) == 5
    # 종목0이 score 가장 큼 (10*1 = 10)
    assert out[0]["name"] == "종목0"


def test_select_top5_momentum_lifts_lower_op_lev():
    """op_lev는 낮아도 strong momentum이면 top5 진입."""
    from src.idea import scoring
    top10 = [
        # A: op_lev 7, purity 7 → rr_no_mom = 4.9
        {"name": "A", "ticker6": "000001",
         "operating_leverage_score": 7, "breakdown": {"idea_purity": 7}},
        # B: op_lev 5, purity 5, momentum 강함 → rr = 2.5 + 10*0.3 = 5.5
        {"name": "B", "ticker6": "000002",
         "operating_leverage_score": 5, "breakdown": {"idea_purity": 5}},
    ]
    def fake_summary(t, days_back=14):
        if t == "000002":
            return {"hit_count": 5, "signals": ["vcp_breakout", "high_52w", "volume_breakout"],
                    "latest_date": "2026-06-01", "latest_signal": "vcp_breakout"}
        return {"hit_count": 0, "signals": [], "latest_date": None, "latest_signal": None}
    with patch("src.idea.scoring.screener_query.is_available", return_value=True), \
         patch("src.idea.scoring.screener_query.get_signal_summary", side_effect=fake_summary):
        out = scoring.select_top5(top10)
    # B는 momentum으로 A를 추월
    assert out[0]["name"] == "B"
