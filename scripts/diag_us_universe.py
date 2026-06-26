"""US 광역 유니버스 소스 가용성 진단 (읽기 전용, DB 미접촉).

배포 환경에서 1회 실행해 소스 체인이 동작하는지·RVMD가 포함되는지 확인:

    python -m scripts.diag_us_universe          # 전체 소스 체인
    python -m scripts.diag_us_universe RVMD NVDA  # 특정 심볼 포함 여부

출력: 각 소스별 종목수/시총결측, $2B floor 후 종목수, 지정 심볼 포함 여부+시총.
네트워크 정책으로 Nasdaq API가 막히면 FDR→안전망 폴백이 작동하는지 드러남.
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from src.us_screener import data_source as ds
from src.us_screener import universe as uni


def main(targets: list[str]) -> None:
    targets = [t.strip().upper().replace("/", ".") for t in targets] or ["RVMD", "NVDA", "AAPL"]
    floor = uni._universe_min_cap()
    print(f"\n=== 소스 체인 진단 (floor=${floor/1e9:.1f}B) ===")

    # 소스별 개별 호출 (체인 우회 — 각 소스 상태 직접 확인)
    print("\n[1순위] Nasdaq screener API")
    try:
        nq = ds._fetch_nasdaq_screener()
        with_cap = sum(1 for l in nq if l[2])
        print(f"  → {len(nq)}종목 (시총보유 {with_cap}, 결측 {len(nq)-with_cap})")
    except Exception as e:
        nq = []
        print(f"  → 실패: {e!r}")

    print("\n[2순위] FDR 거래소 listing")
    try:
        fdr = ds._fetch_fdr_exchange()
        with_cap = sum(1 for l in fdr if l[2])
        print(f"  → {len(fdr)}종목 (시총보유 {with_cap})")
    except Exception as e:
        print(f"  → 실패: {e!r}")

    # 실제 체인 (캐시 통한 최종 선택)
    print("\n[체인 결과] fetch_us_listings()")
    listings = ds.fetch_us_listings(force=True)
    with_cap = sum(1 for l in listings if l[2])
    after_floor = [l for l in listings if l[2] and l[2] >= floor]
    print(f"  → 총 {len(listings)}종목 (시총보유 {with_cap})")
    print(f"  → $2B floor 후: {len(after_floor)}종목 (일일 fetch 대상)")

    print(f"\n=== 지정 심볼 포함 여부 ===")
    bysym = {l[0]: l for l in listings}
    for t in targets:
        l = bysym.get(t)
        if not l:
            print(f"  {t}: ❌ 광역 소스에 없음")
            continue
        cap = l[2]
        in_uni = cap is not None and cap >= floor
        cap_s = f"${cap/1e9:.2f}B" if cap else "결측"
        print(f"  {t}: ✅ 소스 포함 / 시총 {cap_s} / 섹터 {l[3]} / 거래소 {l[4]} "
              f"→ 유니버스 {'진입 ✅' if in_uni else '제외 ❌(floor 미달/결측)'}")


if __name__ == "__main__":
    main(sys.argv[1:])
