"""Anthropic(Claude) 공식 마크다운 가격 페이지 수집 코드.

명세 = 수집기준_결정항목.md Anthropic 절.
등급이 열이 아니라 별도 표(Model pricing / Fast mode / Batch)로 나뉜 구조.
캐시 배수 표(1.25x 등)는 금액이 아니라 안 받는다(금액이 Model pricing 에 있음).
"""
import re
from .base import BaseAdapter


class AnthropicAdapter(BaseAdapter):
    provider = "Anthropic"
    url = "https://platform.claude.com/docs/en/about-claude/pricing"  # 2026-07-25 리다이렉트 최종 주소로 갱신


from . import mdtable as _md
from .base import PriceRow
from .pricetext import parse_prices as _pp

_MD_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
# 이름 뒤 괄호 단서: (retired, ...) · (limited availability) -> 떼서 note 로
_NAME_TAIL = re.compile(r"\s*\(([^)]*)\)\s*$")


class _AnthropicMd:

    def parse_rows(self, text):
        tables = _md.parse_tables(text)
        rows = []
        for t in tables:
            sec = " / ".join(t.headings)
            if t.col("Base Input Tokens") is not None:
                rows += self._model_pricing(t)
            elif "Fast mode" in sec and t.col("Input") is not None:
                rows += self._simple(t, tier="fast", category="Fast mode pricing",
                                     plan=[("Input", "input"), ("Output", "output")])
            elif t.col("Batch input") is not None:
                rows += self._simple(t, tier="batch", category="Batch processing",
                                     plan=[("Batch input", "input"),
                                           ("Batch output", "output")])
            elif t.col("SKU") is not None and t.col("Rate") is not None:
                rows += self._session(t)
        rows, dup = _md.dedupe_rows(rows, self.provider)
        self._warns += dup
        # 일부러 안 받는 절: Worked example = 예시 계산 / AWS·Foundry = CCU 환산
        # 상수($0.01 = 1 CCU 고정, 모델 단가는 본 표와 동일 적용)라 단가가 아니다
        self._warns += _md.leftover_money_warnings(
            self.provider, tables,
            ignore=("Worked example", "Claude Platform on AWS",
                    "Claude in Microsoft Foundry"))
        return rows

    @staticmethod
    def _split_name(cell):
        m = _NAME_TAIL.search(cell)
        if m:
            return _NAME_TAIL.sub("", cell).strip(), m.group(1)
        return cell.strip(), ""

    def _one(self, cell):
        vals = _pp(cell)
        return (vals[0].value, vals[0].unit) if vals else None

    def _model_pricing(self, t):
        t.used = True
        plan = [("Base Input Tokens", "input", ""),
                ("5m Cache Writes", "cache_write", "5m"),
                ("1h Cache Writes", "cache_write", "1h"),
                # 2026-09-04: 열 이름이 'Cache Hits & Refreshes'에서
                # 'Cache hits and refreshes'로 바뀌어 09-02부터 cache_read 가
                # 통째로 안 잡혔다. 부분일치 라벨 'Cache hits'는 옛·새 이름 둘 다 잡는다
                ("Cache hits", "cache_read", ""),
                ("Output Tokens", "output", "")]
        out = []
        for r in t.rows:
            model, tail = self._split_name(r[0])
            for col, item, ttl in plan:
                ci = t.col(col)
                if ci is None or ci >= len(r):
                    continue
                got = self._one(r[ci])
                if got:
                    out.append(PriceRow(
                        provider=self.provider, model=model, item=item,
                        value=got[0], unit=got[1], cache_ttl=ttl,
                        category="Model pricing", source_url=_MD_URL, note=tail))
        return out

    def _simple(self, t, tier, category, plan):
        t.used = True
        out = []
        for r in t.rows:
            # Fast mode 는 한 칸에 모델 둘('Claude Opus 5 / Claude Opus 4.8')
            names = [n.strip() for n in r[0].split(" / ")] if " / " in r[0] else [r[0]]
            for name in names:
                model, tail = self._split_name(name)
                for col, item in plan:
                    ci = t.col(col)
                    if ci is None or ci >= len(r):
                        continue
                    got = self._one(r[ci])
                    if got:
                        out.append(PriceRow(
                            provider=self.provider, model=model, item=item,
                            value=got[0], unit=got[1], tier=tier,
                            category=category, source_url=_MD_URL, note=tail))
        return out

    def _session(self, t):
        t.used = True
        out = []
        ci = t.col("Rate")
        for r in t.rows:
            got = self._one(r[ci]) if ci < len(r) else None
            if got:
                out.append(PriceRow(
                    provider=self.provider, model="Claude Managed Agents",
                    item="session", value=got[0], unit=got[1],
                    category="Session runtime", source_url=_MD_URL, note=r[0]))
        return out


AnthropicAdapter.md_url = _MD_URL
AnthropicAdapter.parse_rows = _AnthropicMd.parse_rows
AnthropicAdapter._split_name = staticmethod(_AnthropicMd._split_name)
AnthropicAdapter._one = _AnthropicMd._one
AnthropicAdapter._model_pricing = _AnthropicMd._model_pricing
AnthropicAdapter._simple = _AnthropicMd._simple
AnthropicAdapter._session = _AnthropicMd._session
