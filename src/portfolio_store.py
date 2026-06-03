"""사용자별 portfolio store — JSON 파일 (per chat_id) 기반.

저장 경로: `/data/portfolio/<chat_id>.json` (state_store._state_dir 활용).

API:
  - load_positions(chat_id) -> list[dict] (빈 list 가능)
  - save_positions(chat_id, positions)
  - add_position(chat_id, ticker, market, buy_price, shares) -> dict | None
  - remove_position(chat_id, ticker) -> bool
  - clear_positions(chat_id) -> bool

각 position dict: {ticker, market, buy_price, shares, added_at}
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK = threading.RLock()


def _portfolio_dir() -> Path:
    from src.state_store import _state_dir
    d = _state_dir() / "portfolio"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        log.exception("[portfolio_store] dir create 실패")
    return d


def _path(chat_id) -> Path:
    return _portfolio_dir() / f"{str(chat_id)}.json"


def load_positions(chat_id) -> list[dict]:
    p = _path(chat_id)
    if not p.exists():
        return []
    try:
        with _LOCK:
            data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except Exception:
        log.exception("[portfolio_store] load %s 실패", chat_id)
        return []


def save_positions(chat_id, positions: list[dict]) -> bool:
    p = _path(chat_id)
    try:
        with _LOCK:
            p.write_text(json.dumps(positions, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        return True
    except Exception:
        log.exception("[portfolio_store] save %s 실패", chat_id)
        return False


def add_position(chat_id, ticker: str, market: str,
                 buy_price: float, shares: float) -> dict | None:
    """등록. 같은 ticker 이미 있으면 평단·수량 갱신 (avg)."""
    tkr = (ticker or "").strip().upper()
    mkt = (market or "").strip().upper()
    if not tkr or mkt not in ("US", "KR"):
        return None
    try:
        bp = float(buy_price)
        sh = float(shares)
    except (ValueError, TypeError):
        return None
    if bp <= 0 or sh <= 0:
        return None
    positions = load_positions(chat_id)
    # 기존 ticker 있으면 평단 가중 평균
    for pos in positions:
        if pos.get("ticker") == tkr and pos.get("market") == mkt:
            old_bp = float(pos.get("buy_price") or 0)
            old_sh = float(pos.get("shares") or 0)
            total_cost = old_bp * old_sh + bp * sh
            total_sh = old_sh + sh
            if total_sh > 0:
                pos["buy_price"] = round(total_cost / total_sh, 4)
                pos["shares"] = total_sh
                pos["updated_at"] = date.today().isoformat()
                save_positions(chat_id, positions)
                return pos
    new_pos = {
        "ticker": tkr,
        "market": mkt,
        "buy_price": bp,
        "shares": sh,
        "added_at": date.today().isoformat(),
    }
    positions.append(new_pos)
    save_positions(chat_id, positions)
    return new_pos


def remove_position(chat_id, ticker: str) -> bool:
    tkr = (ticker or "").strip().upper()
    if not tkr:
        return False
    positions = load_positions(chat_id)
    before = len(positions)
    positions = [p for p in positions if p.get("ticker") != tkr]
    if len(positions) < before:
        save_positions(chat_id, positions)
        return True
    return False


def clear_positions(chat_id) -> bool:
    p = _path(chat_id)
    if not p.exists():
        return False
    try:
        with _LOCK:
            p.unlink()
        return True
    except Exception:
        log.exception("[portfolio_store] clear %s 실패", chat_id)
        return False
