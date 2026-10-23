"""OpenAI 공식 마크다운 가격 페이지 수집 코드.

명세 = 수집기준_결정항목.md OpenAI 절.
구역(Flagship 등 6개)이 제목이 아니라 맨글자 줄이라, 표의 시작 줄 번호로
어느 구역 아래인지 판정한다.
"""
import re

from .base import BaseAdapter


class OpenAIAdapter(BaseAdapter):
    provider = "OpenAI"
    url = "https://developers.openai.com/api/docs/pricing"  # 2026-07-25 리다이렉트 최종 주소로 갱신



from . import mdtable as _md
from .base import PriceRow
from .pricetext import parse_prices as _pp

_MD_URL = "https://developers.openai.com/api/docs/pricing.md"
_AREAS = ("Flagship models", "Cyber models", "Life sciences models",
          "Multimodal models", "Tools", "Specialized models", "Finetuning")
# 모델 이름 뒤 문맥 한정 꼬리. 떼서 note 로 (같은 모델이 두 이름으로 갈라지지 않게)
_CTX_TAIL = re.compile(r"\s*\((<\s*\d+K? context length)\)\s*$")
# 컨테이너 요금 칸의 'N GB $값' 짝
_GB_PAIR = re.compile(r"(\d+)\s*GB\s*\$\s*([\d.]+)")
# 구역처럼 보이는 줄과, 구역이 아닌 하위 제목(Multimodal 안의 묶음 이름 등)
_AREA_LIKE = re.compile(r"^[A-Z][A-Za-z ]* models$")
_SUB_HEADINGS = ("Our latest models", "Realtime and audio generation models",
                 "Image generation models", "Transcription models",
                 "Video generation models", "GPT-Live sessions", "Cloud platforms",
                 "Standard", "Batch", "Flex", "Fast", "Ultrafast")
# Flagship·Cyber 의 등급별 표 절 이름
_PRICING_DATA = re.compile(r"\b(?:standard|batch|flex|fast|ultrafast) pricing data\b", re.I)


# 표의 i 번째 열에 금액 칸이 하나라도 있는지
def _money_col(t, i):
    return any(i < len(r) and _md.MONEY.search(r[i]) for r in t.rows)
# 캐시 쓰기 열의 보관 시간('Short context 30m write' 의 30m)
_TTL_WRITE = re.compile(r"(\d+)\s*(m|h)\s*write")
# Flagship·Cyber 구역에서 문맥 열 없이 'Input, Output, Cached input' 만 있는 표의 문맥 구간
_PLAIN_CONTEXT = "default"


# 열 이름(소문자) -> (item, cache_ttl). 항목 열이 아니면 None
def _item_of(low):
    if "cached input" in low:
        return "cache_read", ""
    m = _TTL_WRITE.search(low)
    if m:
        return "cache_write", m.group(1) + m.group(2)
    # 보관 시간 표기를 못 읽은 쓰기 열('Cache write 1hr' 등)은 모르는 열로 둠
    if "write" in low and re.search(r"\d", low):
        return None
    if "cache write" in low:
        return "cache_write", ""
    if "input" in low:
        return "input", ""
    if "output" in low:
        return "output", ""
    return None


def _row(area, **kw):
    base = dict(provider="OpenAI", source_url=_MD_URL, category=area)
    base.update(kw)
    return PriceRow(**base)


class _OpenAIMd:
    """OpenAIAdapter.parse_rows 의 구현 묶음. 표 하나 = 메서드 하나."""

    def parse_rows(self, text):
        tables = _md.parse_tables(text)
        marks = [(i, ln.strip()) for i, ln in enumerate(text.split("\n"), 1)
                 if ln.strip() in _AREAS]
        # 구역 목록에도 하위 제목 목록에도 없는 제목 줄 = 새 구역일 수 있음
        # 제목 줄 = 'OO models' 꼴이거나, 빈 줄과 공백 줄 뒤에 맨 앞 칸부터 쓰이고 빈 줄 둘이 따르는 짧은 줄
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            name = ln.strip()
            if not name or name in _AREAS or name in _SUB_HEADINGS:
                continue
            heading_like = (ln == name and len(name) < 60 and name[0] not in " |#<>-*`[$"
                            and lines[max(0, i - 3):i] == ["", lines[i - 2], ""]
                            and lines[i - 2] != "" and not lines[i - 2].strip()
                            and lines[i + 1:i + 3] == ["", ""])
            if _AREA_LIKE.match(name) or heading_like:
                self._warns.append(f"{self.provider}: 모르는 구역 이름 - '{name}'")

        def area_of(t):
            a = ""
            for ln, name in marks:
                if ln < t.start_line:
                    a = name
            return a

        rows = []
        for t in tables:
            a = area_of(t)
            if a in ("Flagship models", "Cyber models"):
                rows += self._ctx_table(t, a)
            elif a == "Life sciences models":
                # 문맥 구간 선택이 없는 구역
                rows += self._ctx_table(t, a, plain_ctx="default")
            elif a == "Multimodal models":
                rows += self._multimodal(t, a)
            elif a == "Tools":
                rows += self._tools(t, a)
            elif a == "Specialized models":
                rows += self._specialized(t, a)
            elif a == "Finetuning":
                rows += self._finetune(t, a)
        rows, dup_warns = _md.dedupe_rows(rows, self.provider)
        self._warns += dup_warns
        self._warns += _md.leftover_money_warnings(self.provider, tables)
        return rows

    # 등급 이름 = 절 제목의 첫 낱말(예: '### Ultrafast pricing data'의 ultrafast)
    # 허용 목록 밖의 이름은 빈 문자열이고 호출하는 쪽이 standard로 처리
    @staticmethod
    def _tier_of(t):
        w = (t.section.split() or ["standard"])[0].lower()
        return w if w in ("standard", "batch", "flex", "fast", "ultrafast") else ""

    @staticmethod
    def _model_of(cell):
        m = _CTX_TAIL.search(cell)
        if m:
            return _CTX_TAIL.sub("", cell).strip(), cell
        return cell.strip(), ""

    def _money(self, cell, default_unit="per_1M_tokens"):
        """칸 하나 -> (값, 단위) 또는 None. 값이 여럿이면 첫 값 + 경고 없이 note 몫."""
        vals = _pp(cell, default_unit=default_unit)
        return (vals[0].value, vals[0].unit) if vals else None

    def _ctx_table(self, t, area, plain_ctx=None):
        """Flagship·Cyber: 열 이름에 short/long 문맥 + 항목이 같이 있는 표."""
        ctx_of = {}
        for i, h in enumerate(t.header[1:], 1):
            low = h.lower()
            ctx_of[i] = ("short" if "short context" in low else
                         "long" if "long context" in low else None)
        cols = []
        if any(ctx_of.values()):
            for i, h in enumerate(t.header[1:], 1):
                got = _item_of(h.lower()) if ctx_of[i] else None
                if got:
                    cols.append((i, ctx_of[i]) + got)
                elif _money_col(t, i):
                    self._warns.append(f"{self.provider}: 모르는 열 이름 - 절 '{t.section}' 열 '{h}'")
        # 문맥 열 없이 'Model, Input, Output, Cached input' 만 있는 표.
        # Flagship·Cyber 에서는 등급별 'OO pricing data' 절 아래 표만(새 구역 표를 앞 구역으로 집지 않게)
        elif (t.col("Model") == 0 and t.col("Input") is not None
              and (plain_ctx or _PRICING_DATA.search(" > ".join(t.headings)))):
            for i, h in enumerate(t.header[1:], 1):
                got = _item_of(h.lower())
                if got:
                    cols.append((i, plain_ctx or _PLAIN_CONTEXT) + got)
                elif _money_col(t, i):
                    self._warns.append(f"{self.provider}: 모르는 열 이름 - 절 '{t.section}' 열 '{h}'")
        if not cols:
            return []
        t.used = True
        tier = self._tier_of(t) or "standard"
        out = []
        for r in t.rows:
            model, tail = self._model_of(r[0])
            for i, ctx, item, ttl in cols:
                if i >= len(r):
                    continue
                got = self._money(r[i])
                if got:
                    out.append(_row(area, model=model, item=item, value=got[0],
                                    unit=got[1], tier=tier, context=ctx,
                                    cache_ttl=ttl, note=tail))
        return out

    def _multimodal(self, t, area):
        tier = t.label.lower() if t.label in ("Standard", "Batch") else "standard"
        if t.col("Modality") is not None:
            t.used = True
            out = []
            im, ii = t.col("Model"), t.col("Modality")
            plan = [(t.col("Input"), "input"), (t.col("Cached input"), "cache_read"),
                    (t.col("Output"), "output")]
            for r in t.rows:
                for ci, item in plan:
                    if ci is None or ci >= len(r):
                        continue
                    # 'Cached input' 열이 'Input' 으로도 걸리는 것 방지: 열 번호 중복 제거
                    got = self._money(r[ci])
                    if got:
                        out.append(_row(area, model=r[im], item=item,
                                        value=got[0], unit=got[1], tier=tier,
                                        modality=r[ii].lower() or None))
            return out
        if t.col("Price per second") is not None:
            t.used = True
            ci = t.col("Price per second")
            out = []
            for r in t.rows:
                got = self._money(r[ci], default_unit="per_second")
                if got:
                    out.append(_row(area, model=r[0], item="generation",
                                    value=got[0], unit=got[1], tier=tier,
                                    modality="video", variant=r[1], note=r[ci]))
            return out
        if t.col("Estimated cost") is not None or t.col("Use case") is not None:
            t.used = True
            out = []
            plan = [(t.col("Input"), "input", "per_1M_tokens"),
                    (t.col("Output"), "output", "per_1M_tokens"),
                    (t.col("Estimated cost"), "transcription", "per_minute")]
            uc = t.col("Use case")
            for r in t.rows:
                for ci, item, du in plan:
                    if ci is None or ci >= len(r):
                        continue
                    got = self._money(r[ci], default_unit=du)
                    if got:
                        out.append(_row(area, model=r[0], item=item,
                                        value=got[0], unit=got[1], tier=tier,
                                        note=r[uc] if uc is not None else ""))
            return out
        # 음성 세션 표: 'Model'·'Price per minute' 두 열. gpt-live-1 은 분당 요금이라 unit per_minute
        if t.col("Price per minute") is not None:
            t.used = True
            ci = t.col("Price per minute")
            im = t.col("Model") or 0
            out = []
            for r in t.rows:
                if ci >= len(r) or im >= len(r):
                    continue
                got = self._money(r[ci], default_unit="per_minute")
                if got:
                    out.append(_row(area, model=r[im], item="session",
                                    value=got[0], unit=got[1], tier=tier,
                                    modality="audio", note=r[ci]))
            return out
        return []

    def _tools(self, t, area):
        cp = t.col("Pricing")
        if cp is None:
            return []
        t.used = True
        out = []
        for r in t.rows:
            cell = r[cp]
            gb = _GB_PAIR.findall(cell)
            if len(gb) >= 2:
                for size, val in gb:
                    out.append(_row(area, model=r[0], item="tool_call",
                                    value=float(val), unit="per_session",
                                    variant=f"{size} GB", note=cell))
                continue
            got = self._money(cell, default_unit="per_call")
            if got:
                out.append(_row(area, model=r[0], item="tool_call",
                                value=got[0], unit=got[1],
                                variant=r[1] if len(r) > 1 else "", note=cell))
        return out

    def _specialized(self, t, area):
        cm = t.col("Model")
        if cm is None or t.col("Input") is None:
            return []
        t.used = True
        # fast 등급 탭 이름: 'Fast mode' 또는 'Fast'
        tier = "fast" if t.label in ("Fast mode", "Fast") else "standard"
        out = []
        plan = [(t.col("Input"), "input"), (t.col("Cached input"), "cache_read"),
                (t.col("Output"), "output")]
        for r in t.rows:
            for ci, item in plan:
                if ci is None or ci >= len(r):
                    continue
                got = self._money(r[ci])
                if got:
                    out.append(_row(area, model=r[cm], item=item, value=got[0],
                                    unit=got[1], tier=tier, note=r[0]))
        return out

    def _finetune(self, t, area):
        ct = t.col("Training")
        if ct is None:
            return []
        t.used = True
        tier = t.label.lower() if t.label in ("Standard", "Batch") else "standard"
        out = []
        plan = [(ct, "training"), (t.col("Input"), "input"),
                (t.col("Cached input"), "cache_read"), (t.col("Output"), "output")]
        for r in t.rows:
            model, tail = self._model_of(r[0])
            for ci, item in plan:
                if ci is None or ci >= len(r):
                    continue
                got = self._money(r[ci])
                if got:
                    out.append(_row(area, model=model, item=item, value=got[0],
                                    unit=got[1], tier=tier, note=tail))
        return out


# OpenAIAdapter 에 새 경로를 붙인다(클래스 정의를 흩뜨리지 않고 뒤에서 확장)
OpenAIAdapter.md_url = _MD_URL
for _name in ("parse_rows", "_ctx_table", "_multimodal", "_tools",
              "_specialized", "_finetune"):
    setattr(OpenAIAdapter, _name, getattr(_OpenAIMd, _name))
OpenAIAdapter._tier_of = staticmethod(_OpenAIMd._tier_of)
OpenAIAdapter._model_of = staticmethod(_OpenAIMd._model_of)
OpenAIAdapter._money = _OpenAIMd._money
