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
_AREAS = ("Flagship models", "Cyber models", "Multimodal models",
          "Tools", "Specialized models", "Finetuning")
# 모델 이름 뒤 문맥 한정 꼬리. 떼서 note 로 (같은 모델이 두 이름으로 갈라지지 않게)
_CTX_TAIL = re.compile(r"\s*\((<\s*\d+K? context length)\)\s*$")
# 컨테이너 요금 칸의 'N GB $값' 짝
_GB_PAIR = re.compile(r"(\d+)\s*GB\s*\$\s*([\d.]+)")


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

    # 등급 이름: '### Standard pricing data' -> standard. Fast 는 페이지 말이
    # 'Fast mode'라 fast 로 줄인다(Priority 에서 개명된 것, 2026-07-30)
    @staticmethod
    def _tier_of(t):
        w = (t.section.split() or ["standard"])[0].lower()
        return w if w in ("standard", "batch", "flex", "fast") else ""

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

    def _ctx_table(self, t, area):
        """Flagship·Cyber: 열 이름에 short/long 문맥 + 항목이 같이 있는 8열 표."""
        cols = []
        for i, h in enumerate(t.header[1:], 1):
            low = h.lower()
            ctx = "short" if "short context" in low else (
                  "long" if "long context" in low else None)
            if ctx is None:
                continue
            item = ("cache_read" if "cached input" in low else
                    "cache_write" if "cache write" in low else
                    "input" if "input" in low else
                    "output" if "output" in low else None)
            if item:
                cols.append((i, ctx, item))
        if not cols:
            return []
        t.used = True
        tier = self._tier_of(t) or "standard"
        out = []
        for r in t.rows:
            model, tail = self._model_of(r[0])
            for i, ctx, item in cols:
                if i >= len(r):
                    continue
                got = self._money(r[i])
                if got:
                    out.append(_row(area, model=model, item=item, value=got[0],
                                    unit=got[1], tier=tier, context=ctx,
                                    note=tail))
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
        tier = "fast" if t.label == "Fast mode" else "standard"
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
