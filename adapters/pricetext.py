"""가격 문자열에서 (값, 단위, 자료형)을 읽는다.

기존 `parse_price()`는 숫자 하나만 돌려줘서 `$100 / hour`와 `$100 / 1M tokens`를
구분하지 못했다. 저장 범위를 "과금되는 항목 전부"로 넓히면서(2026-07-27 방침 전환)
단위가 값에 붙어 다녀야 하므로 이 모듈을 새로 둔다.

읽는 방법: 금액을 기준으로 문자열을 토막 내고, 각 금액의 **뒤쪽 꼬리**에서
단위와 자료형을 읽는다. 괄호를 특별 취급하지 않아도 실측한 형태가 전부 처리된다.

    '$2.00 (text) $12.00 (audio)'   -> 두 값, 각각 자료형 text·audio
    '$0.45 ($0.00012 per image)'    -> 두 값, 뒤엣것은 장당 환산
    '$0.05 / min ( $3.00 / hr)'     -> 분당·시간당 둘 다
    '$0.01 / sec $0.002 / img'      -> 초당·장당
    '$6 / $10 / $14'                -> 값 셋(축은 회사별 수집 코드가 표 헤더를 보고 붙임)

실측 근거: 2026-07-27 저장본 6개 페이지 전수. 금액이 든 셀 303종 중 79종이
한 칸에 금액을 둘 이상 담고 있었다.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional

# 값 하나를 못 읽었을 때 쓰는 표시. 조용히 버리지 않는다
UNIT_UNKNOWN = "unknown"
DEFAULT_UNIT = "per_1M_tokens"

# 자료형. 복수형·공백·슬래시·and 로 이어진 목록을 전부 받는다
_MODALITY_WORDS = {
    "text": "text", "texts": "text",
    "audio": "audio", "speech": "audio",
    "image": "image", "images": "image",
    "video": "video", "videos": "video",
    "thinking": "thinking", "reasoning": "thinking",
}

# 단위를 이루는 조각. (정규식, 이름, 수량을 앞에 받는가)
_UNIT_TOKENS = [
    (r"mtoks?\b", "1M_tokens", False),       # MTok 자체가 100만 토큰이라 수량이 안 붙는다
    (r"tokens?\b", "tokens", True),
    (r"chars?\b|characters?\b", "characters", True),
    (r"imgs?\b|images?\b", "image", True),
    (r"frames?\b", "frame", True),
    (r"videos?\b|clips?\b", "video", True),
    (r"secs?\b|seconds?\b", "second", True),
    (r"mins?\b|minutes?\b", "minute", True),
    (r"hrs?\b|hours?\b", "hour", True),
    (r"days?\b", "day", True),
    (r"months?\b", "month", True),
    (r"requests?\b|queries\b|query\b", "request", True),
    (r"calls?\b|invocations?\b", "call", True),
    (r"sessions?\b", "session", True),
    (r"songs?\b", "song", True),
    (r"gib\b", "GiB", True),
    (r"gb\b", "GB", True),
    (r"ccu\b", "CCU", True),
    (r"users?\b", "user", True),
    (r"pages?\b", "page", True),
]

_AMOUNT = re.compile(r"\$\s*([0-9]+(?:[.,][0-9]+)*)")
# 수량 접두: 1,000,000 / 1M / 1K / 1000
_QTY = re.compile(r"(?:^|[^0-9])([0-9][0-9,\.]*\s*[MK]?)\s*$", re.I)
_SKIP_TEXT = {"", "-", "—", "n/a", "not available", "free of charge", "free",
              "included", "no charge", "not applicable"}


@dataclass
class PriceValue:
    """한 조건에서의 값 하나."""
    value: float
    unit: str                       # per_1M_tokens · per_image · per_GB_per_day 등
    modality: Optional[str] = None  # text / audio / image / video / thinking
    note: str = ""                  # 원문 조각. 값이 이상해 보일 때 되짚는 근거

    def key(self):
        return (self.unit, self.modality)


def _norm_qty(raw):
    """단위 앞 수량을 정규화. '1,000,000' -> 1000000, '1M' -> 1000000."""
    t = raw.strip().replace(",", "").replace(" ", "").upper()
    mult = 1
    if t.endswith("M"):
        mult, t = 1_000_000, t[:-1]
    elif t.endswith("K"):
        mult, t = 1_000, t[:-1]
    if not t:
        return mult
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


def _qty_label(n, word):
    """수량 + 단어를 단위 이름으로. 1개면 수량을 붙이지 않는다."""
    if n is None or n == 1:
        return word
    if n == 1_000_000:
        return f"1M_{word}"
    if n == 1_000:
        return f"1K_{word}"
    return f"{n}_{word}"


def _parse_unit(tail):
    """꼬리 문자열에서 단위 이름을 만든다. 못 찾으면 None.

    '1M tokens per hour' 처럼 조각이 겹치면 순서대로 이어 붙인다
    -> per_1M_tokens_per_hour (캐시 보관료). 'GB per day' -> per_GB_per_day.

    괄호로 온전히 닫힌 부분은 보지 않는다. 그 안은 자료형 묶음이기 때문이다
    ('(text/image/video)'의 슬래시를 단위 구분자로 오인하던 것을 막는다).
    괄호 안에 금액이 있는 형태('$0.45 ($0.00012 per image)')는 그 금액이 따로
    잡히고 꼬리가 ' per image)'가 되므로 여기서 걸러지지 않는다.

    단위 뒤에 설명 문장이 이어지면 거기서 끊는다. 안 끊으면 뒤쪽 단어까지
    단위로 끌어온다(실측: '$0.10 / GB-day after 1 GB free per account per month'
    -> per_GB_per_day_per_GB_per_month). 소수점은 뒤에 공백이 없으므로
    '0.5K image' 같은 표기는 잘리지 않는다.
    """
    tail = re.split(r"\.\s|[;+]|\bafter\b|\bplus\b|\bbilled\b|\bincluding\b",
                    tail, maxsplit=1)[0]
    parts = []
    pos = 0
    low = re.sub(r"\([^)]*\)", " ", tail).lower()
    while pos < len(low):
        best = None
        for pat, word, takes_qty in _UNIT_TOKENS:
            m = re.compile(pat, re.I).search(low, pos)
            if m and (best is None or m.start() < best[0].start()):
                best = (m, word, takes_qty)
        if best is None:
            break
        m, word, takes_qty = best
        # 단위 단어 앞에 'per' 나 '/' 가 있어야 단위로 인정한다.
        # (괄호 안에 단독으로 있는 'image'는 단위가 아니라 자료형)
        before = low[pos:m.start()]
        if not re.search(r"(per|/|each)\s*[0-9,\.\sMKmk]*$", before) and not parts:
            pos = m.end()
            continue
        qty = None
        if takes_qty:
            q = _QTY.search(before)
            if q:
                qty = _norm_qty(q.group(1))
        parts.append(_qty_label(qty, word))
        pos = m.end()
    if not parts:
        return None
    return "per_" + "_per_".join(parts)


def _parse_modalities(tail):
    """꼬리에서 자료형 목록. 괄호 안의 자료형 단어만 본다. 없으면 빈 목록."""
    out = []
    for grp in re.findall(r"\(([^)]*)\)", tail):
        # 'per'나 숫자가 섞이면 단위·환산·할인 표기라 자료형 묶음이 아니다.
        # 슬래시만으로 판정하면 '(text/image/video)'가 걸러져서 안 된다
        if re.search(r"\bper\b|\d", grp):
            continue
        for w in re.split(r"[/,]|\band\b|\s+", grp.lower()):
            w = w.strip()
            if w in _MODALITY_WORDS:
                m = _MODALITY_WORDS[w]
                if m not in out:
                    out.append(m)
    return out


def parse_prices(text, default_unit=DEFAULT_UNIT, default_modality=None):
    """문자열에서 값 목록을 뽑는다. 값이 없으면 빈 목록.

    default_unit: 문자열에 단위 표기가 없을 때 쓸 단위. 표의 성격을 아는
                  회사별 수집 코드가 넘겨준다(예: 토큰 단가 표면 per_1M_tokens).
    default_modality: 마찬가지로 표에서 이미 아는 자료형.

    자료형이 여러 개면 값을 그만큼 펼친다(2026-07-27 결정).
    '$0.05 (text/image/video)' -> 세 값
    """
    if text is None:
        return []
    raw = str(text).strip()
    if raw.lower() in _SKIP_TEXT:
        return []

    hits = list(_AMOUNT.finditer(raw))
    if not hits:
        return []

    out = []
    for i, m in enumerate(hits):
        num = m.group(1).replace(",", "")
        # '1,234.56' 은 쉼표가 자리 구분, '1,25' 같은 소수 쉼표는 안 쓰는 페이지들이라
        # 쉼표 제거 후 그대로 읽는다
        try:
            value = float(num)
        except ValueError:
            continue
        tail_end = hits[i + 1].start() if i + 1 < len(hits) else len(raw)
        tail = raw[m.end():tail_end]

        unit = _parse_unit(tail) or default_unit
        mods = _parse_modalities(tail) or ([default_modality] if default_modality else [None])
        note = raw[m.start():tail_end].strip()
        for mod in mods:
            out.append(PriceValue(value=value, unit=unit, modality=mod, note=note))
    return out


def parse_one(text, default_unit=DEFAULT_UNIT):
    """값이 하나뿐일 것으로 아는 자리에서 쓰는 편의 함수.

    값이 둘 이상이면 첫 값을 돌려주되 호출부가 알 수 있도록 개수도 함께 준다.
    반환: (PriceValue 또는 None, 찾은 개수)
    """
    vals = parse_prices(text, default_unit=default_unit)
    return (vals[0] if vals else None), len(vals)
