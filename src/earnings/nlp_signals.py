"""콜 전문 NLP 시그널 — 정규식만, LLM 호출 0회.

세 가지 정량 시그널을 1회 패스로 계산:
1. **숫자 밀도**: $X, X%, X bps, X million/billion 등 정량 토큰 / 1000 단어.
2. **Hedge phrase 카운터**: 자신감 약화 표현 ("subject to", "as of today", "could", "may", ...)
3. **Superlative 카운터**: 최상급 표현 ("record", "unprecedented", "strongest", ...)

이전 분기 대비 변화를 합성 단계에서 cross-check — 숫자 ↓ + hedge ↑ = 자신감 위축 시그널.

전문이 prepared remarks vs Q&A 구분이 보장되지 않아 단순화: 전체 텍스트 1번 분석.
세부 chunked 분석은 향후 확장.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 패턴
# ------------------------------------------------------------------
# 숫자 토큰 — $ 기호·%·bps·million/billion/trillion 단위
_NUMBER_PATTERNS = [
    re.compile(r"\$\d[\d,]*(?:\.\d+)?(?:\s*(?:billion|million|trillion|B|M|T)\b)?", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*%"),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:bps|basis\s+points)\b", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:billion|million|trillion)\b", re.IGNORECASE),
    re.compile(r"\b\d{4,}\b"),  # 4자리 이상 (예: "12,000 units", "50000 GPUs")
]

# Hedge phrases — 분석가가 자신감 약화의 신호로 읽는 표현
_HEDGE_PATTERNS = [
    r"\bsubject to\b",
    r"\bas of today\b",
    r"\bas of now\b",
    r"\bat this point\b",
    r"\bcurrently\b",
    r"\bwe(?:'ll| will) see\b",
    r"\bcould\b",
    r"\bmay (?:see|be|come|need)\b",
    r"\bmight\b",
    r"\bperhaps\b",
    r"\buncertain(?:ty)?\b",
    r"\bvolatil(?:e|ity)\b",
    r"\bbarring\b",
    r"\bassuming\b",
    r"\bif (?:macro|conditions|demand)\b",
    r"\bnot prepared to\b",
    r"\btoo early to\b",
    r"\bdifficult to (?:say|predict|forecast)\b",
    r"\bwe(?:'re| are) (?:monitoring|watching)\b",
]
_HEDGE_RE = re.compile("|".join(_HEDGE_PATTERNS), re.IGNORECASE)

# Superlative phrases — 자신감·기록 갱신 시그널
_SUPERLATIVE_PATTERNS = [
    r"\brecord(?:-breaking)?\b",
    r"\bunprecedented\b",
    r"\bextraordinary\b",
    r"\bstrongest\b",
    r"\bfastest\b",
    r"\blargest\b",
    r"\bbiggest\b",
    r"\bhighest\b",
    r"\bbest (?:ever|in)\b",
    r"\bfirst (?:time|ever)\b",
    r"\bnever (?:before|seen)\b",
    r"\bbreakthrough\b",
    r"\btransformational\b",
    r"\bgenerational\b",
    r"\bgame[- ]changing\b",
    r"\binflection\b",
    r"\baccelerating\b",
    r"\baccelerated\b",
    r"\bdoubl(?:ed|ing)\b",
    r"\btripl(?:ed|ing)\b",
]
_SUPERLATIVE_RE = re.compile("|".join(_SUPERLATIVE_PATTERNS), re.IGNORECASE)


# ------------------------------------------------------------------
# 공개 API
# ------------------------------------------------------------------
def compute_signals(raw_text: str | None, extract: dict | None = None) -> dict[str, Any] | None:
    """raw transcript에서 3개 시그널 계산 + 한 줄 노트.

    raw_text 없으면 None. extract는 미사용 (확장 여지).
    """
    if not raw_text or not isinstance(raw_text, str):
        return None
    text = raw_text.strip()
    if not text:
        return None
    words = re.findall(r"\b\w[\w']*\b", text)
    word_count = max(len(words), 1)

    # 숫자 토큰
    num_total = 0
    for pat in _NUMBER_PATTERNS:
        num_total += len(pat.findall(text))

    # hedge / superlative
    hedge = len(_HEDGE_RE.findall(text))
    superlative = len(_SUPERLATIVE_RE.findall(text))

    per_1000 = lambda n: round(n / word_count * 1000, 2)
    num_density = per_1000(num_total)
    hedge_density = per_1000(hedge)
    sup_density = per_1000(superlative)

    # ratio — superlative가 0이면 hedge가 압도적 → 무한대 대신 hedge 그대로
    if superlative > 0:
        hedge_to_sup = round(hedge / superlative, 2)
    else:
        hedge_to_sup = None

    # 한 줄 노트 — 가장 두드러진 시그널만 콕
    bits: list[str] = []
    if num_density >= 8:
        bits.append("정량 밀도 높음(자신감)")
    elif num_density < 3:
        bits.append("정량 밀도 낮음(추상적)")
    if hedge_density >= 3 and (sup_density < 1.5 or (hedge_to_sup is not None and hedge_to_sup >= 2)):
        bits.append("hedge 우위(방어적)")
    if sup_density >= 2 and hedge_to_sup is not None and hedge_to_sup < 1.0:
        bits.append("superlative 우위(공격적)")
    if not bits:
        bits.append("중립")
    signal_note = ", ".join(bits)

    return {
        "word_count": word_count,
        "numbers_count": num_total,
        "numbers_per_1000": num_density,
        "hedge_count": hedge,
        "hedge_per_1000": hedge_density,
        "superlative_count": superlative,
        "superlative_per_1000": sup_density,
        "hedge_to_superlative_ratio": hedge_to_sup,
        "signal_note": signal_note,
    }


def fmt_signals_text(sig: dict | None) -> str:
    """텔레그램용 짧은 요약."""
    if not sig:
        return ""
    return (
        "🔬 콜 NLP 시그널\n"
        f"  · 정량 밀도: {sig.get('numbers_per_1000', '?')}/1000 단어 "
        f"({sig.get('numbers_count', '?')}회, {sig.get('word_count', '?')} 단어)\n"
        f"  · Hedge 표현: {sig.get('hedge_count', '?')}회 ({sig.get('hedge_per_1000', '?')}/1000)\n"
        f"  · Superlative: {sig.get('superlative_count', '?')}회 ({sig.get('superlative_per_1000', '?')}/1000)\n"
        f"  · Hedge/Sup 비율: {sig.get('hedge_to_superlative_ratio') if sig.get('hedge_to_superlative_ratio') is not None else '—'}\n"
        f"  · 종합: {sig.get('signal_note', '?')}"
    )


def signals_payload_block(ticker: str, sig: dict | None) -> str:
    """synthesis LLM에 넘길 영문 블록."""
    if not sig:
        return ""
    return (
        f"### {ticker} call NLP signals\n"
        f"word_count: {sig.get('word_count')}\n"
        f"numbers_per_1000: {sig.get('numbers_per_1000')} (raw count: {sig.get('numbers_count')})\n"
        f"hedge_per_1000: {sig.get('hedge_per_1000')} (raw count: {sig.get('hedge_count')})\n"
        f"superlative_per_1000: {sig.get('superlative_per_1000')} (raw count: {sig.get('superlative_count')})\n"
        f"hedge_to_superlative_ratio: {sig.get('hedge_to_superlative_ratio')}\n"
        f"signal_note: {sig.get('signal_note')}"
    )
