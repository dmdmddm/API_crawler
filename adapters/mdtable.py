"""마크다운 가격 페이지의 표를 절 이름·열 이름으로 찾는 공통 도구 (2026-08-15).

표를 몇 번째인지(순서)로 집으면 페이지 개편에 뚫린다 — 2026-08-06과 08-14 사이에
OpenAI 의 Cyber 가 Specialized 표 안의 행에서 독립 구역으로 옮겨간 실측. 그래서
표마다 "어느 절 아래, 어느 라벨 밑, 열 이름이 무엇"을 같이 들고 다니게 하고,
어댑터는 그 조합으로 표를 고른다. 못 알아본 표에 금액이 있으면 경고를 만든다
(수집기준_결정항목.md §검증 반영 보강 — 받을 것을 지목, 새 표는 자동으로 걸림).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

# 금액 표기. 통화 기호가 붙은 것만 값으로 본다(순수 숫자는 열 밀림 오염원)
MONEY = re.compile(r"\$\s*([\d][\d,]*(?:\.\d+)?)")

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")   # [글자](주소) -> 글자
_BOLD = re.compile(r"\*\*([^*]*)\*\*")         # **글자** -> 글자
_CODE = re.compile(r"`([^`]*)`")               # `글자` -> 글자


def clean(cell: str) -> str:
    """칸 하나의 마크다운 치장 제거. 링크·굵게·코드 표시·이스케이프(\\$ 등)."""
    s = _LINK.sub(r"\1", cell)
    s = _BOLD.sub(r"\1", s)
    s = _CODE.sub(r"\1", s)
    s = s.replace("\\$", "$").replace("\\<", "<").replace("\\>", ">")
    return s.strip()


@dataclass
class MdTable:
    headings: tuple      # 표 위의 제목 경로. 예: ('Pricing', 'Batch API Pricing')
    label: str           # 표 바로 위의 짧은 안내 줄(등급 라벨 등). 예: 'Standard'
    header: list         # 첫 행(열 이름). clean() 적용됨
    rows: list           # 자료 행 목록. 행 = clean() 된 칸 목록
    start_line: int      # 원문에서 표가 시작한 줄 번호(1부터. 경고에 적을 위치)
    used: bool = field(default=False, compare=False)   # 어댑터가 집어 갔는지

    @property
    def section(self) -> str:
        """가장 가까운 제목 한 개. 대부분의 판정은 이것으로 충분하다."""
        return self.headings[-1] if self.headings else ""

    def money_count(self) -> int:
        return sum(1 for r in self.rows for c in r if MONEY.search(c))

    def col(self, name_part: str):
        """열 이름에 name_part 가 든 첫 열 번호. 없으면 None. 대소문자 무시."""
        low = name_part.lower()
        for i, h in enumerate(self.header):
            if low in h.lower():
                return i
        return None


def parse_tables(text: str) -> list:
    """마크다운 전문 -> MdTable 목록.

    제목 추적: '#' 줄을 수준별로 쌓는다. 라벨: 지난 표 이후 나온 짧은 맨글자 줄
    (60자 미만) — OpenAI 가 'Standard' 같은 등급 라벨을 제목이 아니라 맨글자 줄로
    두는 것을 줍기 위한 것. ★제목이 라벨을 지우면 안 된다: OpenAI 이미지 생성이
    'Standard' 맨글자 줄 -> 무의미한 제목('Grouped Pricing Table data') -> 표 순서라,
    제목에서 지우면 등급 정보가 사라진다(2026-08-15 실물 시험에서 잡음). 라벨은
    표 하나를 내보낼 때만 비운다.
    """
    lines = text.split("\n")
    out = []
    heads = {}            # 수준(1-6) -> 제목 글자
    label = ""
    i = 0
    while i < len(lines):
        s = lines[i].rstrip()
        m = re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            lv = len(m.group(1))
            heads[lv] = clean(m.group(2))
            for deeper in [k for k in heads if k > lv]:
                del heads[deeper]
            i += 1
            continue
        if s.lstrip().startswith("|"):
            start = i
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [clean(c) for c in lines[i].split("|")[1:-1]]
                if cells and not set("".join(cells)) <= set("-: "):
                    block.append(cells)
                i += 1
            if block:
                out.append(MdTable(
                    headings=tuple(heads[k] for k in sorted(heads)),
                    label=label, header=block[0], rows=block[1:],
                    start_line=start + 1))
            label = ""
            continue
        t = s.strip()
        # 인용·목록·코드·컴포넌트 줄은 라벨이 아니다(xAI 할인 목록 '- grok-4.3' 등)
        if t and len(t) < 60 and t[0] not in ">-*#`<":
            label = clean(t)
        i += 1
    return out


def leftover_money_warnings(provider: str, tables: list, ignore=()) -> list:
    """어댑터가 안 집어 간(used=False) 표 중 금액이 든 것 -> 경고 목록.

    ignore: 일부러 안 받는 절 이름 조각들(예: Perplexity 'Cost Examples').
    일부러 버린 것과 몰라서 놓친 것을 가른다 — 뭉치면 경고가 일상이 되어 묻힌다.
    """
    warns = []
    for t in tables:
        if t.used:
            continue
        if any(ig.lower() in h.lower() for h in t.headings for ig in ignore):
            continue
        n = t.money_count()
        if n:
            warns.append(f"{provider}: 못 알아본 표에 금액 {n}칸 - "
                         f"절 '{t.section}' 라벨 '{t.label}' {t.start_line}행 "
                         f"열 {t.header[:4]}")
    return warns


def dedupe_rows(rows, provider=""):
    """같은 키의 줄 정리. 페이지가 같은 모델을 두 절에 싣는 경우가 실재한다
    (OpenAI gpt-5.6-sol 이 Flagship 과 Cyber 양쪽에 — 2026-08-15 실측 8건).

    값이 같으면 먼저 나온 줄만 남긴다(category = 먼저 나온 절).
    값이 다르면 둘 다 버리지 않고 첫 줄을 남기되 경고를 만든다 —
    같은 축에 다른 값 = 페이지 모순이거나 파서 오류라 사람이 봐야 한다.
    반환 = (정리된 줄 목록, 경고 목록).
    """
    seen = {}
    out, warns = [], []
    for r in rows:
        k = r.key()
        if k not in seen:
            seen[k] = r
            out.append(r)
        elif seen[k].value != r.value:
            warns.append(f"{provider}: 같은 축에 다른 값 - {r.model} {r.item} "
                         f"{seen[k].value} (절 '{seen[k].category}') 대 "
                         f"{r.value} (절 '{r.category}')")
    return out, warns
