"""Google Gemini(AI Studio) 공식 마크다운(.md.txt) 가격 페이지 수집 코드.

명세 = 수집기준_결정항목.md Google 절.
전치 구조: 모델 = h2 절, 등급 = h3 소절, 행 = 항목, 값 열 = Paid Tier.
단위는 표 머리('Paid Tier, per 1M tokens in USD')에 있다. 한 칸에 값이
여럿인 칸 131개(문맥 27·시기 24·보관료 44·해상도 4·자료형 등)를 가른다.
2026-07-25: Googlebot 사칭 UA 제거. 기본 브라우저 UA로도 200 OK 확인.
"""
import re
from .base import BaseAdapter, PriceRow


class GeminiAdapter(BaseAdapter):
    provider = "Google"
    url = "https://ai.google.dev/gemini-api/docs/pricing"

from . import mdtable as _md
from .pricetext import parse_prices as _gpp

_MD_URL = "https://ai.google.dev/gemini-api/docs/pricing.md.txt"
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF]")
_FOOT = re.compile(r"\^[^^ ]*\^")            # 각주 표시 ^*^ 제거
_CTX_PAIR = re.compile(                       # '$2.00, prompts <= 200k tokens'
    r"\$\s*([\d.,]+)\s*,?\s*prompts\s*(<=|>)\s*[\d,]+k?\s*tokens?", re.I)
# [수정 2026-08-15] 뒤에 붙는 "(storage price)" 까지 삼킨다. 남겨 두면 그 괄호가 자료형 규칙에
# 걸려 modality 로 들어간다
_STORAGE = re.compile(
    r"\$\s*([\d.,]+)\s*/\s*1,000,000 tokens per hour(?:\s*\(storage price\))?", re.I)
# '$0.75 (text)' · '$3.00 or $0.005/min (audio)' — 토큰당 값 + 선택적 분당 병기
_QUAL = re.compile(
    r"\$\s*([\d.,]+)(?:\s*or\s*\$\s*([\d.,]+)\s*/\s*min)?\s*\(([^)$]*)\)")
_RES = re.compile(r"^\d+[kK]$|720p|1080p|4[kK]|\bHD\b|\bSD\b")
_THROUGH = re.compile(r"through [A-Z][a-z]+ \d{1,2}, \d{4}", re.I)
# [추가 2026-08-15] '$0.067 per 1K/2K image' · '$0.12 per 4K image' — 해상도별 장당 단가.
# 배치·flex 출력 칸이 토큰당 값 없이 장당만 적는 자리가 있어(Nano Banana Pro 실측)
# 토큰당으로 넣으면 틀린 값이 된다. 'per image'(해상도 없음)는 여기 안 걸리고 _gpp 로 간다
_PER_RES_IMAGE = re.compile(
    r"\$\s*([\d.,]+)\s*per\s+([\w/.]+)(?:\s+resolution)?\s+image", re.I)
# [추가 2026-08-15] 토큰당 단가로 보기엔 작은 값의 하한. Google 페이지의 토큰당 최저가는
# $0.01(Flash-Lite 캐시 읽기)이고, 그 아래로 나오는 것은 '$0.0006 (image)' 같은
# 장당 환산 표기뿐이다(560토큰 = 1장 각주). 환산 표기는 안 받는다(수집기준_결정항목.md)
_MIN_PER_MTOK = 0.01

# 행 이름 -> (item, modality, variant). None 이면 이름 규칙 함수에서 판정
_G_ROW_ITEMS = [
    ("context caching (storage)", ("cache_storage", None, "")),
    ("context caching price", ("cache_read", None, "")),
    ("audio input price", ("input", "audio", "")),
    ("text input price", ("input", "text", "")),
    ("image input price", ("input", "image", "")),
    ("video input price", ("input", "video", "")),
    ("input price", ("input", None, "")),
    ("output price", ("output", None, "")),
    ("tuning price", ("training", None, "")),
    ("image generation pricing", ("generation", "image", "")),
    ("video price", ("generation", "video", "")),
]


def _g_row_item(name):
    """행 이름 -> (item, modality, variant). 못 정하면 None."""
    low = name.lower()
    for pat, hit in _G_ROW_ITEMS:
        if low.startswith(pat):
            return hit
    m = re.match(r"(?:imagen [\d.]+ )?(\w+) image price", low)
    if m:
        return ("generation", "image", m.group(1))
    m = re.match(r"veo [\d.]+ (\w+) video", low)
    if m:
        return ("generation", "video", m.group(1))
    if low.startswith("lyria"):
        var = "clip" if "clip" in low else "full song"
        return ("generation", "audio", var)
    if low.startswith("grounding with"):
        return ("grounding", None, name.split("with", 1)[1].strip())
    return None


class _GoogleMd:

    def parse_rows(self, text):
        tables = _md.parse_tables(text)
        rows = []
        tiers = ("Standard", "Batch", "Flex", "Priority")
        for t in tables:
            if not t.headings or not t.money_count():
                continue
            # 이 마크다운에는 h1 이 없어 제목 경로가 (모델 h2, 등급 h3)다.
            # 오른쪽 끝이 등급이면 등급, 등급이 아닌 첫 제목이 모델(절 이름)
            tier = t.headings[-1].lower() if t.headings[-1] in tiers else "standard"
            head = next((h for h in reversed(t.headings) if h not in tiers), "")
            model = _EMOJI.sub("", head).strip()
            paid = next((i for i, h in enumerate(t.header)
                         if "paid tier" in h.lower()), None)
            if head.startswith("Pricing for"):
                rows += self._tools_table(t, head, paid)
                continue
            if paid is None:
                continue
            unit = self._header_unit(t.header[paid])
            t.used = True
            for r in t.rows:
                if paid >= len(r):
                    continue
                name = _FOOT.sub("", r[0]).strip()
                rows += self._cell(model, tier, name, r[paid], unit)
        rows, dup = _md.dedupe_rows(rows, self.provider)
        self._warns += dup
        self._warns += _md.leftover_money_warnings(
            self.provider, tables, ignore=("Free", "Paid", "Enterprise"))
        return rows

    @staticmethod
    def _header_unit(header):
        low = header.lower()
        if "per second" in low:
            return "per_second"
        if "per image" in low:
            return "per_image"
        if "per request" in low:
            return "per_call"
        return "per_1M_tokens"

    def _cell(self, model, tier, row_name, cell, unit):
        """Paid 칸 하나 -> 줄 목록. 한 칸 여러 값 유형을 순서대로 가른다."""
        if "$" not in cell:
            return []
        hit = _g_row_item(row_name)
        if hit is None:
            self._warns.append(
                f"{self.provider}: 모르는 행 이름 - {model} · {row_name[:50]}")
            return []
        item, modality, variant = hit
        out = []

        def add(value, ctx="default", mod=modality, var=variant,
                it=item, u=unit, note=""):
            # [추가 2026-08-15] 토큰당 단가 하한 미만 = 장당 환산 표기('$0.0006 (image)').
            # 저장하지 않는다. 원문은 같은 칸의 다른 줄 note 에 남는다
            if u == "per_1M_tokens" and value < _MIN_PER_MTOK:
                return
            out.append(PriceRow(
                provider=self.provider, model=model, item=it, value=value,
                unit=u, tier=tier, context=ctx, modality=mod, variant=var,
                category=model, source_url=_MD_URL, note=note))

        body = cell
        # 'Equivalent to $0.045 per 0.5K image' 류 = 환산 참고 표기. 공지 단가가
        # 아니라 저장하지 않는다(계산값 금지 기조). 원문은 note 로 남는다
        # [수정 2026-08-15] 대소문자 무시 — 'equivalent to $0.0011 per image' 가 소문자라
        # 안 잘려 0.0011 이 토큰당 값으로 들어갔다(Nano Banana Pro 표준 입력 실측)
        eq = re.search(r"equivalent to", body, re.I)
        if eq:
            body = body[:eq.start()]
        # [추가 2026-08-15] 해상도별 장당 단가는 per_image + variant 로 먼저 떼어낸다
        for m in _PER_RES_IMAGE.finditer(body):
            add(float(m.group(1).replace(",", "")), mod="image", u="per_image",
                var=(variant + " " + m.group(2)).strip(), note=cell[:150])
        body = _PER_RES_IMAGE.sub("", body)
        # 보관료가 딸린 캐시 칸: 보관료를 떼어 cache_storage 줄로
        st = _STORAGE.search(body)
        if st:
            add(float(st.group(1).replace(",", "")), it="cache_storage",
                u="per_1M_tokens_per_hour", note=cell[:150])
            body = _STORAGE.sub("", body)

        ctx_pairs = _CTX_PAIR.findall(body)
        if ctx_pairs:
            for val, op in ctx_pairs:
                add(float(val.replace(",", "")),
                    ctx="short" if op == "<=" else "long", note=cell[:150])
            return out
        if _THROUGH.search(body) and "starting" in body:
            # 시기 갈림: 지금 적용값(첫 값)만. 예고값은 원문 note 로만 남긴다
            m = _md.MONEY.search(body)
            if m:
                add(float(m.group(1).replace(",", "")), note=cell[:150])
            return out
        quals = _QUAL.findall(body)
        plain = _md.MONEY.findall(_QUAL.sub("", body))
        # [수정 2026-08-15] 괄호가 하나뿐이어도 자료형 규칙을 탄다. 전에는 값이 둘 이상일 때만
        # 탔는데, TTS 의 '$0.25 (text)'·'$5.00 (audio)' 가 자료형 없이 들어가고, 장당 값을 떼낸
        # 뒤 하나 남은 '$6.00 (text and thinking)' 이 표준(text)과 다르게 갈렸다
        if quals:
            for val, per_min, q in quals:
                q = q.strip().lower()
                if _RES.search(q):
                    add(float(val.replace(",", "")),
                        var=(variant + " " + q).strip(), note=cell[:150])
                    continue
                # [수정 2026-08-15] 'text / image / video'·'text/image' 처럼 자료형 여러 개가
                # 묶인 괄호는 구분 없는 공통 단가 = modality 없음. 앞 낱말만 보고 text 로
                # 넣으면 같은 모양의 칸이 표준에서는 text, priority 에서는 없음으로 갈렸다
                mod = (None if "/" in q or "," in q else
                       "text" if q.startswith("text") else
                       "audio" if q.startswith("audio") else
                       "image" if q.startswith("image") else
                       "video" if q.startswith("video") else q[:20])
                add(float(val.replace(",", "")), mod=mod, note=cell[:150])
                if per_min:
                    # 같은 자료형의 분당 병기 단가('or $0.005/min') — 별도 단위 줄
                    add(float(per_min.replace(",", "")), mod=mod,
                        u="per_minute", note=cell[:150])
            for val in plain:
                add(float(val.replace(",", "")), note=cell[:150])
            return out
        vals = _gpp(body, default_unit=unit)
        if vals:
            v = vals[0]
            add(v.value, u=v.unit or unit,
                note=cell[:150] if len(vals) > 1 or v.unit != unit else "")
        return out

    def _tools_table(self, t, section, paid=None):
        """도구·에이전트 절: 행 이름이 항목이 아니라 도구 이름이다."""
        t.used = True
        out = []
        for r in t.rows:
            cells = [r[paid]] if paid is not None and paid < len(r) else r[1:]
            for c in cells:
                if "$" not in c:
                    continue
                vals = _gpp(c, default_unit="per_call")
                if vals:
                    name = _FOOT.sub("", r[0]).strip()
                    item = "grounding" if "grounding" in name.lower() else "tool_call"
                    out.append(PriceRow(
                        provider=self.provider, model=name, item=item,
                        value=vals[0].value, unit=vals[0].unit,
                        category=section, source_url=_MD_URL, note=c[:150]))
                    break
        return out


GeminiAdapter.md_url = _MD_URL
GeminiAdapter.parse_rows = _GoogleMd.parse_rows
GeminiAdapter._header_unit = staticmethod(_GoogleMd._header_unit)
GeminiAdapter._cell = _GoogleMd._cell
GeminiAdapter._tools_table = _GoogleMd._tools_table
