"""매일 1일치 증분 업데이트 — KR/US 공통 구현.

`make_api(db, data_source, universe)`가 의존 모듈을 closure로 묶어 함수들을 반환.
시장별 wrapper(`src/screener/incremental.py`, `src/us_screener/incremental.py`)가
자기 시장의 모듈을 주입하고 메서드를 재노출.

매일 cron 호출. 오늘 거래일 OHLCV를 fetch해 DB에 추가.
KRX 데이터는 보통 16:30~17:00 사이 발행되므로 16:00에는 미발행. 호출자가
재시도(최대 ~30분)할 수 있도록 fetch_today_or_recent를 별도 제공.

또한 280일 이전 데이터는 정리 (DB 비대화 방지).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 5년(1260거래일) + 여유 = 1400일치 보관 (역사적 신고가 ATH 계산용)
RETENTION_DAYS = 1400


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _last_business_day(d=None):
    """KST 기준 가장 최근 영업일 (오늘이 영업일이면 오늘) — date 객체."""
    if d is None:
        d = datetime.now(KST).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def make_api(db, data_source, universe) -> SimpleNamespace:
    """시장 모듈 주입 → public 함수들을 closure로 묶어 namespace 반환."""

    def update_today() -> dict:
        """오늘(KST) 데이터 fetch + 280일 이전 정리.

        반환: {"date": iso_str, "rows": int, "is_business_day": bool, "empty": bool}.

        pykrx date-batch 실패 시 FDR ticker-batch 폴백은 기본 비활성화 (sequential
        수천 종목 fetch가 1시간+ 걸려 self-test/cron을 hang시키기 때문).
        """
        db.ensure_schema()
        if not db.get_active_tickers():
            log.info("[incremental] universe 비어있음 → refresh")
            universe.refresh_universe()
        active_set = {t["ticker"] for t in db.get_active_tickers()}

        today = datetime.now(KST).date()
        iso = today.strftime("%Y-%m-%d")
        ymd = today.strftime("%Y%m%d")

        if not data_source.is_business_day(today):
            log.info("[incremental] %s 주말 — skip", iso)
            return {"date": iso, "rows": 0, "is_business_day": False, "empty": True}

        rows = data_source.fetch_market_ohlcv_by_date(ymd)
        if active_set:
            rows = [r for r in rows if r[0] in active_set]

        if not rows and active_set:
            if _int_env("SCREENER_INCREMENTAL_FDR_FALLBACK", 0) == 1:
                cap = _int_env("SCREENER_INCREMENTAL_FDR_CAP", 80)
                timeout_s = _int_env("SCREENER_INCREMENTAL_FDR_TIMEOUT_S", 180)
                log.warning(
                    "[incremental] %s pykrx 빈 결과 → FDR ticker-batch 폴백 (cap=%d, timeout=%ds)",
                    iso, cap, timeout_s,
                )
                start_iso = (today - timedelta(days=5)).strftime("%Y-%m-%d")
                end_iso = iso
                merged: list[tuple] = []
                t0 = time.monotonic()
                scanned = 0
                for ticker in sorted(active_set)[:cap]:
                    if time.monotonic() - t0 > timeout_s:
                        log.warning(
                            "[incremental] FDR 폴백 timeout %ds 초과 → %d/%d에서 중단",
                            timeout_s, scanned, cap,
                        )
                        break
                    try:
                        tr = data_source.fetch_ohlcv_by_ticker_via_fdr(ticker, start_iso, end_iso)
                        merged.extend([r for r in tr if r[1] >= start_iso])
                    except Exception:
                        pass
                    scanned += 1
                    if scanned % 50 == 0:
                        log.info("[incremental] FDR 폴백 진행 %d/%d (rows=%d)", scanned, cap, len(merged))
                rows = merged
                log.info("[incremental] FDR 폴백 완료 scanned=%d rows=%d", scanned, len(rows))
            else:
                log.info(
                    "[incremental] %s pykrx 빈 결과 (KRX 16:30 이전 미발행 가능) → 호출자 재시도 또는 누적 DB로 진행",
                    iso,
                )

        if not rows:
            log.info("[incremental] %s 빈 결과", iso)
            return {"date": iso, "rows": 0, "is_business_day": True, "empty": True}

        inserted = db.upsert_ohlcv_bulk(rows)

        # 오래된 데이터 정리
        cutoff = (today - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
        deleted = db.delete_older_than(cutoff)
        if deleted:
            log.info("[incremental] %d행 정리 (cutoff=%s)", deleted, cutoff)

        log.info("[incremental] %s 추가 rows=%d", iso, inserted)
        return {"date": iso, "rows": inserted, "is_business_day": True, "empty": False}

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

        # 0차: Naver Finance ticker-batch (1순위 — FDR 시뮬레이션 미스매치 우회)
        naver_cap = _int_env("SCREENER_NAVER_CAP", 1200)
        naver_timeout = _int_env("SCREENER_NAVER_TIMEOUT_S", 600)
        rows: list[tuple] = []
        if active_set:
            log.info(
                "[incremental] %s Naver Finance ticker-batch 시작 (cap=%d, timeout=%ds)",
                target_iso, naver_cap, naver_timeout,
            )
            from datetime import datetime as _dt
            target_dt = _dt.strptime(target_iso, "%Y-%m-%d").date()
            # 충분히 넓게 (10일) 받아서 target 날짜 포함 보장
            nv_start = (target_dt - timedelta(days=10)).strftime("%Y-%m-%d")
            nv_end = (target_dt + timedelta(days=1)).strftime("%Y-%m-%d")
            merged: list[tuple] = []
            target_match: list[tuple] = []
            t0 = time.monotonic()
            scanned = 0
            first_diag = False
            for ticker in active_list[:naver_cap]:  # 시총 desc 정렬된 순서대로
                if time.monotonic() - t0 > naver_timeout:
                    log.warning("[incremental] Naver timeout %ds → %d에서 중단", naver_timeout, scanned)
                    break
                try:
                    tr = data_source.fetch_ohlcv_by_ticker_via_naver(ticker, nv_start, nv_end)
                    if not first_diag and tr:
                        sample = [(r[1], r[5]) for r in tr]
                        log.warning("[incremental] DIAG NAVER(%s) range=%s~%s actual=%s",
                                    ticker, nv_start, nv_end, sample)
                        first_diag = True
                    merged.extend(tr)
                    target_match.extend([r for r in tr if r[1] == target_iso])
                except Exception as e:
                    log.debug("Naver %s 실패: %s", ticker, e)
                scanned += 1
                if scanned % 200 == 0:
                    log.info(
                        "[incremental] Naver 진행 %d/%d (rows=%d, target_match=%d)",
                        scanned, naver_cap, len(merged), len(target_match),
                    )
            log.info(
                "[incremental] Naver 완료 scanned=%d total_rows=%d target_match=%d",
                scanned, len(merged), len(target_match),
            )
            # target 날짜 매치된 게 있으면 그걸 사용
            if target_match:
                rows = merged

        # 1차 폴백: pykrx (Naver가 빈 결과일 때만)
        if not rows:
            rows = data_source.fetch_market_ohlcv_by_date(ymd)
            if rows and active_set:
                rows = [r for r in rows if r[0] in active_set]

        # 2차 폴백: FDR ticker-batch
        if not rows and active_set:
            cap = _int_env("SCREENER_INCREMENTAL_FDR_CAP", 1000)
            timeout_s = _int_env("SCREENER_INCREMENTAL_FDR_TIMEOUT_S", 480)
            log.warning(
                "[incremental] %s pykrx 빈 결과 → FDR ticker-batch 폴백 (cap=%d, timeout=%ds)",
                target_iso, cap, timeout_s,
            )
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
                    if not first_diag_logged and tr:
                        dates_close = [(r[1], r[5]) for r in tr]
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
        return {"date": target_iso, "rows": inserted, "empty": False}

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
        today = datetime.now(KST).date()
        target = _last_business_day(today)
        target_iso = target.strftime("%Y-%m-%d")

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

    return SimpleNamespace(
        update_today=update_today,
        update_specific_date=update_specific_date,
        ensure_recent_business_day_data=ensure_recent_business_day_data,
    )
