"""미국 매일 증분 업데이트 (화~토 07:00 KST cron = 미국 장마감 후).

2026-09-25 재설계 (사고: "61종목 계산 · 458종목 base_date 누락"):
  - 대상일 = **NYSE 캘린더 기준 마지막으로 마감된 거래일** (`us_target_date`). 예전엔 KST
    날짜(금 07:00 KST → '금')를 대상으로 잡아 아직 열리지 않은 날을 찾았다.
  - 전 종목 sweep 전에 대형주 5개로 probe — 대상일 봉이 하나도 없으면 sweep 생략하고
    `source_down`으로 보고 (Yahoo 장애일에 전 종목 fetch를 반복하던 문제).
  - 종목별 fetch 병렬(US_SCREENER_FETCH_WORKERS), 누락 종목 1회 재시도 패스, 커버리지 집계.
  - Yahoo 빈 봉은 DB에 없는 날짜만 보충 (Nasdaq → 분봉 재구성).
  - 재활성화 종목: fetch 시작을 DB 최신일+1까지 당겨 공백 방지.
  - 액면분할·ADR 비율 변경: 최근 구간 종가가 DB와 일정 비율로 어긋나면 전체 이력 재구축.
  - 보존 기간 > 백필 범위 (정리가 백필 트리거를 재점화하던 KR 루프와 동일 버그 예방).
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as _FutTimeout
from datetime import date, datetime, timedelta, timezone

from src.us_screener import data_source, db, market_calendar, universe

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 보존 기간(달력일) — 백필 범위(1260거래일 ≈ 1905달력일)보다 길어야 한다. 짧으면 정리 후
# max_len<1000 → 매일 백필 재트리거 (KR 2026-09-23 실측 동일 버그).
RETENTION_DAYS = 2000
US_CLOSE_READY_HHMM = data_source.US_CLOSE_READY_HHMM
WINDOW_DAYS = 14                 # 매일 받는 최근 구간 (빈 봉 복구 여유)
REACTIVATE_MAX_DAYS = 400        # 이보다 오래 비었으면 증분 대신 백필(pending)이 담당
PROBE_TICKERS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL")
SPLIT_TOLERANCE = 0.02           # 최근 구간 종가가 DB와 2% 넘게, 일정 비율로 어긋나면 분할로 판단


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _now_et() -> datetime:
    return data_source.now_et()


def us_target_date(now_et: datetime | None = None) -> date:
    """가장 최근 **마감된** NYSE 거래일. 16:30 ET 이전이면 오늘 제외. 주말·NYSE 휴장 skip."""
    now_et = now_et or _now_et()
    d = now_et.date()
    if (now_et.hour, now_et.minute) < US_CLOSE_READY_HHMM:
        d -= timedelta(days=1)
    return market_calendar.last_trading_day_on_or_before(d)


def _last_business_day(d=None):
    """(호환) KST 기준 가장 최근 평일."""
    if d is None:
        d = datetime.now(KST).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ------------------------------------------------------------------
# 분할 감지
# ------------------------------------------------------------------
def detect_split(fetched: list[tuple], db_closes: dict[str, int]) -> float | None:
    """겹치는 날짜의 (fetched close / DB close) 비율이 모두 1±tol 밖이고 서로 1% 안이면 그 비율.

    Yahoo 일봉은 조회 시점 기준 분할 조정 — 분할 후 최근 구간만 덮어쓰면 과거 행과 스케일이
    섞여 가짜 신고가가 난다 (리뷰 지적). 겹치는 날짜 2개 이상일 때만 판단.
    """
    pairs = sorted((r[1], r[5] / db_closes[r[1]]) for r in fetched
                   if r[1] in db_closes and db_closes[r[1]] > 0 and r[5] > 0)
    if len(pairs) < 2:
        return None
    # 가장 오래된 날짜부터 같은 비율(서로 1% 이내)로 이어지는 구간 — Yahoo가 조정을 하루 늦게
    # 반영하면 최신 1~2개 DB 행은 이미 새 스케일(비율≈1)이라 전체 일치를 요구하면 놓친다 (리뷰 지적).
    run = [pairs[0][1]]
    for _, r in pairs[1:]:
        if max(run + [r]) / min(run + [r]) <= 1.01:
            run.append(r)
        else:
            break
    if len(run) < 2:
        return None
    mid = (min(run) + max(run)) / 2
    return mid if abs(mid - 1) > SPLIT_TOLERANCE else None


# ------------------------------------------------------------------
# 병렬 batch
# ------------------------------------------------------------------
def _run_batch(tickers: list[str], target_iso: str, starts: dict[str, str], end_iso: str,
               workers: int, timeout_s: int) -> tuple[list[tuple], set[str], list[str], list[str]]:
    """반환: (rows, hit, missed, split_tickers). 워커가 예외를 내도 종목 단위로 기록.

    timeout 시 남은 작업은 cancel하고 기다리지 않는다 (shutdown(wait=False)).
    """
    def _one(t: str):
        start = starts.get(t) or end_iso
        known = db.dates_for_ticker(t, start)
        rows = data_source.fetch_ohlcv_by_ticker_via_naver(t, start, end_iso, known_dates=known)
        split = None
        if rows and known:
            split = detect_split(rows, db.closes_for_ticker(t, start))
        return rows, (target_iso in known), split

    rows_all: list[tuple] = []
    hit: set[str] = set()
    split_tickers: list[str] = []
    done_set: set[str] = set()
    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {pool.submit(_one, t): t for t in tickers}
    try:
        for fut in as_completed(futures, timeout=timeout_s):
            t = futures.pop(fut)
            done_set.add(t)
            try:
                rows, had_target, split = fut.result()
            except Exception as e:  # 한 종목 예외가 조용히 사라지지 않게 기록
                log.warning("[incremental] %s fetch 예외: %s", t, e)
                rows, had_target, split = [], False, None
            rows_all.extend(rows)
            if had_target or any(r[1] == target_iso for r in rows):
                hit.add(t)
            if split:
                split_tickers.append(t)
                log.warning("[incremental] %s 분할/비율변경 감지 (최근/DB 종가 비율 %.3f)", t, split)
            if len(done_set) % 500 == 0:
                log.info("[incremental] 진행 %d/%d (target_match=%d, %.0fs)",
                         len(done_set), len(tickers), len(hit), time.monotonic() - t0)
    except _FutTimeout:
        log.warning("[incremental] timeout %ds → %d/%d에서 중단", timeout_s, len(done_set), len(tickers))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    missed = [t for t in tickers if t not in hit]
    return rows_all, hit, missed, split_tickers


def _fetch_starts(tickers: list[str], target: date) -> dict[str, str]:
    """종목별 fetch 시작일: 기본 target-14일, DB 최신일이 그보다 오래됐으면 최신일+1
    (재활성화 종목 공백 방지). REACTIVATE_MAX_DAYS 넘게 비었으면 기본값 (백필 담당)."""
    base_start = target - timedelta(days=WINDOW_DAYS)
    floor = target - timedelta(days=REACTIVATE_MAX_DAYS)
    stats = db.ticker_row_stats(tickers)
    out = {}
    for t in tickers:
        latest = stats.get(t, (0, None))[1]
        start = base_start
        if latest:
            ld = date.fromisoformat(latest)
            if floor <= ld < base_start:
                # 공백을 메우되 DB와 7일 겹치게 — 분할 감지에 겹치는 날짜가 필요 (리뷰 지적)
                start = ld - timedelta(days=7)
        out[t] = start.isoformat()
    return out


def update_specific_date(target_iso: str, force: bool = False,
                         only_tickers: list[str] | None = None) -> dict:
    """지정 날짜 OHLCV fetch + DB 저장.

    반환: {"date", "rows", "empty", "coverage", ["holiday_like" | "source_down"], "splits"}.
    데이터 소스: Yahoo 일봉 → (빈 봉) Nasdaq / Yahoo 분봉 → (Yahoo 실패) Stooq → Nasdaq.
    """
    db.ensure_schema()
    all_tickers = db.get_active_tickers()
    all_tickers.sort(key=lambda t: -(t.get("market_cap") or 0))   # 대형주 먼저
    active_list = [t["ticker"] for t in all_tickers]
    if only_tickers is not None:
        keep = set(only_tickers)
        active_list = [t for t in active_list if t in keep]
    if not active_list:
        return {"date": target_iso, "rows": 0, "empty": True, "coverage": {}}

    target = date.fromisoformat(target_iso)
    cap = _int_env("US_SCREENER_FETCH_CAP", 6000)
    timeout_s = _int_env("US_SCREENER_FETCH_TIMEOUT_S", 900)
    workers = max(1, _int_env("US_SCREENER_FETCH_WORKERS", 6))
    targets = active_list[:cap]
    if len(active_list) > cap:
        log.warning("[incremental] 유니버스 %d > cap %d — %d종목 미수집", len(active_list), cap,
                    len(active_list) - cap)
    starts = _fetch_starts(targets, target)
    data_source.reset_source_stats()
    t0 = time.monotonic()

    # probe — 대형주에 대상일 봉이 하나도 없으면 전 종목 sweep 생략.
    # 부분 재수집(only_tickers)은 이미 다른 종목이 대상일을 가진 상태 → probe 불필요
    # (남은 종목은 저유동주일 가능성이 커 probe로 쓰면 소스 장애 오판).
    if only_tickers is not None:
        probe = []
    else:
        probe = [t for t in PROBE_TICKERS if t in set(targets)] or targets[:5]
    p_rows, p_hit, _, p_splits = (_run_batch(probe, target_iso, starts, target_iso, len(probe), 120)
                                  if probe else ([], set(), [], []))
    if probe and not p_hit:
        keep_rows = [r for r in p_rows if r[0] not in set(p_splits)]
        if keep_rows:
            db.upsert_ohlcv_bulk(keep_rows)
        trading = market_calendar.is_trading_day(target)
        log.warning("[incremental] %s probe %d종목 대상일 봉 0 → sweep 생략 (%s)", target_iso,
                    len(probe), "소스 장애 의심" if trading else "휴장일")
        return {"date": target_iso, "rows": 0, "empty": True,
                "source_down": trading, "holiday_like": not trading,
                "coverage": {"target": len(targets), "hit": 0, "miss": len(targets), "probe_only": True},
                "source_stats": data_source.source_stats()}

    rest = [t for t in targets if t not in set(probe)]
    log.info("[incremental] %s 종목별 batch: %d종목 (workers=%d, timeout=%ds)",
             target_iso, len(targets), workers, timeout_s)
    rows, hit, missed, splits = _run_batch(rest, target_iso, starts, target_iso, workers, timeout_s)
    rows = p_rows + rows
    hit |= p_hit
    splits = p_splits + splits
    missed = [t for t in targets if t not in hit]

    # 누락 종목 1회 재시도 (일시적 429·네트워크 — 조용히 넘기지 않음)
    if missed and len(missed) <= max(50, len(targets) // 5):
        time.sleep(_int_env("US_SCREENER_RETRY_PASS_SLEEP_S", 20))
        r2, h2, _, s2 = _run_batch(missed, target_iso, starts, target_iso,
                                   min(workers, 4), min(timeout_s, 300))
        rows += r2
        hit |= h2
        splits += s2
        log.info("[incremental] 재시도 패스: %d종목 중 %d 회수", len(missed), len(h2))
        missed = [t for t in targets if t not in hit]

    # 무거래일 판정: 마지막 체결일이 대상일 이전 = 그날 거래 없음 (BIO.B·SENEB 같은 저유동
    # 클래스주 실측). 데이터 누락이 아니므로 전일 종가 보합·거래량 0 봉을 기록 — 누락 집계에서
    # 빼고, 신호에도 영향 없음(보합·무거래는 신고가·돌파 불가).
    no_trade: list[str] = []
    fresh_last: dict[str, tuple] = {}
    for r in rows:
        if r[1] < target_iso and (r[0] not in fresh_last or r[1] > fresh_last[r[0]][1]):
            fresh_last[r[0]] = r
    for t in missed[:200]:
        ltd = data_source.last_trade_date(t)
        if ltd and ltd < target_iso:
            # 직전 종가: 이번에 받은 행 우선 (DB가 마지막 체결일을 아직 모를 수 있음 — 리뷰 지적)
            fr = fresh_last.get(t)
            if fr and fr[1] == ltd:
                prev_close = fr[5]
            else:
                prev = db.load_ohlcv(t, days=1)
                prev_close = (prev[-1]["close"] if prev and prev[-1]["date"] == ltd else None)
            if prev_close:
                rows.append((t, target_iso, prev_close, prev_close, prev_close, prev_close, 0, None))
                no_trade.append(t)
    if no_trade:
        hit |= set(no_trade)
        missed = [t for t in missed if t not in set(no_trade)]
        log.info("[incremental] %s 무거래 %d종목 (보합·거래량0 기록): %s", target_iso,
                 len(no_trade), ", ".join(no_trade[:30]))

    coverage = {"target": len(targets), "hit": len(hit), "miss": len(missed),
                "no_trade": len(no_trade), "elapsed_s": round(time.monotonic() - t0, 1)}
    stats = data_source.source_stats()
    log.info("[incremental] %s batch 완료: %s sources=%s", target_iso, coverage, stats)
    if missed:
        log.warning("[incremental] %s 대상일 봉 누락 %d종목 (시총순): %s", target_iso, len(missed),
                    ", ".join(missed[:80]))

    # 분할 감지 종목은 창 구간 행을 저장하지 않고 전체 이력 재구축을 먼저 시도 — 재구축이
    # 실패하면 DB가 여전히 옛 스케일이라 다음 실행에서 다시 감지된다 (한 번 놓치면 영구
    # 혼합 스케일이 되던 문제 — 리뷰 지적).
    split_set = set(splits)
    inserted = db.upsert_ohlcv_bulk([r for r in rows if r[0] not in split_set]) if rows else 0
    log.info("[incremental] %s 저장 rows=%d", target_iso, inserted)
    rebuilt_ok: list[str] = []
    if split_set:
        try:
            from src.us_screener import backfill
            rebuilt = backfill.rebuild_history(sorted(split_set))
            rebuilt_ok = [t for t, n in rebuilt.items() if n]
            log.info("[incremental] 분할 감지 %d종목 이력 재구축: %s", len(split_set), rebuilt)
        except Exception:
            log.exception("[incremental] 분할 이력 재구축 실패 — 다음 실행에서 재감지")
        failed_rebuild = split_set - set(rebuilt_ok)
        if failed_rebuild:
            hit -= failed_rebuild
            coverage["hit"] = len(hit)
            coverage["miss"] = coverage["target"] - len(hit)
    return {"date": target_iso, "rows": inserted, "empty": not hit, "coverage": coverage,
            "splits": sorted(rebuilt_ok), "split_pending": sorted(split_set - set(rebuilt_ok)),
            "source_stats": stats}


def update_today() -> dict:
    """미국 최근 마감 거래일 1일치 fetch + 오래된 데이터 정리.

    반환: {"date", "rows", "is_business_day", "empty", "coverage", ...}.
    """
    db.ensure_schema()
    if not db.get_active_tickers():
        log.info("[incremental] universe 비어있음 → refresh")
        universe.refresh_universe()

    target = us_target_date()
    iso = target.isoformat()
    # 같은 대상일을 이미 대부분 받았으면(같은 날 /screen 재실행·백필 직후) 없는 종목만 수집.
    # 예전엔 95%+면 통째로 건너뛰어 재시도·무거래 처리까지 빠졌다 (리뷰 지적).
    active_n = len(db.get_active_tickers())
    missing = db.active_tickers_missing_date(iso)
    if not missing:
        log.info("[incremental] %s 활성 %d종목 전부 보유 — 수집 생략", iso, active_n)
        return {"date": iso, "rows": 0, "is_business_day": True, "empty": False,
                "coverage": {"target": active_n, "hit": active_n, "miss": 0}}
    subset = missing if len(missing) <= active_n * 0.5 else None
    if subset is not None:
        log.info("[incremental] %s 누락 %d/%d종목만 수집", iso, len(subset), active_n)
    res = update_specific_date(iso, force=True, only_tickers=subset)
    out = {"date": iso, "rows": res.get("rows", 0), "is_business_day": True,
           "empty": bool(res.get("empty")), "coverage": res.get("coverage"),
           "holiday_like": bool(res.get("holiday_like")), "source_down": bool(res.get("source_down")),
           "splits": res.get("splits", []), "source_stats": res.get("source_stats", {})}
    if out["empty"]:
        log.info("[incremental] %s(미국 거래일) 대상일 봉 없음 — %s", iso,
                 "소스 장애 의심" if out["source_down"] else "휴장")
        return out

    cutoff = (target - timedelta(days=RETENTION_DAYS)).isoformat()
    deleted = db.delete_older_than(cutoff)
    if deleted:
        log.info("[incremental] %d행 정리 (cutoff=%s)", deleted, cutoff)
    return out


def ensure_recent_business_day_data() -> dict:
    """가장 최근 마감 거래일까지 데이터 보장 (update_today가 빈 결과일 때 호출).

    대상일이 이미 DB에 충분히(활성 종목 50%+) 있으면 skip. 아니면 대상일 → 직전 거래일 순.
    """
    db.ensure_schema()
    target = us_target_date()
    d = target
    active_n = max(1, len(db.get_active_tickers()))
    for _ in range(5):
        iso = d.isoformat()
        if db.date_row_count(iso) >= active_n * 0.5:
            log.info("[incremental] %s 이미 DB에 있음 (%d행)", iso, db.date_row_count(iso))
            return {"date": iso, "rows": 0, "empty": False,
                    "source": "cached" if d == target else "fallback_cached"}
        res = update_specific_date(iso)
        if not res["empty"]:
            return {**res, "source": "target" if d == target else "fallback"}
        d = market_calendar.prev_trading_day(d)
    log.warning("[incremental] 최근 거래일 5일 모두 fetch 실패 — 누적 DB로 진행")
    return {"date": target.isoformat(), "rows": 0, "empty": True, "source": "none"}
