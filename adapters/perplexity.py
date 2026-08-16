"""Perplexity(Sonar) 공식 마크다운 가격 페이지 수집 코드.

명세 = 수집기준_결정항목.md Perplexity 절.
받을 절 4개만 화이트리스트(Cost Examples = 예시 계산 함정, Gateway = 별도 페이지
안 받기로). 페이지 안 계산기 JSON('단일 정본'이라 자칭)과 표를 맞대 공짜 교차검증.
"""
from .base import BaseAdapter


class PerplexityAdapter(BaseAdapter):
    provider = "Perplexity"
    url = "https://docs.perplexity.ai/docs/getting-started/pricing"  # 2026-07-25 리다이렉트 최종 주소로 갱신

import json as _json

from . import mdtable as _md
from .base import PriceRow
from .pricetext import parse_prices as _pp

_MD_URL = "https://docs.perplexity.ai/docs/getting-started/pricing.md"


class _PerplexityMd:

    def parse_rows(self, text):
        tables = _md.parse_tables(text)
        rows = []
        for t in tables:
            path = " / ".join(t.headings)
            if "Sonar API" in path:
                rows += self._sonar(t)
            elif "Agent API" in path or "Tool Pricing" in path:
                rows += self._agent_tools(t)
            elif "Search API" in path:
                rows += self._search_api(t)
            elif "Embeddings" in path:
                rows += self._embeddings(t)
        rows, dup = _md.dedupe_rows(rows, self.provider)
        self._warns += dup
        self._warns += _md.leftover_money_warnings(
            self.provider, tables,
            ignore=("Cost Examples", "Estimate", "Gateway"))
        self._warns += self._json_crosscheck(text, rows)
        return rows

    def _one(self, cell, default_unit="per_1M_tokens"):
        vals = _pp(cell, default_unit=default_unit)
        return (vals[0].value, vals[0].unit) if vals else None

    def _sonar(self, t):
        sec = "Sonar API Pricing"
        # 본 단가 표: 항목이 열 이름에 박혀 있다
        if t.col("Input Tokens") is not None:
            t.used = True
            plan = [("Input Tokens", "input", "per_1M_tokens"),
                    ("Output Tokens", "output", "per_1M_tokens"),
                    ("Citation Tokens", "citation", "per_1M_tokens"),
                    ("Search Queries", "search_query", "per_1K_call"),
                    ("Reasoning Tokens", "reasoning", "per_1M_tokens")]
            out = []
            for r in t.rows:
                for col, item, unit in plan:
                    ci = t.col(col)
                    if ci is None or ci >= len(r):
                        continue
                    got = self._one(r[ci], default_unit=unit)
                    if got:
                        out.append(PriceRow(
                            provider=self.provider, model=r[0], item=item,
                            value=got[0], unit=unit, category=sec,
                            source_url=_MD_URL))
            return out
        # 검색 요청비: 문맥 크기(Low/Medium/High)가 열로 갈린 표
        if t.col("Low Context Size") is not None:
            t.used = True
            out = []
            for r in t.rows:
                for col, ctx in (("Low Context Size", "low"),
                                 ("Medium Context Size", "medium"),
                                 ("High Context Size", "high")):
                    ci = t.col(col)
                    if ci is None or ci >= len(r):
                        continue
                    got = self._one(r[ci], default_unit="per_1K_call")
                    if got:
                        out.append(PriceRow(
                            provider=self.provider, model=r[0],
                            item="search_query", value=got[0],
                            unit="per_1K_call", context=ctx, category=sec,
                            source_url=_MD_URL))
            return out
        # 검색 종류(fast/pro) 표: 한 칸에 '$6 / $10 / $14' = low/medium/high
        if t.col("Request Fee") is not None and t.col("Search Type") is not None:
            t.used = True
            out = []
            ci = t.col("Request Fee")
            for r in t.rows:
                vals = _md.MONEY.findall(r[ci]) if ci < len(r) else []
                if len(vals) == 3:
                    for v, ctx in zip(vals, ("low", "medium", "high")):
                        out.append(PriceRow(
                            provider=self.provider, model="Sonar Pro",
                            item="search_query", value=float(v.replace(",", "")),
                            unit="per_1K_call", context=ctx, variant=r[0],
                            category=sec, source_url=_MD_URL, note=r[ci]))
                elif vals:
                    self._warns.append(f"{self.provider}: 검색 종류 표 칸의 값이 "
                                       f"3개가 아님 - {r[0]}: {r[ci][:50]}")
            return out
        return []

    def _agent_tools(self, t):
        cp = t.col("Price")
        if cp is None:
            return []
        t.used = True
        out = []
        for r in t.rows:
            got = self._one(r[cp], default_unit="")
            if got and got[1]:
                item = "session" if "session" in r[cp] else "tool_call"
                out.append(PriceRow(
                    provider=self.provider, model=r[0], item=item,
                    value=got[0], unit=got[1], category="Agent API Pricing",
                    source_url=_MD_URL, note=r[cp][:120]))
        return out

    def _search_api(self, t):
        ci = t.col("Price per 1K requests")
        if ci is None:
            return []
        t.used = True
        out = []
        for r in t.rows:
            got = self._one(r[ci], default_unit="per_1K_call")
            if got:
                out.append(PriceRow(
                    provider=self.provider, model=r[0], item="search_query",
                    value=got[0], unit="per_1K_call",
                    category="Search API Pricing", source_url=_MD_URL))
        return out

    def _embeddings(self, t):
        ci = t.col("Price")
        if ci is None:
            return []
        t.used = True
        out = []
        cd = t.col("Dimensions")
        for r in t.rows:
            got = self._one(r[ci])
            if got:
                note = f"{r[cd]} dimensions" if cd is not None and cd < len(r) else ""
                out.append(PriceRow(
                    provider=self.provider, model=r[0], item="input",
                    value=got[0], unit="per_1M_tokens",
                    category="Embeddings API Pricing", source_url=_MD_URL,
                    note=note))
        return out

    def _json_crosscheck(self, text, rows):
        """페이지 안 계산기 JSON('단일 정본' 자칭)과 표 값 맞대기. 저장은 표 값
        (2026-08-15 명세), JSON 은 대조 전용 — 어긋나면 경고만."""
        i = text.find("const PRICING = {")
        if i < 0:
            return [f"{self.provider}: 계산기 JSON 을 못 찾음 - 교차 대조 생략"]
        depth = 0
        j = i + len("const PRICING = ")
        for k in range(j, len(text)):
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    break
        try:
            data = _json.loads(text[j:k + 1])
        except ValueError as e:
            return [f"{self.provider}: 계산기 JSON 해석 실패 - {e}"]

        def leaves(o, key=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield from leaves(v, k)
            elif isinstance(o, list):
                for v in o:
                    yield from leaves(v, key)
            elif isinstance(o, (int, float)) and not isinstance(o, bool):
                # 차원 수 같은 가격 아닌 숫자는 대조에서 뺀다(1024·2560 실측)
                if "dim" not in key.lower():
                    yield float(o)

        json_vals = {v for v in leaves(data.get("sonar", {}))} \
            | {v for v in leaves(data.get("embeddings", {}))}
        table_vals = {r.value for r in rows}
        missing = sorted(v for v in json_vals if v not in table_vals)
        if missing:
            return [f"{self.provider}: 계산기 JSON 에는 있는데 표에서 못 받은 값 "
                    f"{len(missing)}개 - {missing[:6]}"]
        return []


PerplexityAdapter.md_url = _MD_URL
for _n in ("parse_rows", "_one", "_sonar", "_agent_tools", "_search_api",
           "_embeddings", "_json_crosscheck"):
    setattr(PerplexityAdapter, _n, getattr(_PerplexityMd, _n))
