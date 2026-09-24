"""미국 매일 증분 업데이트 (화~토 07:00 KST cron = 미국 장마감 후).

2026-09-25 재설계:
  - 대상일 = **미국 동부시간 기준** 최근 마감 거래일 (`us_target_date`). 예전엔 KST 날짜(금 07:00
    KST → '금')를 대상으로 잡아 아직 열리지도 않은 날을 찾았고, 1순위 경로 결과가 통째로
    버려졌다 (target_match=0).
  - 종목별 fetch 병렬화(US_SCREENER_FETCH_WORKERS) — 유니버스 ~2,550종목.
  - Yahoo 빈 봉은 DB에 없는 날짜만 Nasdaq으로 보충 (known_dates 전달).
  - 보존 기간 > 백필 범위 (정리가 백필 트리거를 재점화하던 KR 루프와 동일 버그 예방).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from src.us_screener import data_source, db, universe

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 보존 기간(달력일) — 백필 범위(1260거래일 ≈ 1905달력일)보다 길어야 한다. 짧으면 정리 후
# max_len<1000 → 매일 백필 재트리거 (KR 2026-09-23 실측 동일 버그).
RETENTION_DAYS = 2000
# 미국 정규장 마감 16:00 ET + 데이터 반영 버퍼
US_CLOSE_READY_HHMM = (16, 30)


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _now_et() -> datetime:
    """미국 동부시간 현재 (DST 자동). zoneinfo 없으면 EDT(-4) 근사."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-4)))


def us_target_date(now_et: datetime | None = None):
    """가장 최근 **마감된** 미국 거래일 (date). 16:30 ET 이전이면 전 영업일. 주말 skip.

    공휴일은 모름 — 그날 데이터가 없으면 호출자가 target_match=0 → 직전 영업일 폴백.
    """
    now_et = now_et or _now_et()
    d = now_et.date()
    if d.weekday() >= 5 or (now_et.hour, now_et.minute) < US_CLOSE_READY_HHMM:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _last_business_day(d=None):
    """KST 기준 가장 최근 영업일 (오늘이 영업일이면 오늘) — date 객체."""
    if d is None:
        d = datetime.now(KST).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def update_today() -> dict:
    """미국 최근 마감 거래일 1일치 fetch + 오래된 데이터 정리.

    반환: {"date": iso_str, "rows": int, "is_business_day": bool, "empty": bool}.
    미국은 date-batch 소스가 없어 종목별 batch(update_specific_date)가 1순위.
    """
    db.ensure_schema()
    if not db.get_active_tickers():
        log.info("[incremental] universe 비어있음 → refresh")
        universe.refresh_universe()

    target = us_target_date()
    iso = target.strftime("%Y-%m-%d")
    res = update_specific_date(iso, force=True)
    if res.get("empty"):
        log.info("[incremental] %s(미국 거래일) 빈 결과 — 휴장일 또는 소스 장애", iso)
        return {"date": iso, "rows": 0, "is_business_day": True, "empty": True,
                "holiday_like": bool(res.get("holiday_like")), "coverage": res.get("coverage")}

    cutoff = (target - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    deleted = db.delete_older_than(cutoff)
    if deleted:
        log.info("[incremental] %d행 정리 (cutoff=%s)", deleted, cutoff)
    return {"date": iso, "rows": res.get("rows", 0), "is_business_day": True, "empty": False,
            "coverage": res.get("coverage")}


def update_specific_date(target_iso: str, force: bool = False) -> dict:
    """지정 날짜 OHLCV fetch + DB 추가. 휴장일 무관.

    반환: {"date": iso, "rows": int, "empty": bool}.
    데이터 소스 우선순위: Naver Finance(정확도 1순위) → pykrx → FDR ticker-batch.
    Naver는 한국 정규장 종가를 매일 정확히 갱신하므로 FDR/pykrx 환경 미스매치 우회.
    force=True면 이미 DB에 있어도 덮어씀 (잘못된 데이터 정정 용도).
    """
    db.ensure_schema()
    ymd = target_iso.replace("-", "")
    # 시총 desc 정렬 — 시총 큰 (대형주) 부터 fetch 보장. cap timeout 발생 시
    # 소형주만 누락. 사용자가 보는 "역사적 신고가"는 보통 중·대형주.
    all_tickers = db.get_active_tickers()
    all_tickers.sort(key=lambda t: -(t.get("market_cap") or 0))
    active_list = [t["ticker"] for t in all_tickers]
    active_set = set(active_list)

    # 0차: 종목별 batch (Yahoo 직접 → gap은 Nasdaq 보충 → FDR/Stooq 폴백) — 병렬
    cap = _int_env("US_SCREENER_FETCH_CAP", 5000)
    timeout_s = _int_env("US_SCREENER_FETCH_TIMEOUT_S", 900)
    workers = max(1, _int_env("US_SCREENER_FETCH_WORKERS", 6))
    rows: list[tuple] = []
    coverage: dict = {}
    if active_set:
        from datetime import datetime as _dt
        target_dt = _dt.strptime(target_iso, "%Y-%m-%d").date()
        # 넉넉히 (14일) 받아서 target 포함 + 최근 빈 봉 복구
        nv_start = (target_dt - timedelta(days=14)).strftime("%Y-%m-%d")
        nv_end = target_iso
        targets = active_list[:cap]
        log.info(
            "[incremental] %s 종목별 batch 시작: %d종목 (workers=%d, timeout=%ds)",
            target_iso, len(targets), workers, timeout_s,
        )

        def _one(t: str) -> tuple[str, list[tuple]]:
            known = db.dates_for_ticker(t, nv_start)
            return t, data_source.fetch_ohlcv_by_ticker_via_naver(t, nv_start, nv_end,
                                                                  known_dates=known)

        merged: list[tuple] = []
        hit: set[str] = set()
        failed: list[str] = []
        t0 = time.monotonic()
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from concurrent.futures import TimeoutError as _FutTimeout
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_one, t): t for t in targets}
            done = 0
            try:
                for fut in as_completed(futures, timeout=timeout_s + 30):
                    t = futures[fut]
                    try:
                        _, tr = fut.result()
                    except Exception as e:  # 한 종목 예외가 조용히 사라지지 않게 기록
                        log.warning("[incremental] %s fetch 예외: %s", t, e)
                        tr = []
                    merged.extend(tr)
                    if any(r[1] == target_iso for r in tr):
                        hit.add(t)
                    else:
                        failed.append(t)
                    done += 1
                    if done % 500 == 0:
                        log.info("[incremental] 진행 %d/%d (target_match=%d)", done, len(targets), len(hit))
                    if time.monotonic() - t0 > timeout_s:
                        log.warning("[incremental] timeout %ds → %d/%d에서 중단", timeout_s, done, len(targets))
                        for f in futures:
                            f.cancel()
                        break
            except _FutTimeout:
                log.warning("[incremental] futures timeout → %d/%d", done, len(targets))
                for f in futures:
                    f.cancel()
        not_done = [t for t in targets if t not in hit and t not in failed]
        coverage = {"target": len(targets), "hit": len(hit), "miss": len(failed) + len(not_done),
                    "elapsed_s": round(time.monotonic() - t0, 1)}
        log.info("[incremental] %s batch 완료: %s", target_iso, coverage)
        miss = failed + not_done
        if miss and hit:
            # 전체 미스가 아니라 일부 미스면 = 종목별 문제 (거래정지·심볼변경·소스 결측) — 명단 기록
            log.warning("[incremental] %s 누락 %d종목: %s", target_iso, len(miss), ", ".join(miss[:60]))
        if hit:
            rows = merged
        elif merged:
            # 다른 날짜는 받았는데 target만 전 종목 없음 = 미국 휴장일(또는 target 미확정).
            # 받은 최근 봉(빈 봉 복구분 포함)은 저장하고, 무거운 FDR 순차 폴백은 건너뜀.
            inserted = db.upsert_ohlcv_bulk(merged)
            log.info("[incremental] %s target 봉 0 — 휴장일 판단, 최근 봉 %d행만 갱신",
                     target_iso, inserted)
            return {"date": target_iso, "rows": 0, "empty": True, "holiday_like": True,
                    "coverage": coverage}

    # 1차 폴백: pykrx (Naver가 빈 결과일 때만)
    if not rows:
        rows = data_source.fetch_market_ohlcv_by_date(ymd)
        if rows and active_set:
            rows = [r for r in rows if r[0] in active_set]

    # 2차 폴백: FDR ticker-batch
    if not rows and active_set:
        cap = _int_env("US_SCREENER_FDR_CAP", 1000)
        timeout_s = _int_env("US_SCREENER_FDR_TIMEOUT_S", 480)
        log.warning(
            "[incremental] %s pykrx 빈 결과 → FDR ticker-batch 폴백 (cap=%d, timeout=%ds)",
            target_iso, cap, timeout_s,
        )
        # 시총 3000억+ 종목만 우선 (보유 universe 정렬: 시총 큰 순서로 가져오면 좋지만
        # 단순화 — DB ticker 정렬). target 날짜 전후 5일 fetch (어제·오늘 모두 포함되도록)
        from datetime import datetime as _dt
        target_dt = _dt.strptime(target_iso, "%Y-%m-%d").date()
        start_iso = (target_dt - timedelta(days=5)).strftime("%Y-%m-%d")
        end_iso = (target_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        merged: list[tuple] = []
        target_match: list[tuple] = []
        t0 = time.monotonic()
        scanned = 0
        first_diag_logged = False
        for ticker in sorted(active_set)[:cap]:
            if time.monotonic() - t0 > timeout_s:
                log.warning(
                    "[incremental] FDR 폴백 timeout %ds 초과 → %d/%d에서 중단",
                    timeout_s, scanned, cap,
                )
                break
            try:
                tr = data_source.fetch_ohlcv_by_ticker_via_fdr(ticker, start_iso, end_iso)
                # 진단: 첫 응답 ticker의 실제 dates + close 출력 (FDR이 진짜 target 날짜
                # 데이터를 주는지 확인). 시뮬레이션 환경에서 외부 API가 미래 날짜에
                # 어떤 응답을 주는지 디버깅 용도.
                if not first_diag_logged and tr:
                    dates_close = [(r[1], r[5]) for r in tr]  # (date_iso, close)
                    log.warning(
                        "[incremental] DIAG FDR(%s) range=%s~%s actual=%s",
                        ticker, start_iso, end_iso, dates_close,
                    )
                    first_diag_logged = True
                merged.extend(tr)
                target_match.extend([r for r in tr if r[1] == target_iso])
            except Exception:
                pass
            scanned += 1
            if scanned % 100 == 0:
                log.info(
                    "[incremental] FDR 진행 %d/%d (rows=%d, target_match=%d)",
                    scanned, cap, len(merged), len(target_match),
                )
        rows = merged
        log.info(
            "[incremental] FDR 폴백 완료 scanned=%d total_rows=%d target_match=%d",
            scanned, len(rows), len(target_match),
        )

    if not rows:
        return {"date": target_iso, "rows": 0, "empty": True}

    inserted = db.upsert_ohlcv_bulk(rows)
    log.info("[incremental] %s 강제 fetch 추가 rows=%d", target_iso, inserted)
    return {"date": target_iso, "rows": inserted, "empty": False, "coverage": coverage}


def ensure_recent_business_day_data() -> dict:
    """가장 최근 영업일까지 데이터 보장. 호출자가 16:00 cron에서 today 미발행이거나
    주말일 때 사용.

    동작:
      1. KST 오늘이 영업일이면 today 시도 → 실패 시 어제 영업일 시도
      2. 오늘이 주말이면 가장 최근 평일(금) 시도
      3. 이미 DB에 있는 날짜면 skip

    반환: {"date": iso, "rows": int, "empty": bool, "source": "today"|"recent"}.
    """
    db.ensure_schema()
    target = us_target_date()
    target_iso = target.strftime("%Y-%m-%d")

    # cached 여부와 무관하게 Naver 1차 fetch (이전 잘못된 데이터 정정 가능)
    # 강제로 다시 받지 않으려면 SCREENER_FORCE_REFETCH=0 으로 설정.
    force_refetch = _int_env("SCREENER_FORCE_REFETCH", 1) == 1

    if not force_refetch and db.has_date(target_iso):
        log.info("[incremental] %s 이미 DB에 있음 (force_refetch=0)", target_iso)
        return {"date": target_iso, "rows": 0, "empty": False, "source": "cached"}

    # target 시도 (Naver→pykrx→FDR 순)
    res = update_specific_date(target_iso, force=force_refetch)
    if not res["empty"]:
        return {**res, "source": "target"}

    # 실패 시 직전 영업일 시도 (최대 5영업일)
    d = target - timedelta(days=1)
    for _ in range(5):
        d = _last_business_day(d)
        d_iso = d.strftime("%Y-%m-%d")
        if db.has_date(d_iso):
            log.info("[incremental] fallback %s 이미 DB에 있음", d_iso)
            return {"date": d_iso, "rows": 0, "empty": False, "source": "fallback_cached"}
        res = update_specific_date(d_iso)
        if not res["empty"]:
            log.info("[incremental] fallback %s 사용", d_iso)
            return {**res, "source": "fallback"}
        d -= timedelta(days=1)

    log.warning("[incremental] 최근 영업일 5일 모두 fetch 실패 — 누적 DB로 진행")
    return {"date": target_iso, "rows": 0, "empty": True, "source": "none"}

