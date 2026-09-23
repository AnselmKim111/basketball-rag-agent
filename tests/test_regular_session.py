"""정규장(15:30) 종가 — Naver 분봉 파싱·정규장 봉 산출·오늘 row 교체 회귀 테스트.

배경(2026-09-23 실측): NXT 통합 이후 Naver 일봉의 오늘 종가는 16:00~20:00 사이 계속
움직여 DB 저장값과 validator 재fetch값이 어긋남 → 매일 신호의 75~85%가 검증 탈락.
"""
from __future__ import annotations

from src.screener import data_source as ds

MINUTE_XML = """<protocol>
<chartdata symbol="159010" name="아스플로" count="6" timeframe="minute" precision="0" origintime="202609230900">
<item data="202609230900|null|null|null|24900|1000" />
<item data="202609231200|null|null|null|30750|500000" />
<item data="202609231529|null|null|null|29750|909000" />
<item data="202609231530|null|null|null|29800|910025" />
<item data="202609231540|null|null|null|29700|911000" />
<item data="202609231731|null|null|null|29550|916342" />
<item data="bad|row" />
</chartdata>
</protocol>"""


def test_parse_minute_bars_sorted_and_filtered():
    bars = ds.parse_naver_minute_bars(MINUTE_XML)
    assert [b[0][8:] for b in bars] == ["0900", "1200", "1529", "1530", "1540", "1731"]
    assert bars[3] == ("202609231530", 29800, 910025)


def test_regular_session_bar_excludes_after_hours():
    bars = ds.parse_naver_minute_bars(MINUTE_XML)
    o, h, l, c, v = ds.regular_session_bar(bars, "2026-09-23")
    assert c == 29800          # 15:30 동시호가 — 16:00 이후 29700/29550은 무시
    assert v == 910025         # 15:30 시점 누적 거래량
    assert (o, h, l) == (24900, 30750, 24900)
    assert ds.regular_session_bar(bars, "2026-09-22") is None


def test_override_replaces_only_today_row(monkeypatch):
    monkeypatch.delenv("SCREENER_REGULAR_CLOSE", raising=False)
    monkeypatch.setattr(ds, "fetch_regular_session_bar_via_naver",
                        lambda t, d, count=700: (24900, 30750, 24850, 29800, 910025))
    rows = [("159010", "2026-09-22", 22650, 24500, 22150, 24250, 207524, None),
            ("159010", "2026-09-23", 25100, 30950, 24550, 29550, 916342, None)]
    out = ds.apply_regular_session_override("159010", rows, today_iso="2026-09-23")
    assert out[0][5] == 24250                      # 어제 row 그대로
    assert out[1][2:7] == (24900, 30750, 24850, 29800, 910025)   # 전부 정규장 분봉 기준
    assert out[1][0] == "159010" and out[1][1] == "2026-09-23"


def test_override_keeps_daily_row_when_minute_fetch_fails(monkeypatch):
    monkeypatch.setattr(ds, "fetch_regular_session_bar_via_naver", lambda t, d, count=700: None)
    rows = [("159010", "2026-09-23", 24900, 30950, 24550, 29550, 916342, None)]
    assert ds.apply_regular_session_override("159010", rows, today_iso="2026-09-23") == rows


def test_override_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SCREENER_REGULAR_CLOSE", "0")
    called = []
    monkeypatch.setattr(ds, "fetch_regular_session_bar_via_naver",
                        lambda t, d, count=700: called.append(t) or (1, 1, 1, 1, 1))
    rows = [("159010", "2026-09-23", 24900, 30950, 24550, 29550, 916342, None)]
    assert ds.apply_regular_session_override("159010", rows, today_iso="2026-09-23") == rows
    assert called == []


def test_override_noop_when_today_absent(monkeypatch):
    called = []
    monkeypatch.setattr(ds, "fetch_regular_session_bar_via_naver",
                        lambda t, d, count=700: called.append(t))
    rows = [("159010", "2026-09-22", 1, 1, 1, 1, 1, None)]
    ds.apply_regular_session_override("159010", rows, today_iso="2026-09-23")
    assert called == []
