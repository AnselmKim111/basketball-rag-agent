"""일간 스냅샷 영속 + 전일 대비 변화(delta) 계산 — "매일 변화 팔로업"의 핵심.

매일 리포트 종료 시 구조화 스냅샷을 저장하고, 다음 날 직전 스냅샷을 로드해
무엇이 바뀌었는지(시장 색깔 전환, RSP 신고가 상태, 리더십 진입/이탈, 수급 반전 등)를 계산.

저장 위치: /data/report_state (Railway 볼륨) → 없으면 reports/_state 폴백.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _state_dir() -> Path:
    for cand in ("/data/report_state", "reports/_state"):
        p = Path(cand)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    p = Path("reports/_state")
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_snapshot(date_iso: str, snapshot: dict) -> str | None:
    try:
        path = _state_dir() / f"{date_iso}.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("[report.state] 스냅샷 저장: %s", path)
        return str(path)
    except Exception:
        log.exception("[report.state] 스냅샷 저장 실패")
        return None


def load_previous(date_iso: str) -> dict | None:
    """date_iso 이전의 가장 최근 스냅샷 로드. 없으면 None."""
    d = _state_dir()
    try:
        files = sorted(p.stem for p in d.glob("*.json"))
    except Exception:
        return None
    prev = [f for f in files if f < date_iso]
    if not prev:
        return None
    try:
        return json.loads((d / f"{prev[-1]}.json").read_text(encoding="utf-8"))
    except Exception:
        log.exception("[report.state] 전일 스냅샷 로드 실패")
        return None


def build_snapshot(date_iso: str, market_color: dict, theme_summary: dict,
                   theme_rows: list[dict], macro_summary: dict, breadth: dict,
                   korea_summary: dict, rsp_new_high: bool,
                   highlights_meta: list[dict] | None = None) -> dict:
    """스냅샷 dict 생성 (JSON 직렬화 가능 값만).

    highlights_meta: watchlist에서 산출된 종목 메타 (다음날 F/U stream용).
    """
    leaders = [r["label"] for r in theme_rows
               if r.get("bucket") in ("주도지속", "새로 강해지는")]
    laggards = [r["label"] for r in theme_rows if r.get("bucket") == "소외·빈집"]
    theme_r5d = {r["label"]: r.get("r5d") for r in theme_rows}
    return {
        "date": date_iso,
        "market_color": market_color.get("market_color"),
        "rsp_new_high": bool(rsp_new_high),
        "leaders": leaders,
        "laggards": laggards,
        "hot": theme_summary.get("hot", []),
        "cold": theme_summary.get("cold", []),
        "theme_r5d": theme_r5d,
        "macro": macro_summary,
        "breadth": breadth,
        "korea": korea_summary,
        "highlights_meta": highlights_meta or [],
    }


def compute_deltas(today: dict, prev: dict | None) -> dict:
    """전일 대비 변화 계산. prev 없으면 baseline 표시."""
    if not prev:
        return {"baseline": True,
                "notes": ["직전 스냅샷 없음 — 오늘을 기준선(baseline)으로 설정."]}
    notes: list[str] = []

    # 1) 시장 색깔 전환
    if today.get("market_color") != prev.get("market_color"):
        notes.append(f"시장 색깔 전환: {prev.get('market_color')} → {today.get('market_color')}")

    # 2) RSP 신고가 상태 변화
    if today.get("rsp_new_high") and not prev.get("rsp_new_high"):
        notes.append("RSP(동일가중) 신규 신고가 진입 — 폭 확산(쏠림 둔화) 신호")
    elif not today.get("rsp_new_high") and prev.get("rsp_new_high"):
        notes.append("RSP 신고가 상태 이탈")

    # 3) 리더십 진입/이탈 테마
    t_lead = set(today.get("leaders", [])); p_lead = set(prev.get("leaders", []))
    entered = sorted(t_lead - p_lead); exited = sorted(p_lead - t_lead)
    if entered:
        notes.append("리더십 신규 진입: " + ", ".join(entered[:6]))
    if exited:
        notes.append("리더십 이탈: " + ", ".join(exited[:6]))

    # 4) 빈집 → 반등 (어제 소외, 오늘 5D 플러스)
    p_lag = set(prev.get("laggards", []))
    t_r5d = today.get("theme_r5d", {})
    revived = sorted([l for l in p_lag if (t_r5d.get(l) or 0) > 1.0])
    if revived:
        notes.append("소외→반등 조짐: " + ", ".join(revived[:6]))

    # 5) 주요 지표 Δ
    deltas_num = {}
    for k in ("VIX", "미국 10년물 금리", "WTI 유가", "USD/KRW"):
        tv = (today.get("macro") or {}).get(k)
        pv = (prev.get("macro") or {}).get(k)
        if isinstance(tv, (int, float)) and isinstance(pv, (int, float)) and pv:
            deltas_num[k] = round(tv - pv, 2)

    # 6) 한국 수급 방향 반전
    for mkt in ("KOSPI", "KOSDAQ"):
        for inv in ("외국인", "기관", "개인"):
            tv = ((today.get("korea") or {}).get(mkt) or {}).get(inv)
            pv = ((prev.get("korea") or {}).get(mkt) or {}).get(inv)
            if isinstance(tv, (int, float)) and isinstance(pv, (int, float)):
                if (tv >= 0) != (pv >= 0):
                    notes.append(f"한국 {mkt} {inv} 수급 방향 반전 ({'매수' if tv >= 0 else '매도'}로)")

    return {"baseline": False, "prev_date": prev.get("date"),
            "notes": notes or ["전일 대비 큰 구조 변화 없음 — 추세 유지."],
            "macro_delta": deltas_num}
