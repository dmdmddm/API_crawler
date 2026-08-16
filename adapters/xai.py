"""xAI(Grok) 공식 마크다운 가격 페이지 수집 코드.

명세 = 수집기준_결정항목.md xAI 절.
문맥 구간이 모델 이름 괄호에 있고, 배치 할인은 표가 아니라 소제목+목록,
우선 처리는 문장(2x)뿐이라 배수 줄(multiplier)로 만든다.
"""
import re
from .base import BaseAdapter


class GrokAdapter(BaseAdapter):
    provider = "xAI"
    url = "https://docs.x.ai/developers/pricing"          # 사람이 읽는 가격 표

from . import mdtable as _md
from .base import PriceRow
from .pricetext import parse_prices as _pp

_MD_URL = "https://docs.x.ai/developers/pricing.md"
_CTX_PAREN = re.compile(r"\s*\(([<≥][^)]*prompt tokens)\)\s*$")
# '**20% off standard rates**' 소제목 + 밑의 '- 모델' 목록
_DISCOUNT_HEAD = re.compile(r"\*\*(\d+)% off standard rates\*\*")
_PRIORITY = re.compile(r"billed at a \*\*(\d+)x\*\* premium over standard rates")


class _XaiMd:

    def parse_rows(self, text):
        tables = _md.parse_tables(text)
        rows = []
        for t in tables:
            sec = t.section
            if t.col("Input / 1M tokens") is not None:
                rows += self._text_api(t)
            elif sec == "Imagine Pricing" and t.col("Cost") is not None:
                rows += self._per_unit(t, "Imagine Pricing", "generation")
            elif sec == "Voice Pricing" and t.col("Cost") is not None:
                rows += self._voice(t)
            elif t.col("Cost / 1k Calls") is not None:
                rows += self._tools(t, sec)
            elif sec in ("Files and Collections Pricing", "Download Costs") \
                    and t.col("Rate") is not None:
                rows += self._files(t, sec)
        rows += self._multiplier_rows(text, rows)
        rows, dup = _md.dedupe_rows(rows, self.provider)
        self._warns += dup
        # Batch/Priority 절 표는 금액 없는 설명 표라 leftover 에 안 걸린다
        self._warns += _md.leftover_money_warnings(self.provider, tables)
        return rows

    def _one(self, cell, default_unit="per_1M_tokens"):
        vals = _pp(cell, default_unit=default_unit)
        return (vals[0].value, vals[0].unit) if vals else None

    def _text_api(self, t):
        t.used = True
        out = []
        plan = [("Input / 1M", "input"), ("Cached input", "cache_read"),
                ("Output", "output")]
        ctx_col = t.col("Context")
        for r in t.rows:
            name = r[0]
            m = _CTX_PAREN.search(name)
            ctx = "default"
            note = ""
            if m:
                ctx = "short" if m.group(1).startswith("<") else "long"
                note = m.group(1)
                name = _CTX_PAREN.sub("", name).strip()
            if ctx_col is not None and ctx_col < len(r) and r[ctx_col]:
                note = (note + " · " if note else "") + f"context window {r[ctx_col]}"
            for col, item in plan:
                ci = t.col(col)
                if ci is None or ci >= len(r):
                    continue
                got = self._one(r[ci])
                if got:
                    out.append(PriceRow(
                        provider=self.provider, model=name, item=item,
                        value=got[0], unit=got[1], context=ctx,
                        category="Text API Pricing", source_url=_MD_URL,
                        note=note))
        return out

    def _per_unit(self, t, category, item):
        t.used = True
        out = []
        ci = t.col("Cost")
        for r in t.rows:
            got = self._one(r[ci], default_unit="")
            if got and got[1]:
                mod = ("video" if "video" in r[0] else
                       "image" if "image" in r[0] else None)
                out.append(PriceRow(
                    provider=self.provider, model=r[0], item=item,
                    value=got[0], unit=got[1], modality=mod,
                    category=category, source_url=_MD_URL, note=r[ci]))
            elif r[ci].strip():
                self._warns.append(
                    f"{self.provider}: 단위를 못 정한 칸 - {r[0]}: {r[ci][:60]}")
        return out

    def _voice(self, t):
        """음성 표. 한 칸에 값이 여럿(분당+글자당, REST/Streaming)인 것을 가른다."""
        t.used = True
        out = []
        ci = t.col("Cost")
        for r in t.rows:
            cell = r[ci]
            # 'REST'/'Streaming' 같은 변형 짝: '$0.10 / hr (REST), $0.20 / hr (Streaming)'
            pairs = re.findall(r"\$\s*([\d.]+)\s*/\s*hr\s*\(([A-Za-z]+)\)", cell)
            if pairs:
                for val, var in pairs:
                    out.append(PriceRow(
                        provider=self.provider, model=r[0], item="transcription",
                        value=float(val), unit="per_hour", variant=var,
                        modality="audio", category="Voice Pricing",
                        source_url=_MD_URL, note=cell))
                continue
            vals = _pp(cell, default_unit="")
            kept = False
            for v in vals:
                if not v.unit:
                    continue
                # 시간당 병기('($4.80 / hr)')는 분당의 환산 표기라 분당만 남긴다
                if v.unit == "per_hour" and any(x.unit == "per_minute" for x in vals):
                    continue
                item = ("output" if "per_1M_char" in v.unit else "transcription")
                out.append(PriceRow(
                    provider=self.provider, model=r[0], item=item,
                    value=v.value, unit=v.unit, modality="audio",
                    category="Voice Pricing", source_url=_MD_URL, note=cell))
                kept = True
            if not kept and cell.strip():
                self._warns.append(
                    f"{self.provider}: 음성 칸에서 값을 못 정함 - {r[0]}: {cell[:60]}")
        return out

    def _tools(self, t, sec):
        t.used = True
        out = []
        ci = t.col("Cost / 1k Calls")
        for r in t.rows:
            got = self._one(r[ci], default_unit="per_1K_call") if ci < len(r) else None
            if got:
                out.append(PriceRow(
                    provider=self.provider, model=r[0], item="tool_call",
                    value=got[0], unit="per_1K_call",
                    variant=r[1] if len(r) > 1 else "",
                    category=sec, source_url=_MD_URL, note=r[ci]))
        return out

    def _files(self, t, sec):
        t.used = True
        out = []
        ci = t.col("Rate")
        for r in t.rows:
            got = self._one(r[ci], default_unit="") if ci < len(r) else None
            if got and got[1]:
                item = "egress" if "download" in r[0].lower() else "storage"
                out.append(PriceRow(
                    provider=self.provider, model=r[0], item=item,
                    value=got[0], unit=got[1], category=sec,
                    source_url=_MD_URL, note=r[ci]))
        return out

    def _multiplier_rows(self, text, money_rows):
        """배수 줄. 배치 = 할인 목록의 모델 × 그 모델의 표준 항목(항목별 펼침).
        우선 처리 = Text API 전 모델 × 표준 항목. 명세 = §배수로만 공지하는 요금."""
        out = []
        std = {}
        for r in money_rows:
            if r.category == "Text API Pricing" and r.tier == "standard":
                std.setdefault(r.model, set()).add(r.item)

        m = _DISCOUNT_HEAD.search(text)
        if m:
            factor = round(1 - int(m.group(1)) / 100, 4)
            tail = text[m.end():]
            models = re.findall(r"^- +([\w.\-]+)", tail[:600], re.M)
            for name in models:
                if name not in std:
                    self._warns.append(
                        f"{self.provider}: 배치 할인 목록의 모델이 가격 표에 없음 - {name}")
                    continue
                for item in sorted(std[name]):
                    out.append(PriceRow(
                        provider=self.provider, model=name, item=item,
                        value=factor, unit="", tier="batch",
                        multiplier="standard", category="Batch API Pricing",
                        source_url=_MD_URL, note=m.group(0)))
        else:
            self._warns.append(f"{self.provider}: 배치 할인 소제목을 못 찾음 - "
                               f"페이지 개편 여부 확인 필요")

        p = _PRIORITY.search(text)
        if p:
            factor = float(p.group(1))
            for name, items in sorted(std.items()):
                for item in sorted(items):
                    out.append(PriceRow(
                        provider=self.provider, model=name, item=item,
                        value=factor, unit="", tier="priority",
                        multiplier="standard", category="Priority Processing Pricing",
                        source_url=_MD_URL, note=p.group(0)))
        else:
            self._warns.append(f"{self.provider}: 우선 처리 배수 문장을 못 찾음")
        return out


GrokAdapter.md_url = _MD_URL
for _n in ("parse_rows", "_one", "_text_api", "_per_unit", "_voice",
           "_tools", "_files", "_multiplier_rows"):
    setattr(GrokAdapter, _n, getattr(_XaiMd, _n))
