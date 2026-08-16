"""DeepSeek 공식 가격 페이지 수집 코드.

표가 전치(모델=열)+병합 셀 구조라, 가격 행을 라벨로 찾아 모델별로 매핑한다.
입력가는 캐시 미스(cache miss) 기준이고, 캐시 히트가는 캐시 읽기 단가로 쓴다.
2026-07-25: 캐시 히트 행을 캐시 읽기 단가로 수집(이전에는 버렸다).
실측 deepseek-v4-flash: 캐시히트 $0.0028 / 캐시미스 $0.14 / 출력 $0.28.
캐시 쓰기·배치 단가는 이 페이지에 없다.

2026-08-14: 시간대별 요금 예고 표를 읽는 부분을 더했다. 페이지에 이렇게 적혀 있다 -
"off-peak rates at half the peak rates. Peak hours are 01:00-04:00 and 06:00-10:00 UTC.
The new prices take effect at 16:00 UTC on August 16, 2026". 같은 모델에 단가가 두 벌
(피크·오프피크)이 되므로 등급 칸(tier)에 나눠 담는다. 배치 단가를 담는 방식과 같다.
시행 전까지는 run.py in_effect·db.applies_on 이 effective_from 을 보고 걸러 낸다.
"""
import re
from bs4 import BeautifulSoup
from .base import BaseAdapter

# 예고 표의 시간대 라벨 -> 등급 이름
_PEAK_TIER = {"PEAK": "peak", "OFF-PEAK": "off_peak"}
# 열 제목 -> 어느 단가인가
_COL_ITEM = (("INPUT TOKENS (CACHE HIT)", "cache_read"),
             ("INPUT TOKENS (CACHE MISS)", "input"),
             ("OUTPUT TOKENS", "output"))
# "take effect at 16:00 UTC on August 16, 2026" 에서 시행일을 읽는다.
# ★날짜를 코드에 박지 않는 이유 = 회사가 시행일을 미루거나 앞당긴다. 2026-08-13 에
#   Anthropic 이 9월 1일 인상 예고를 통째로 뺀 전례가 있다. 페이지에 적힌 것을 읽고,
#   못 읽으면 예고 표를 아예 안 받는다(날짜 없는 단가는 그날 단가로 오인된다)
_EFFECTIVE = re.compile(
    r"take\s+effect[^.]{0,60}?on\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}


def _is_scheduled(rows):
    """시간대별 요금 예고 표인가. 어느 칸이든 PEAK·OFF-PEAK 라벨이 있으면 그 표다."""
    return any(c.strip().upper() in _PEAK_TIER for cells in rows for c in cells)


class DeepSeekAdapter(BaseAdapter):
    provider = "DeepSeek"
    url = "https://api-docs.deepseek.com/quick_start/pricing"
    # 이 페이지의 단가는 전부 100만 토큰당이다. 다른 단위가 나오면 표가 바뀐 것
    accepted_units = ("per_1M_tokens",)


# ──────────────────────────────────────────────────────────────────────
# 6개사 중 유일한 HTML 원본(마크다운 미제공 실측, 2026-08-15).
# 값은 뒤에서 모델 수만큼 자른다(가격 행 앞 라벨 칸이 rowspan 으로 1-2개라
# 위치가 흔들린다). 명세 = 수집기준_결정항목.md DeepSeek 절.
# ──────────────────────────────────────────────────────────────────────

from .base import PriceRow


class _DeepSeekRows:

    def parse_rows(self, html):
        soup = BeautifulSoup(html, "lxml")
        all_rows = [[[c.get_text(" ", strip=True) for c in r.find_all(["th", "td"])]
                     for r in t.find_all("tr")] for t in soup.find_all("table")]
        rows = []
        rows += self._sched_rows(all_rows, soup)
        for tbl in all_rows:
            if _is_scheduled(tbl):
                continue
            models = None
            for cells in tbl:
                if cells and cells[0].strip().upper() == "MODEL":
                    models = [re.sub(r"\s*\(.*?\)\s*$", "", c).strip()
                              for c in cells[1:]]
                    break
            if not models:
                continue
            plan = [("INPUT TOKENS (CACHE HIT)", "cache_read"),
                    ("INPUT TOKENS (CACHE MISS)", "input"),
                    ("OUTPUT TOKENS", "output")]
            for cells in tbl:
                joined = " ".join(cells).upper()
                item = next((it for label, it in plan if label in joined), None)
                if item is None:
                    continue
                if len(cells) < len(models) + 1:
                    self._warns.append(
                        f"{self.provider}: '{cells[0][:30]}' 행 칸 부족 - 건너뜀")
                    continue
                for mdl, cell in zip(models, cells[-len(models):]):
                    v = self.price(cell, f"{mdl} {item}")
                    if v is not None:
                        rows.append(PriceRow(
                            provider=self.provider, model=mdl, item=item,
                            value=v, unit="per_1M_tokens",
                            category="Model Details", source_url=self.url))
            if rows:
                break
        return rows

    def _sched_rows(self, all_rows, soup):
        """시행 예고 표(피크·오프피크) -> 배수 아닌 금액 줄 + effective_from.
        시행 전까지는 적재층의 applies_on 이 걸러 낸다(공통 규칙)."""
        tbl = next((r for r in all_rows if _is_scheduled(r)), None)
        if not tbl:
            return []
        m = _EFFECTIVE.search(soup.get_text(" "))
        if not m or m.group(1).lower() not in _MONTHS:
            self._warns.append(
                f"{self.provider}: 예고 표의 시행일을 못 읽어 받지 않음")
            return []
        eff = "%s-%02d-%02d" % (m.group(3), _MONTHS[m.group(1).lower()],
                                int(m.group(2)))
        head = [c.upper() for c in tbl[0]]
        items = [(i, item) for i, c in enumerate(head)
                 for label, item in _COL_ITEM if label in c]
        if len(items) < 2:
            self._warns.append(f"{self.provider}: 예고 표 열 제목 인식 실패")
            return []
        out, model = [], None
        for cells in tbl[1:]:
            tier = next((_PEAK_TIER[c.strip().upper()] for c in cells
                         if c.strip().upper() in _PEAK_TIER), None)
            if tier is None or len(cells) < len(items) + 1:
                continue
            if len(cells) == len(items) + 2:
                model = cells[0].strip()
            if not model:
                continue
            for (_, item), cell in zip(items, cells[-len(items):]):
                v = self.price(cell, f"{model} {tier} {item}")
                if v is not None:
                    out.append(PriceRow(
                        provider=self.provider, model=model, item=item,
                        value=v, unit="per_1M_tokens", tier=tier,
                        effective_from=eff, category="Pricing Details",
                        source_url=self.url,
                        note=m.group(0)[:120]))
        return out


DeepSeekAdapter.parse_rows = _DeepSeekRows.parse_rows
DeepSeekAdapter._sched_rows = _DeepSeekRows._sched_rows
