"""보관한 그날 페이지에서 원문 조각을 뽑아 전날과 대조한다.

값이 바뀐 날 "우리가 잘못 읽은 것인지, 페이지가 원래 그런지"를 사람이 눈으로
가릴 수 있게 근거를 남긴다(설계 = docs_아카이브_0816/DB설계_0727.md). 파서를 안 거치고 글자만 찾아
오려내므로, 표 고르기가 틀려도 이 경로는 영향을 안 받는다.

DB에는 안 넣는다(2026-08-10 사용자 결정). data/excerpts/YYYY-MM-DD.json 으로만
남긴다. data/changes/ 에 끼워 넣지 않는 이유 = 그 파일은 대시보드·메일·price_change
표로 흘러가서 조각 300자가 거기까지 딸려 간다.
"""
from __future__ import annotations
import gzip
import json
import os
import re

from bs4 import BeautifulSoup

BASE = os.path.dirname(os.path.abspath(__file__))
PAGE_DIR = os.path.join(BASE, "data", "pages")
EXCERPT_DIR = os.path.join(BASE, "data", "excerpts")

# 모델 이름 앞뒤로 몇 글자를 오려낼 것인가.
# ★500자인 이유 = 실측(2026-08-10, 6개사 60모델). "조각 안에 그 모델의 표준 입력
#   단가가 들어 있나"를 반경별로 셌다.
#     100자 32개 · 200자 35개 · 300자 53개 · 400자 59개 · 500자 60개(전부)
#   회사마다 이름과 값 사이에 모델 코드·설명 문단·표 머리글이 끼어 있어서 좁으면
#   자기 단가에 못 닿는다.
# ★단가가 조각 밖이면 진짜 가격 변동에도 조각이 안 움직여서, collect() 가 그것을
#   "원문이 그대로인데 값만 바뀜 = 파서 오류 의심"으로 잘못 신고한다. 300자일 때
#   60개 중 7개가 그랬다(2026-08-10 검증 지적, 실측으로 재확인).
# 조각은 파일(data/excerpts/)에만 넣으므로 길이 제한이 없다. 평균 1,027자.
SPAN = 500


# 오프라인 시험이 읽을 보관 페이지 날짜. run.py --offline · test_adapters.py ·
# audit.py --offline 셋이 이 한 줄을 같이 본다. 페이지를 새 날짜로 옮기려면
# 여기만 고친다.
# ★날짜를 고정하는 이유 = 시험이 기대하는 값이 그날 페이지 기준이다. 최신 날짜를
#   자동으로 따라가게 하면 회사가 값을 바꾼 날 시험이 통째로 깨진다.
# 2026-08-15: 새 경로 고정본(마크다운 6개사 + HTML) 날짜로 옮김
FIXTURE_DATE = "2026-08-15"


def page_path(date_str, provider, kind="html"):
    """그날 그 회사 페이지 사본 경로. adapters.base.save_page 와 같은 이름 규칙.
    kind = html(보관·되짚기용) / md(마크다운 회사의 파싱 원본)."""
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", provider).lower()
    return os.path.join(PAGE_DIR, date_str, f"{safe}.{kind}.gz")


def fixture_source(adapter, date_str=None):
    """오프라인 시험이 읽을 그 회사의 파싱 원본(마크다운 회사는 md, 아니면 html).
    run.py --offline · audit.py --offline 이 쓴다. 없으면 FileNotFoundError."""
    kind = "md" if getattr(adapter, "md_url", "") else "html"
    path = page_path(date_str or FIXTURE_DATE, adapter.provider, kind)
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
        return f.read()


def fixture_html(provider, date_str=None):
    """오프라인 시험이 읽을 그 회사 페이지 원문. 없으면 FileNotFoundError.

    2026-08-10 이전에는 data/fixtures/ 에 압축 안 한 사본을 따로 뒀다. 매일 받는
    data/pages/ 와 같은 것인데 갱신이 따로여서 낡아 갔다(6개 중 5개가 2026-07-16
    고정, xAI 만 07-27). 같은 것을 두 벌 두지 않는다.
    """
    path = page_path(date_str or FIXTURE_DATE, provider)
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
        return f.read()


# [추가 2026-08-16] 회사 -> 파싱 원본 종류. 어댑터의 md_url 선언과 같은 규칙
_PARSE_KIND = None


def parse_kind(provider):
    """그 회사의 파싱 원본 종류('md' 또는 'html'). 값을 읽는 어댑터와 같은 규칙."""
    global _PARSE_KIND
    if _PARSE_KIND is None:
        from adapters.registry import ALL_ADAPTERS
        _PARSE_KIND = {ad.provider: ("md" if getattr(ad, "md_url", "") else "html")
                       for ad in ALL_ADAPTERS}
    return _PARSE_KIND.get(provider, "html")


def _read_page(date_str, provider, kind):
    """그날 보관 파일 하나를 읽는다. 파일이 없으면 None. 못 읽으면 예외."""
    path = page_path(date_str, provider, kind)
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
        return f.read()


def page_html(date_str, provider):
    """보관 페이지 원문(태그 그대로). 파일이 없으면 None. 못 읽으면 예외."""
    return _read_page(date_str, provider, "html")


def flatten(html):
    """태그를 떼고 공백을 한 칸으로 줄인 텍스트.

    공백을 줄이는 이유 = 줄바꿈·들여쓰기가 날마다 달라도 같은 조각으로 보여야
    전날과 대조가 된다.
    """
    return " ".join(BeautifulSoup(html, "lxml").get_text(" ").split())


def page_text(date_str, provider):
    """파싱 원본에서 뽑은 한 줄 텍스트. 파일이 없거나 못 읽으면 None.

    [수정 2026-08-16] 보는 파일을 HTML 고정에서 그 회사의 파싱 원본(마크다운
    회사는 md, DeepSeek 는 html)으로 바꿈. 값을 여기서 읽었으니 근거 조각과
    전날 대조도 같은 글자를 봐야 한다. 실측: OpenAI 새 페이지는 HTML 쪽에
    모델 85개 중 30개만 있어 나머지는 대조가 아예 안 됐다.
    읽는 방법은 어댑터와 독립(파서를 안 거치고 글자만 오려 문자열 비교).

    HTML 은 태그를 뗀다 - 이름이 <span> 등으로 쪼개져 있으면 원문에서 안
    찾아진다. 마크다운은 태그가 없어 공백만 한 칸으로 접는다(flatten 과 같은
    이유 - 줄바꿈이 날마다 달라도 같은 조각으로 보여야 전날과 대조가 된다).
    """
    kind = parse_kind(provider)
    try:
        raw = _read_page(date_str, provider, kind)
    except Exception as e:
        print(f"  [원문] {date_str} {provider} 파싱 원본({kind})을 "
              f"못 읽음({type(e).__name__})")
        return None
    if raw is None:
        return None
    return " ".join(raw.split()) if kind == "md" else flatten(raw)


def seen_in_page(date_str, provider, names):
    """그날 보관본에 아직 이름이 남아 있는 것만 고른다.

    반환: (남아 있는 이름 집합, 못 본 사유 또는 None)

    [수정 2026-08-16] 파싱 원본(마크다운 회사는 md)을 먼저 보고, HTML 보관본도
    있으면 같이 본다. 어느 쪽에든 이름이 있으면 '남아 있음'(= 삭제 의심 유지).
    md 에서 빠졌는데 HTML 화면에 남은 경우는 회사의 삭제가 아니라 원본 선택
    문제일 수 있어서다. 파싱 원본이 없으면 확인 못 한 것으로 돌려준다 -
    HTML 만 봐서는 md 회사의 삭제 여부를 말할 수 없다(OpenAI 실측 30/85).

    ★HTML 은 원문과 태그 뗀 텍스트를 둘 다 본다. 2026-08-12 OpenAI 가 가격
      데이터를 <astro-island props="..."> 속성 안 JSON 으로 옮겼는데, 속성 안
      글자는 get_text() 로 안 나온다. 그날 실측: gpt-5.4-nano 가 원문 3회 ·
      텍스트 0회. 거꾸로 이름이 <span> 으로 쪼개진 페이지는 텍스트 쪽에서만 잡힘.
    """
    kind = parse_kind(provider)
    try:
        src = _read_page(date_str, provider, kind)
    except Exception as e:
        return set(), f"{type(e).__name__}: {e}"
    if src is None:
        return set(), f"파싱 원본({kind}) 보관 없음"
    # [추가 2026-08-16 검증 반영] 빈 파일 = 확인 불가. 그냥 지나가면 전 모델이
    #   사유 없이 '진짜 없음'이 되어 DB 비고까지 적힌다
    if not src.strip():
        return set(), f"파싱 원본({kind})이 비어 있음"
    # [추가 2026-08-16 검증 반영] 공백 접은 판도 같이 본다. page_text(조각 층)와
    #   같은 접기 - 이름이 줄바꿈으로 갈리면 두 층의 판정이 어긋난다
    texts = [src, " ".join(src.split())]
    if kind == "md":
        try:
            html = page_html(date_str, provider)
        except Exception:
            html = None
        if html:
            texts.append(html)
    else:
        html = src
    if html:
        try:
            texts.append(flatten(html))
        except Exception:
            pass
    return {n for n in names
            if any(occurrences(t, n) for t in texts)}, None


def occurrences(text, name):
    """이름이 나오는 자리 전부. 뒤에 글자나 붙임표가 이어지면 뺀다.

    빼는 이유 = 짧은 이름이 긴 이름의 앞부분으로 걸린다. 2026-08-10 실측 반례:
    'Sonar Pro' 가 'Sonar Prompt Guide' 에, 'Gemini 3.5 Flash' 가
    'Gemini 3.5 Flash-Lite' 에 걸렸다.
    """
    out, i = [], text.find(name)
    while i >= 0:
        after = text[i + len(name):i + len(name) + 1]
        if not (after.isalnum() or after == "-"):
            out.append(i)
        i = text.find(name, i + 1)
    return out


def find_excerpt(text, model, span=SPAN):
    """텍스트에서 모델 이름을 찾아 앞뒤 span자를 오려낸다. 못 찾으면 None.

    이름 그대로 못 찾으면 적용 시기 문구를 뗀 이름으로 한 번 더 찾는다
    ('Claude Sonnet 5 through August 31, 2026' -> 'Claude Sonnet 5').
    그래도 없으면 대소문자를 무시하고 찾는다. 페이지가 표기를 바꾸는 일이 잦다.

    ★이름이 여러 번 나오면 단가가 가장 많이 담긴 자리를 고른다. 첫 자리를 그냥
    쓰면 차례·안내 문구를 잡는다. 2026-08-10 실측 반례: 'Claude Opus 5' 의 첫
    자리가 "What's new in Claude Opus 5" 였고 그 근처에 단가가 없었다.
    """
    if not text or not model:
        return None
    import db
    names = [model]
    base = db.split_period(model)[0]
    if base != model:
        names.append(base)
    low = None
    for name in names:
        hits = occurrences(text, name)
        if not hits:
            if low is None:
                low = text.lower()
            hits = occurrences(low, name.lower())
        if not hits:
            continue
        best = max(hits, key=lambda i:
                   text[max(0, i - span):i + len(name) + span].count("$"))
        s, e = max(0, best - span), min(len(text), best + len(name) + span)
        return ("..." if s else "") + text[s:e] + ("..." if e < len(text) else "")
    return None


def collect(date_str, changes, prev_by_provider):
    """변동이 있는 모델마다 그날 조각과 전날 조각을 모은다.

    반환: (items, problems)
      items    = data/excerpts/날짜.json 에 넣을 목록. 한 항목이 한 모델
      problems = 확인 필요에 올릴 사유 문장들

    한 모델의 줄 여럿이 같이 바뀌어도 항목은 하나다. 조각이 모델 이름 주변이라
    여러 줄이 같은 글자를 본다(2026-08-01 이 12건 = 2모델 x 6칸이었다).

    종류별로 하는 일이 다르다.
      changed - 그날·전날 둘 다 뽑아 대조한다. 여기서만 파서 오류를 의심할 수 있다
      added   - 그날만. 전날에 없던 모델이라 대조할 것이 없다
      removed - 전날만. 오늘 목록에 없는 것이 사실이라 그날 못 찾는 게 정상이다
    """
    by_model = {}
    for c in changes:
        e = by_model.setdefault((c["provider"], c["model"]),
                                {"kinds": set(), "fields": set()})
        e["kinds"].add(c["kind"])
        # 2026-08-15 새 변동 형식은 항목 이름이 'item' 칸(input·output·cache_read 등)
        if c.get("item"):
            e["fields"].add(c["item"])

    cache = {}

    def text_of(d, prov):
        if (d, prov) not in cache:
            cache[(d, prov)] = page_text(d, prov)
        return cache[(d, prov)]

    items, problems, no_page, no_prev = [], [], set(), set()
    for (prov, model), e in sorted(by_model.items()):
        kinds = e["kinds"]
        pd = prev_by_provider.get(prov)
        item = {"provider": prov, "model": model,
                "kinds": sorted(kinds), "changed_fields": sorted(e["fields"]),
                "today": None, "prev_date": pd, "prev": None,
                "found": None, "same_as_prev": None}

        want_today = bool(kinds & {"changed", "added"})
        want_prev = bool(kinds & {"changed", "removed"})

        if want_today:
            text = text_of(date_str, prov)
            if text is None:
                if prov not in no_page:
                    no_page.add(prov)
                    problems.append(f"{prov}: 그날 보관 페이지가 없어 원문 조각을 못 뽑음")
            else:
                item["today"] = find_excerpt(text, model)
                item["found"] = item["today"] is not None
                if not item["found"]:
                    problems.append(f"{prov} {model}: 그날 원문에서 모델 이름을 "
                                    "못 찾아 대조 못 함")

        if want_prev:
            if pd is None:
                problems.append(f"{prov} {model}: 비교 기준 날짜가 없어 전날 원문과 "
                                "대조 못 함")
            else:
                ptext = text_of(pd, prov)
                if ptext is None:
                    # 페이지 하나가 없는 것이라 모델마다 같은 문장을 되풀이하지
                    # 않는다. 2026-07-29 재현 = xAI 4모델에 같은 줄 4개
                    if (prov, pd) not in no_prev:
                        no_prev.add((prov, pd))
                        problems.append(f"{prov}: 전날({pd}) 보관 페이지가 없어 "
                                        "원문 대조 못 함")
                else:
                    item["prev"] = find_excerpt(ptext, model)
                    # 사라진 모델은 전날에 있는 게 맞다. 못 찾으면 대조를 못 한 것이라
                    # 조용히 넘기지 않는다(2026-08-10 검증 지적)
                    if item["prev"] is None:
                        problems.append(f"{prov} {model}: 전날({pd}) 원문에서 모델 "
                                        "이름을 못 찾아 대조 못 함")

        # ★파서 오류 의심. 페이지 글자가 그대로인데 우리가 읽은 값만 바뀌었다면
        #   페이지가 아니라 우리 쪽이 바뀐 것이다. 2026-08-01 에 고친 표 고르기
        #   취약점이 정확히 이 종류였다(할증가 표를 표준가로 읽음).
        if "changed" in kinds and item["today"] and item["prev"]:
            item["same_as_prev"] = item["today"] == item["prev"]
            if item["same_as_prev"]:
                problems.append(f"{prov} {model}: 원문이 전날({pd})과 같은데 값이 "
                                "바뀜 - 파서 오류 의심")
        items.append(item)
    return items, problems


def save(date_str, items, prev_by_provider):
    """data/excerpts/YYYY-MM-DD.json 에 남긴다. 반환: 저장 경로."""
    os.makedirs(EXCERPT_DIR, exist_ok=True)
    path = os.path.join(EXCERPT_DIR, f"{date_str}.json")
    payload = {"date": date_str, "baselines": prev_by_provider, "items": items}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def provider_urls():
    """회사 이름 -> 공식 가격 페이지 주소.

    provider.pricing_url 이 아니라 수집 코드의 url 을 쓰는 이유 = 수집이 실패한
    회사도 주소를 알 수 있고 DB 없이도 된다. 메일은 DB 적재가 실패해도 나가야 한다.
    """
    from adapters.registry import ALL_ADAPTERS
    return {ad.provider: ad.url for ad in ALL_ADAPTERS if getattr(ad, "url", "")}
