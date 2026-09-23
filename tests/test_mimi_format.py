"""미미 스타일 메시지 (2026-09-23) — 업종 그룹핑·★·소형주 버킷·돌파직전 dedup·차트 계약."""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("pandas")

from src.screener.formatter import display_items, format_results


def _it(t, name, sector="", cap=5e11, chg=1.0):
    return {"ticker": t, "name": name, "sector": sector, "market_cap": cap, "chg_pct": chg}


def _results(**over):
    base = {k: [] for k in ("near_breakout_52w", "high_all", "high_52w", "high_52w_hi",
                            "high_52w_small", "high_26w", "vcp_breakout", "volume_surge",
                            "rs_leaders")}
    base.update(over)
    return base


def test_sector_grouped_one_line_and_star():
    r = _results(
        high_52w=[_it("A", "아스플로", "반도체", chg=22.9), _it("B", "엑시콘", "반도체", chg=13.0)],
        high_52w_hi=[_it("A", "아스플로", "반도체", chg=22.9), _it("C", "샘씨엔에스", "반도체", chg=3.3)],
        high_all=[_it("A", "아스플로", "반도체", chg=22.9), _it("D", "안랩", "소프트웨어", chg=3.5)],
    )
    msg = format_results(r, datetime(2026, 9, 23, 16, 5))
    assert msg.startswith("📈 신고가 — 2026.09.23.(수) 16:05 KST")
    assert "(반도체) 아스플로★, 엑시콘, 샘씨엔에스" in msg     # 중복 1회 + chg desc + ★
    assert "(소프트웨어) 안랩★" in msg
    assert "★ 역사적 신고가" in msg
    assert "종목 /" not in msg and "━━━" not in msg           # 숫자 열·구분선 없음
    assert msg.count("아스플로") == 1


def test_near_breakout_excludes_new_high_and_기타_last():
    r = _results(
        high_52w=[_it("A", "신고가주", "반도체")],
        near_breakout_52w=[_it("A", "신고가주", "반도체"), _it("B", "근접주", ""),
                           _it("C", "기계주", "기계")],
    )
    msg = format_results(r, datetime.now())
    near = msg.split("🎯 돌파 직전")[1]
    assert "신고가주" not in near
    assert near.index("(기계) 기계주") < near.index("(기타) 근접주")


def test_smallcap_bucket_line_and_no_chart():
    r = _results(
        high_52w_small=[_it("S1", "동전주", "기타", cap=4e10, chg=9.0)],
        high_52w=[_it("S2", "소형신고가", "반도체", cap=8e10, chg=2.0),
                  _it("B", "대형", "반도체", cap=5e11)],
    )
    msg = format_results(r, datetime.now())
    assert "(소형주/1,000억↓) 동전주, 소형신고가" in msg
    assert "(반도체) 대형" in msg
    d = display_items(r)
    assert {it["ticker"] for it in d["new_high"]} == {"B"}
    assert {it["ticker"] for it in d["new_high_small"]} == {"S1", "S2"}


def test_noise_categories_not_displayed():
    r = _results(high_26w=[_it("X", "회복주")], vcp_breakout=[_it("Y", "VCP주")],
                 volume_surge=[_it("Z", "수급주")], rs_leaders=[_it("W", "RS주")])
    msg = format_results(r, datetime.now())
    for n in ("회복주", "VCP주", "수급주", "RS주"):
        assert n not in msg
    assert "해당 없음" in msg


def test_links_and_ops_footer():
    r = _results(high_52w=[_it("A", "링크주", "반도체")])
    msg = format_results(r, datetime.now(), links={"A": "https://t.me/c/1"},
                         ops_notes=["검증제외 2"])
    assert '<a href="https://t.me/c/1">링크주</a>' in msg
    assert msg.rstrip().endswith("<i>〔운영: 검증제외 2〕</i>")


def test_base_date_shown_only_when_stale():
    r = _results()
    now = datetime(2026, 9, 23, 16, 0)
    assert "기준" not in format_results(r, now, base_date="2026-09-23")
    assert "(기준 2026-09-22 종가)" in format_results(r, now, base_date="2026-09-22")
