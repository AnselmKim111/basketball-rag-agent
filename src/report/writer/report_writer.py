"""LLM 리포트 작성 — 데이터/신호/차트 메타 → 한국어 Markdown 내러티브.

summarizer.chat_with_retry 재사용 (synthesis tier). 차트는 코드가 생성하고,
여기선 차트 목록(파일명+설명)을 LLM에 넘겨 본문에 ![](images/..) 삽입하게 한다.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_SYSTEM_PATH = Path(__file__).resolve().parents[3] / "prompts" / "report_system.txt"


def _system_prompt() -> str:
    try:
        return _SYSTEM_PATH.read_text(encoding="utf-8")
    except Exception:
        return "너는 한국어 시황 전략가다. 차트와 데이터로 시장 색깔 변화를 진단하라."


def write_report(
    date_iso: str,
    market_color: dict,
    chart_list: list[dict],
    signals: list[dict],
    macro_summary: dict,
    korea_summary: dict,
    news: list[dict] | None = None,
    theme_momentum: dict | None = None,
    deltas: dict | None = None,
    breadth: dict | None = None,
    highlights: list[dict] | dict | None = None,
    earnings: dict | None = None,
    stale: list[dict] | None = None,
    risk_gauge: dict | None = None,
    fx_gauge: dict | None = None,
    prev_snapshot: dict | None = None,
) -> str:
    """LLM으로 전체 Markdown 리포트 생성 (8섹션 + 전일 대비 팔로업).

    chart_list: [{"filename","title","caption_hint","section"}], signals: MarketSignal dicts,
    macro_summary/korea_summary: 요약 dict, theme_momentum: {buckets,hot,cold},
    deltas: 전일 대비 변화, breadth: 시장 폭, highlights: 개별주 [{label,chg,note}].
    risk_gauge/fx_gauge: §0/§4 게이지 점수 (Daily Brief 박스 명시 fact).
    실패 시 규칙 기반 fallback markdown 반환.
    """
    # highlights는 watchlist 결과 {us:[...], kr:[...]} 또는 legacy list 형태 둘 다 허용
    hl_payload = highlights if isinstance(highlights, dict) else {"legacy": highlights or []}

    # Daily Brief 박스 명시 fact dict — LLM이 정확 수치 인용 보장
    # 매크로 임계 alert (VIX·10Y·DXY·BTC·WTI)
    macro_alerts: dict[str, str] = {}
    m = macro_summary or {}
    vix = m.get("VIX")
    if isinstance(vix, (int, float)):
        if vix < 13:
            macro_alerts["VIX"] = f"VIX {vix:.1f} (낮음 ≤13 · 공포 부재 신호)"
        elif vix > 25:
            macro_alerts["VIX"] = f"VIX {vix:.1f} (높음 ≥25 · 위험회피 신호)"
        elif vix > 18:
            macro_alerts["VIX"] = f"VIX {vix:.1f} (관찰 ≥18 · thesis 압력)"
    ten = m.get("미국 10년물 금리")
    if isinstance(ten, (int, float)):
        if ten > 4.7:
            macro_alerts["10Y"] = f"10Y {ten:.2f}% (높음 ≥4.7 · 주식 부담)"
        elif ten < 3.8:
            macro_alerts["10Y"] = f"10Y {ten:.2f}% (낮음 ≤3.8 · 주식 우호)"
    dxy = m.get("DXY")
    if isinstance(dxy, (int, float)):
        if dxy > 105:
            macro_alerts["DXY"] = f"DXY {dxy:.1f} (강달러 ≥105 · 환전 압력 강화)"
        elif dxy < 95:
            macro_alerts["DXY"] = f"DXY {dxy:.1f} (약달러 ≤95 · 신흥국 우호)"
    wti = m.get("WTI 유가")
    if isinstance(wti, (int, float)):
        if wti > 90:
            macro_alerts["WTI"] = f"WTI ${wti:.0f} (높음 ≥$90 · 인플레 압력)"
        elif wti < 65:
            macro_alerts["WTI"] = f"WTI ${wti:.0f} (낮음 ≤$65 · 청정에너지 압력)"
    krw = m.get("USD/KRW")
    if isinstance(krw, (int, float)):
        if krw > 1500:
            macro_alerts["USDKRW"] = f"USD/KRW {krw:.0f}원 (높음 ≥1500 · 환전 압력 강화)"

    # 데이터 freshness — stale 키 list
    data_freshness = {
        "stale_keys": [s.get("key") for s in (stale or []) if s.get("key")],
        "stale_count": len(stale or []),
    }

    brief_facts = {
        "risk_gauge": {
            "score": (risk_gauge or {}).get("score"),
            "label": (risk_gauge or {}).get("label"),
            "prev_score": (prev_snapshot or {}).get("gauge_score"),
            "prev_label": (prev_snapshot or {}).get("gauge_label"),
        },
        "fx_pressure": {
            "score": (fx_gauge or {}).get("score"),
            "label": (fx_gauge or {}).get("label"),
            "components": (fx_gauge or {}).get("components"),
            "prev_score": (prev_snapshot or {}).get("fx_pressure_score"),
            "prev_label": (prev_snapshot or {}).get("fx_pressure_label"),
        },
        "theme_hot": (theme_momentum or {}).get("hot", [])[:5],
        "korea_streaks": {
            mkt: {k: v for k, v in (((korea_summary or {}).get(mkt) or {}).items())
                  if k.endswith("_streak")}
            for mkt in ("KOSPI", "KOSDAQ")
        },
        "macro_alerts": macro_alerts,
        "data_freshness": data_freshness,
    }

    # Top Surprises Today — 오늘 가장 의외적 fact 5개 자동 추출
    surprises: list[str] = []
    try:
        # 1. 어제 약화 → 오늘 가장 강한 반전 (highlights_meta 비교)
        prev_meta = (prev_snapshot or {}).get("highlights_meta") or []
        prev_chg_map = {it.get("ticker"): it.get("chg_5d") for it in prev_meta if it.get("ticker")}
        today_items = []
        if isinstance(highlights, dict):
            today_items = list(highlights.get("us") or []) + list(highlights.get("kr") or [])
        max_reversal = None
        max_reversal_lapse = None
        for it in today_items:
            tkr = it.get("ticker")
            chg_t = it.get("chg_5d")
            chg_p = prev_chg_map.get(tkr)
            if isinstance(chg_t, (int, float)) and isinstance(chg_p, (int, float)):
                delta = chg_t - float(chg_p)
                lbl = (it.get("label") or tkr).split("(")[0].strip()
                if delta > 0 and (max_reversal is None or delta > max_reversal[1]):
                    max_reversal = (lbl, delta, chg_p, chg_t)
                if delta < 0 and (max_reversal_lapse is None or delta < max_reversal_lapse[1]):
                    max_reversal_lapse = (lbl, delta, chg_p, chg_t)
        if max_reversal:
            lbl, d, p, t = max_reversal
            surprises.append(f"📈 최대 반전: {lbl} (어제 {p:+.1f}% → 오늘 {t:+.1f}%, Δ+{d:.1f})")
        if max_reversal_lapse:
            lbl, d, p, t = max_reversal_lapse
            surprises.append(f"📉 최대 lapse: {lbl} (어제 {p:+.1f}% → 오늘 {t:+.1f}%, Δ{d:.1f})")

        # 2. 가장 큰 외국인 streak (절대값 max)
        max_streak = None
        for mkt in ("KOSPI", "KOSDAQ"):
            for inv in ("외국인", "기관", "개인"):
                s = ((korea_summary or {}).get(mkt) or {}).get(f"{inv}_streak")
                if isinstance(s, (int, float)) and (max_streak is None or abs(s) > abs(max_streak[2])):
                    max_streak = (mkt, inv, int(s))
        if max_streak and abs(max_streak[2]) >= 5:
            mkt, inv, s = max_streak
            direction = "매도" if s < 0 else "매수"
            surprises.append(f"⚠ 최대 streak: {mkt} {inv} {abs(s)}거래일 연속 {direction}")

        # 3. earnings 최대 surprise (절대값)
        recent_e = (earnings or {}).get("recent") or {}
        all_e = (recent_e.get("top_beats") or []) + (recent_e.get("top_misses") or [])
        if all_e:
            top_surprise = max(all_e, key=lambda x: abs(x.get("surprise_pct") or 0))
            sym = top_surprise.get("symbol")
            sp = top_surprise.get("surprise_pct")
            if sym and isinstance(sp, (int, float)) and abs(sp) >= 10:
                tag = "beat" if sp > 0 else "miss"
                surprises.append(f"💥 최대 어닝 surprise: {sym} EPS {sp:+.1f}% {tag}")

        # 4. 게이지 최대 변화 (Risk vs FX 중 큰 변화)
        r_diff = None; f_diff = None
        if brief_facts["risk_gauge"]["score"] is not None and brief_facts["risk_gauge"]["prev_score"] is not None:
            r_diff = brief_facts["risk_gauge"]["score"] - brief_facts["risk_gauge"]["prev_score"]
        if brief_facts["fx_pressure"]["score"] is not None and brief_facts["fx_pressure"]["prev_score"] is not None:
            f_diff = brief_facts["fx_pressure"]["score"] - brief_facts["fx_pressure"]["prev_score"]
        max_gauge_diff = None
        if r_diff is not None and abs(r_diff) >= 5:
            max_gauge_diff = ("Risk", r_diff)
        if f_diff is not None and abs(f_diff) >= 5 and (max_gauge_diff is None or abs(f_diff) > abs(max_gauge_diff[1])):
            max_gauge_diff = ("FX 압력", f_diff)
        if max_gauge_diff:
            name, d = max_gauge_diff
            arrow = "↑" if d > 0 else "↓"
            surprises.append(f"🎯 {name} 게이지 {arrow} {abs(d):.0f}점 변화")

        # 5. theme_hot의 최대 5D 강도 테마
        hot = (theme_momentum or {}).get("hot") or []
        if hot:
            surprises.append(f"🔥 hot 테마 #1: {hot[0]}")
    except Exception:
        pass
    brief_facts["top_surprises"] = surprises[:6]

    payload = {
        "date": date_iso,
        "market_color": market_color,
        "deltas_vs_yesterday": deltas or {},
        "breadth": breadth or {},
        "theme_momentum": theme_momentum or {},
        "charts": chart_list,
        "signals": signals[:60],
        "macro": macro_summary,
        "korea": korea_summary,
        "highlights": hl_payload,
        "news": (news or [])[:15],
        "earnings": earnings or {},
        "stale_data": stale or [],
        "brief_facts": brief_facts,
    }
    user_msg = (
        "다음은 오늘 시장 데이터·전일 대비 변화(deltas_vs_yesterday)·생성된 차트 목록이다. "
        "시스템 규칙(버터대디 캐주얼 1인칭 문체 + 신호 인사이트 톤)에 따라 한국어 Markdown "
        "시황 리포트를 작성하라.\n\n"
        "**필수 구조 (어기면 실패):**\n"
        "1. 제목 직후 **📡 오늘 시장이 말하는 한 줄 (THESIS BOX)** 4줄 인용블록: 관찰·핵심신호·변하면의미바뀌는것·호흡. "
        "(베팅·진입·손절·확신도 별점 표현 일체 금지 — 액션은 사용자가 한다)\n"
        "2. **📍 어제 대비 변화**, **Executive Summary**(첫 줄 인과 사슬 4-5단).\n"
        "3. §0~§8 매 섹션은 **관찰 → 해석 → 시장 신호의 의미 → 반박(해당시) → • takeaway(thesis 관계)** 4부 스키마.\n"
        "4. 모든 차트는 반드시 `![](images/파일명)`으로 본문 1회 삽입.\n"
        "5. **마지막에 🌅 내일 체크포인트 표 3행** (무엇·시간·왜 중요·진행 신호).\n"
        "6. 마지막 문장은 '지수는 움직였고, 이제 돈은 어디로 가고 있는가?'에 답.\n\n"
        "**중요 톤 룰**: 봇은 데이터·신호·맥락만 정확하고 풍부하게 전달. '진입/손절/목표가/베팅/추천/확신도 별점' "
        "어휘 일체 금지. 신호의 의미·강도·다른 신호와의 일치도를 인사이트있게 포착하는 데 집중. "
        "기술 레벨(MA·52w·support·resistance)은 fact 형태로만 인용('현재 52w 99%, 200MA $122 위') — "
        "'$146 돌파 시 매수' 같은 트리거 표현 금지. 종목별 한 줄 takeaway는 (thesis 일치 / 강화 / 약화 / 독립 모멘텀 / 노이즈) 중 하나로 분류.\n\n"
        "**§6 개별 종목 watchlist**: highlights = {us:[…], kr:[…]} 각 종목은 enriched dict — "
        "categories(earnings_actual/estimate_revision/ipo/sector_leader/trend_reversal/followup), "
        "earnings_actual(어닝 실제 결과 — eps_surprise_pct·rev_surprise_pct·source_consensus), "
        "estimate_revision(분석가 기대치 변화 — buy_count_old→new·buy_delta — 어닝과 별개 신호), "
        "ipo(상장 fact + 자금흐름 함의), screener_signals(기술 신호 종류), technical(MA·52w·support/resistance), "
        "news_snippets(매체명만 인용, URL 금지), sector_context(섹터 모멘텀과 일치/대비). "
        "**어닝 실제(earnings_actual)와 분석가 기대치(estimate_revision)는 다른 의미** — 전자는 사실 발표 후 reaction, "
        "후자는 발표 없이 시장 컨센서스 shift. 둘 다 있으면 결과→기대 사슬, 한쪽만이면 그 해석만. "
        "카테고리별 mini-section(📈 어닝 / 🔭 기대 / 💎 IPO / 🚀 리더 / 🔄 반전 / 🔁 F/U)으로 구조화. "
        "카테고리 0개면 그 mini-section 자체 생략.\n\n"
        "**일반 룰**: news는 내러티브에 자연스럽게 녹이되 **출처 URL 절대 금지**, 매체/주체 이름만. "
        "입력 news에 있는 사실만 쓰고 없는 수치·사실은 만들지 마라. "
        "stale_data 카테고리는 '⚠ 전일자 데이터(asof)'로 명시. 데이터 미확보는 '데이터 미수집'으로.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    try:
        from openai import OpenAI
        from src import summarizer
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
        )
        model = os.getenv("REPORT_SYNTHESIS_MODEL") or os.getenv("IDEA_SYNTHESIS_MODEL") or "anthropic/claude-sonnet-4.5"
        max_tok = int(os.getenv("REPORT_MAX_TOKENS") or "24000")
        md = summarizer.chat_with_retry(
            client,
            messages=[{"role": "system", "content": _system_prompt()},
                      {"role": "user", "content": user_msg}],
            max_tokens=max_tok,
            model=model,
            temperature=0.4,
            context="report_writer",
        )
        if md and len(md.strip()) > 200:
            return _ensure_all_charts(md.strip(), chart_list)
        log.warning("[report.writer] LLM 짧은 응답 → fallback")
    except Exception:
        log.exception("[report.writer] LLM 실패 → fallback")
    return _ensure_all_charts(_fallback_markdown(date_iso, market_color, chart_list, signals, deltas),
                              chart_list)


def _ensure_all_charts(md: str, chart_list: list[dict]) -> str:
    """LLM이 일부 차트를 빠뜨리거나 없는 파일명을 적어도, 모든 차트가 제 섹션 맥락에
    정확히 1회씩 들어가도록 보정. PDF 단일 파일에 전 차트가 빠짐없이 박히게 하는 핵심.

    1) 전체를 감싼 코드펜스 제거
    2) chart_list에 없는(환각) 이미지 참조 제거
    3) 본문에 안 들어간 차트를 그 section 헤딩 아래(없으면 말미 부록)에 삽입
    """
    import re
    if not chart_list:
        return md
    s = md.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s).strip()
    md = s

    valid = {c["filename"] for c in chart_list}

    # 2) 환각 이미지 참조 제거 (filename이 chart_list에 없으면 삭제)
    def _drop(m):
        return m.group(0) if m.group(1) in valid else ""
    md = re.sub(r"!\[[^\]]*\]\(images/([^)\s]+)\)", _drop, md)

    referenced = {f for f in re.findall(r"images/([^)\s]+)", md) if f in valid}
    missing = [c for c in chart_list if c["filename"] not in referenced]
    if not missing:
        return md

    lines = md.split("\n")

    def _section_key(sec: str) -> str:
        return re.sub(r"^\s*\d+\.?\s*", "", sec or "").strip()

    from collections import OrderedDict
    by_sec: "OrderedDict[str, list]" = OrderedDict()
    for c in missing:
        by_sec.setdefault(c.get("section", ""), []).append(c)

    appendix: list[dict] = []
    for sec, charts in by_sec.items():
        key = _section_key(sec)
        tokens = [t for t in key.split() if len(t) >= 2]
        target = None
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("#") and ((key and key in ln) or (tokens and tokens[0] in ln)):
                target = i
                break
        if target is None:
            appendix.extend(charts)
            continue
        block: list[str] = []
        for c in charts:
            block.append(f"\n![](images/{c['filename']})")
            if c.get("caption_hint"):
                block.append(f"*{c['caption_hint']}*")
        lines[target + 1:target + 1] = block
    md = "\n".join(lines)

    if appendix:
        md += "\n\n## 📎 추가 차트\n"
        for c in appendix:
            md += f"\n![](images/{c['filename']})\n"
            if c.get("caption_hint"):
                md += f"*{c['caption_hint']}*\n"
    return md


def _fallback_markdown(date_iso, market_color, chart_list, signals, deltas=None) -> str:
    """LLM 실패 시 차트+신호만으로 최소 리포트."""
    lines = [
        f"# [Daily Macro] {market_color.get('market_color','Mixed')} ({date_iso})",
        f"\n**오늘의 시장 색깔: {market_color.get('market_color','Mixed')}** "
        f"({', '.join(market_color.get('evidence', []))})\n",
    ]
    if deltas and deltas.get("notes"):
        lines.append("\n## 📍 어제 대비 변화")
        for note in deltas["notes"]:
            lines.append(f"- {note}")
    for ch in chart_list:
        lines.append(f"\n## {ch.get('title','')}")
        lines.append(f"![]({'images/' + ch['filename']})")
        if ch.get("caption_hint"):
            lines.append(f"\n{ch['caption_hint']}")
    if signals:
        lines.append("\n## 탐지된 신호")
        for s in signals[:30]:
            lines.append(f"- {s['asset']}: {s['signal_type']} — {s.get('comment','')}")
    lines.append("\n_지수는 움직였고, 이제 돈은 어디로 가고 있는가?_")
    return "\n".join(lines)
