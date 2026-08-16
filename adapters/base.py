"""공통 데이터 모델 + 회사별 수집 코드의 공통 바탕 + 수집/파싱 유틸.

모든 회사별 수집 코드는 BaseAdapter를 상속하고 parse_rows(text)만 구현한다.
단가는 PriceRow 한 줄 = 한 값이고 단위(unit)가 줄마다 붙는다.
"""
from __future__ import annotations
import gzip
import os
import re
from dataclasses import dataclass, asdict
from typing import Optional
import requests


class EmptyParseError(RuntimeError):
    """페이지는 받았는데 한 건도 파싱되지 않음.

    2026-07-28 도입. 그전에는 0개 파싱이 경고로만 남고 '성공'으로 기록됐다.
    성공으로 남으면 변동 비교에서 안 빠져서, 어제 있던 그 회사 모델이 전부
    '삭제'로 기록된다. 페이지에는 멀쩡히 있는데 이력에는 없어졌다고 남는다.
    예외로 만들면 재시도 3회를 거치고 실패로 확정되어, 이미 검증된 실패 경로
    (비교에서 제외)를 그대로 탄다.
    """

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# 2026-07-25 GOOGLEBOT_UA 제거: 검색엔진 크롤러 사칭이라 사업용으로 부적절.
# 실측상 Gemini 페이지는 일반 UA로도 200 OK + 가격 표 79개 확보되어 우회가 불필요.

from .pricetext import parse_prices  # noqa: E402  단위까지 읽는 정본 파서


@dataclass
class PriceRow:
    """한 줄이 한 값. "이 조건에서 이 항목은 얼마"를 뜻한다.

    2026-07-27 방침 전환으로 도입. 이전 구조(단가 여섯 개 고정 칸, 전부
    '100만 토큰당', 2026-08-16 삭제)는 항목마다 단위가 다르거나(Gemini 2.5
    Flash Image는 입력이 토큰당·출력이 장당) 자료형별로 가격이 갈리는 경우를
    담지 못했다.

    같은 모델이 여러 줄이 된다. 등급·문맥 구간·자료형·항목·단위의 조합마다 한 줄.
    """
    provider: str                       # 제공사, 예: 'Google'
    model: str                          # 모델 또는 서비스 이름
    item: str                           # 항목. 표준 목록 = 수집기준_결정항목.md §item·unit 표준
    value: float                        # 금액. multiplier 가 채워진 줄에서는 배수
    unit: str                           # per_1M_tokens · per_image · per_second 등. 배수 줄은 빈칸
    tier: str = "standard"              # standard / batch / flex / priority / peak 등 회사가 쓴 말
    context: str = "default"            # default / short / long / low / medium / high
    modality: Optional[str] = None      # text / audio / image / video / thinking
    currency: str = "USD"
    effective_from: Optional[str] = None  # 시기별 가격. 예: '2026-09-01'부터
    source_url: str = ""
    note: str = ""                      # 원문 조각. 값이 이상해 보일 때 되짚는 근거
    # ── 2026-08-15 확장 (수집기준_결정항목.md §검증 반영 보강) ──
    variant: str = ""                   # 같은 모델 안의 변형. 720p / 4k / fast / ultra 등
    cache_ttl: str = ""                 # 캐시 보관 기간. 5m / 1h
    region: str = "global"              # global(기본) / regional(지역 지정 할증) — 빈칸 안 씀
    multiplier: str = ""                # 채워지면 value 는 금액이 아니라 "여기 적힌 등급 단가의 배수"
    category: str = ""                  # 페이지 절 이름 원문(Flagship models 등). 알림 필터 근거

    def key(self) -> tuple:
        """같은 값을 가리키는 축의 조합. 변동 비교의 단위.

        category 는 일부러 뺀다 — 회사가 모델을 다른 절로 옮겨도(gpt-5.5-cyber 가
        여드레 사이 Specialized → Cyber 로 이동한 실측) 같은 값의 이력이 안 갈라지게.
        """
        return (self.provider, self.model, self.tier, self.context,
                self.modality or "", self.variant, self.cache_ttl,
                self.region, self.multiplier, self.item, self.unit,
                self.effective_from or "")

    def to_dict(self) -> dict:
        return asdict(self)

def save_page(page_dir, provider, text, suffix="html"):
    """그날 받은 페이지를 압축해 남긴다. 반환: 저장 경로.

    2026-07-28 도입. 그전에는 받은 페이지를 파싱하고 버렸다. 새벽에 수집이 이상해도
    아침에 페이지를 열면 이미 그때 내용이 아니라(회사가 고쳤거나 지역별로 다르거나)
    원인을 찾을 수 없었다. 남는 건 '3개 잡혔다'는 숫자뿐이었다.
    용량 실측: 하루 6개 2.9MB -> 압축 0.46MB, 1년 168MB.

    suffix: 'html' 또는 'md'. 마크다운 수집(2026-08-15)부터 둘 다 보관한다 —
    파싱 원본(md)이 있어야 재파싱 대조가 되고, HTML 은 사람이 화면을 되짚는 용도.
    """
    os.makedirs(page_dir, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", provider).lower()
    path = os.path.join(page_dir, f"{safe}.{suffix}.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)
    return path


class BaseAdapter:
    provider = "Base"
    url = ""
    # 공식 제공 마크다운 주소(2026-08-15). 선언한 회사는 이것이 파싱 원본이고
    # url(HTML)은 보관 전용이 된다. 주소 규칙이 회사마다 다르다 — OpenAI/Anthropic/
    # xAI/Perplexity = 뒤에 .md, Google = 뒤에 .md.txt. 없다고 판정하기 전에
    # 그 사이트 llms.txt 를 볼 것(수집기준_결정항목.md 실측)
    md_url = ""
    user_agent = BROWSER_UA
    # ★이 회사 표에서 값으로 받을 단위(허용 목록). 여기 없는 단위는 값으로 쓰지
    #   않고 경고를 남긴다. 막을 것을 나열하지 않고 받을 것을 나열하는 이유 =
    #   회사가 새 상품을 표에 끼워 넣어도 선언에 없으면 자동으로 걸린다.
    #   회사마다 표기가 달라 공통 목록으로는 못 거른다(2026-08-14 실측:
    #   Anthropic 은 '/ MTok' 하나, Google 은 시간당·장당·검색질의당이 한 표에
    #   섞이고, OpenAI 는 값이 속성 안 JSON 이라 단위 표기가 아예 없다).
    accepted_units = ("per_1M_tokens",)
    # 토큰당이 아닌 것을 알고 있고, 이 여섯 칸에 안 넣기로 한 단위. 경고를 안 낸다.
    # 선언한 적 없는 단위만 경고가 되게 해서, 매일 나오는 정상 항목이 경고를 채워
    # 진짜 신호를 묻는 것을 막는다(2026-08-14: Google 이미지 단가가 매일 걸렸다)
    known_other_units = ()

    def __init__(self):
        # 값으로 안 받은 칸을 여기 쌓아 run_rows 가 경고로 돌려준다. 조용히 버리면
        # 표가 바뀌어 수집이 줄어도 아무 데도 안 남는다(2026-08-12 사고)
        self._warns = []

    # 이번 수집에서 받은 페이지의 정황(응답 코드·크기·저장 경로). run()이 매번 새로 채운다.
    # 개수만 남기면 나중에 왜 줄었는지 못 찾는다. 크기만 비교해도 상당수가 그 자리에서 풀린다
    # (219KB가 12KB면 페이지 구조 변경, 크기 그대로인데 개수만 줄면 값 표기 변경 쪽)
    fetch_info: dict = {}

    def fetch(self) -> str:
        return self._get(self.url)

    def _get(self, url) -> str:
        headers = {"User-Agent": self.user_agent,
                   "Accept-Language": "en-US,en;q=0.9"}
        resp = requests.get(url, headers=headers, timeout=45)
        resp.raise_for_status()
        # 인코딩을 헤더 선언 -> 본문 추정 순으로 확정한다. 기본값(ISO-8859-1)에
        # 맡기면 선언 없는 페이지가 깨진다(DeepSeek 저장본 mojibake 실측, 2026-08-13)
        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        self.fetch_info = {"http": resp.status_code, "bytes": len(resp.content)}
        return resp.text

    def price(self, text, where=""):
        """단가 셀 하나를 금액으로. 받을 단위가 아니면 None 과 경고.

        단위를 읽는 parse_prices 를 쓰고, 어느 단위를 받을지는 회사가
        accepted_units 로 선언한다. '$3.00 / hr' 같은 다른 단위는 안 받는다.

        where: 경고에 적을 자리 이름(행 라벨 등). 어느 칸에서 났는지 못 찾으면
        경고를 받고도 고칠 곳을 못 찾는다.
        """
        vals = parse_prices(text)
        if not vals:
            return None
        for v in vals:
            if v.unit in self.accepted_units:
                return v.value
        units = {v.unit for v in vals}
        if units <= set(self.known_other_units):
            return None       # 알고 있는 다른 단위. 여기 안 넣기로 한 것이라 조용히 넘긴다
        got = ", ".join(sorted(units - set(self.known_other_units)))
        self._warns.append(
            f"{self.provider}: {where or '단가 칸'}의 '{str(text)[:30]}'은(는) "
            f"단위가 {got}라 받지 않음 - 토큰당 단가가 아니거나 표가 바뀐 것")
        return None

    def parse_rows(self, text):
        """원본 글자 -> PriceRow 목록. 회사별 어댑터가 구현."""
        raise NotImplementedError

    def run_rows(self, text: Optional[str] = None, page_dir: Optional[str] = None):
        """수집 실행. text 를 주면 그걸로 파싱(오프라인), 아니면 네트워크 수집.

        page_dir: 주면 받은 페이지를 거기에 압축 저장한다(라이브 수집일 때만).
        파싱 전에 저장한다. 파싱이 터져도 그때 받은 페이지는 남아야 원인을 찾는다.

        마크다운 회사(md_url 선언)는 마크다운이 파싱 원본이고, 보관은 HTML 과
        마크다운 둘 다 한다(결정란 8 — 재파싱 대조는 md, 사람 되짚기는 html).
        HTML 보관이 실패해도 그날 수집은 계속한다(값이 우선).
        """
        self.fetch_info = {}
        self._warns = []
        if text is None:
            src = self.md_url or self.url
            text = self._get(src)
            info = self.fetch_info      # 파싱 원본의 정황. 아래 HTML 보관용 _get 이 덮어쓰지 않게 붙잡아 둔다
            if page_dir:
                try:
                    kind = "md" if self.md_url else "html"
                    info["page"] = save_page(page_dir, self.provider, text, kind)
                    if self.md_url:
                        html = self._get(self.url)   # 이 호출이 self.fetch_info 를 새 dict 로 바꾼다
                        info["html_bytes"] = self.fetch_info.get("bytes")
                        info["html_page"] = save_page(page_dir, self.provider, html, "html")
                except Exception as e:
                    info["page_error"] = f"{type(e).__name__}: {e}"
            # [수정 2026-08-15] 마크다운 회사에서 fetch_info 가 HTML 것(크기·보관 경로 없음)으로
            # 남던 것을 파싱 원본 것으로 되돌린다. 로그의 크기·보관 경로가 실제 원본을 가리켜야 한다
            self.fetch_info = info
        rows = self.parse_rows(text)
        if not rows:
            raise EmptyParseError(f"{self.provider}: 페이지는 받았으나 0개 파싱")
        return rows, list(self._warns)
