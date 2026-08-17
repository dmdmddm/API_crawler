"""자족형(self-contained) HTML 대시보드 생성 (2026-08-15 새 줄 구조판).

구성: 요약 지표 -> 확인 필요 배너 -> 변동 배너 -> 변경 이력 -> 가격 추이 -> 제공사별 가격 표 -> 수집 상태.
외부 리소스 없이 단일 HTML로 완결(사내 웹페이지에 그대로 삽입 가능).

표시 설계(2026-08-15 개정 — 저장본이 값마다 한 줄인 PriceRow 목록으로 바뀜):
- 회사마다 **주요 모델**(mailer.FEATURED — 최신 판과 그 직전 판)과 오늘 바뀐 줄만
  펼치고, 나머지(구세대·특수 목적·Flex·Fast mode·Multimodal·Finetuning 등)는 접는다.
- 표는 (모델 · 등급 · 문맥 구간 · 변형 · 지역) 을 한 행으로 하고 항목(입력·캐시 읽기·
  캐시 쓰기·출력·…)을 열로 편다. 자료형·캐시 기간은 열 이름에 붙인다(음성 입력·5m 캐시 쓰기).
  단위가 100만 토큰당이 아닌 값은 칸 안에 단위를 같이 적고, 배수 줄은 '×0.8 standard' 로 적는다.
- 접고 펼치는 동작은 JS 없이 HTML <details>로 한다(사내 페이지 삽입 시 충돌 없음).
- 그날 값이 바뀐 칸은 변동률과 색으로, 새로 생긴 줄은 왼쪽 표시로 구분한다.
"""
from __future__ import annotations
import json
import os
import tempfile
import html as _html

BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, "public")

PROVIDER_ORDER = ["Anthropic", "OpenAI", "Google", "xAI", "Perplexity", "DeepSeek"]
PROVIDER_LABEL = {"Google": "Google (Gemini)", "xAI": "xAI (Grok)",
                  "Anthropic": "Anthropic (Claude)", "OpenAI": "OpenAI (GPT)",
                  "Perplexity": "Perplexity (Sonar)"}

# 항목의 화면 표기. 표 열 이름·변경 이력·추이 그래프가 같이 쓴다
ITEM_KO = {
    "input": "입력", "output": "출력", "cache_read": "캐시 읽기", "cache_write": "캐시 쓰기",
    "cache_storage": "캐시 보관", "generation": "생성", "training": "학습",
    "session": "세션", "tool_call": "도구 호출", "citation": "인용 토큰",
    "search_query": "검색 질의", "reasoning": "추론 토큰", "transcription": "전사",
    "storage": "저장", "egress": "내려받기", "grounding": "그라운딩",
}
# 추이 그래프의 화면 코드(db.HISTORY_KINDS 와 짝) -> 표기
FIELD_KO = {
    "input": "입력", "output": "출력",
    "cache_read": "캐시 읽기", "cache_write": "캐시 쓰기",
    "batch_input": "배치 입력", "batch_output": "배치 출력",
}
# 열 순서. 여기 없는 항목은 그 뒤에 이름순
ITEM_ORDER = ("input", "cache_read", "cache_write", "output", "cache_storage",
              "generation", "training", "session", "tool_call")
# 단위 짧은 표기(100만 토큰당은 기본이라 안 적는다)
UNIT_KO = {
    "per_1M_tokens": "", "per_1M_characters": "/1M자", "per_1M_tokens_per_hour": "/1M토큰·시간",
    "per_image": "/장", "per_second": "/초", "per_minute": "/분", "per_hour": "/시간",
    "per_song": "/곡", "per_session": "/세션", "per_session_per_hour": "/세션·시간",
    "per_call": "/건", "per_1K_call": "/1천 건", "per_1K_request": "/1천 건",
    "per_1K_request_per_image": "/1천 건·장", "per_GiB": "/GiB", "per_GiB_per_day": "/GiB·일",
    "per_GB_per_day": "/GB·일", "per_month": "/월",
}
MODALITY_KO = {"text": "글자", "audio": "음성", "image": "이미지", "video": "영상"}
CONTEXT_KO = {"default": "", "short": "20만 토큰 이하", "long": "20만 토큰 초과",
              "low": "검색 low", "medium": "검색 medium", "high": "검색 high"}


def _esc(s):
    return _html.escape(str(s))


def _fmt(v):
    """저장된 값을 그대로 보여준다. 자리에서 잘라내지 않는다(DeepSeek 캐시 0.003625 실측)."""
    if v is None:
        return "-"
    if v == 0:
        return "$0"
    s = f"{v:.10f}".rstrip("0").rstrip(".")
    return f"${s}"


def _delta(pct):
    if pct is None:
        return ""
    cls = "up" if pct > 0 else "down"
    arrow = "▲" if pct > 0 else "▼"
    return f'<span class="delta {cls}">{arrow}&nbsp;{abs(pct)}%</span>'


def _cond(r):
    """줄의 조건 표기(등급 뒤에 붙는 것): 문맥 구간 · 변형 · 지역. 자료형·캐시 기간은 열 쪽."""
    bits = []
    ctx = CONTEXT_KO.get(r.get("context") or "default", r.get("context") or "")
    if ctx:
        bits.append(ctx)
    if r.get("variant"):
        bits.append(str(r["variant"]))
    if (r.get("region") or "global") != "global":
        bits.append(str(r["region"]))
    return " · ".join(bits)


def _col_key(r):
    """열 = (항목, 자료형, 캐시 기간). 순서는 ITEM_ORDER -> 자료형 -> 캐시 기간."""
    item = r.get("item") or ""
    return (item, r.get("modality") or "", r.get("cache_ttl") or "")


def _col_label(col):
    item, mod, ttl = col
    name = ITEM_KO.get(item, item)
    if mod:
        name = f"{MODALITY_KO.get(mod, mod)} {name}"
    if ttl:
        name = f"{name} {ttl}"
    return name


def _col_sort(col):
    item = col[0]
    pos = ITEM_ORDER.index(item) if item in ITEM_ORDER else len(ITEM_ORDER)
    return (pos, item, col[1], col[2])


def _cell(r, chg):
    """값 칸. 배수 줄은 ×배수, 단위가 토큰당이 아니면 단위 표기. 오늘 변동이면 변동률."""
    if r.get("multiplier"):
        txt = f"×{r['value']:g} {r['multiplier']}"
    else:
        u = UNIT_KO.get(r.get("unit") or "", r.get("unit") or "")
        txt = _fmt(r["value"]) + (f'<span class="unit">{_esc(u)}</span>' if u else "")
    if chg is None:
        return f'<td class="num">{txt}</td>'
    if chg["kind"] == "added":
        return f'<td class="num"><span class="pill new">신규</span> {txt}</td>'
    mark = ' <span class="pill spike">큰 변동</span>' if chg.get("spike") else ""
    return (f'<td class="num chg"><span class="old">{_fmt(chg.get("old_value"))}</span> '
            f'{txt} {_delta(chg.get("pct"))}{mark}</td>')


def _pivot(rows, cmap):
    """줄 목록 -> 표 HTML. 행 = (모델, 등급, 조건), 열 = 항목(자료형·캐시 기간 포함)."""
    if not rows:
        return ""
    cols = sorted({_col_key(r) for r in rows}, key=_col_sort)
    grid = {}
    order = []
    for r in rows:
        rk = (r["model"], r.get("tier") or "standard", _cond(r), r.get("effective_from") or "")
        if rk not in grid:
            grid[rk] = {}
            order.append(rk)
        grid[rk][_col_key(r)] = r
    head = ("<tr><th>모델</th><th>등급</th><th>조건</th>"
            + "".join(f'<th class="num">{_esc(_col_label(c))}</th>' for c in cols) + "</tr>")
    body = []
    for rk in order:
        model, tier, cond, ef = rk
        cells = grid[rk]
        row_changed = any(_ckey(r) in cmap for r in cells.values())
        cls = ' class="changed"' if row_changed else ""
        name = _esc(model) + (f' <span class="period">{_esc(ef)}부터</span>' if ef else "")
        tds = "".join(_cell(cells[c], cmap.get(_ckey(cells[c]))) if c in cells
                      else '<td class="num">-</td>' for c in cols)
        body.append(f"<tr{cls}><td>{name}</td><td>{_esc(tier)}</td>"
                    f'<td class="cond">{_esc(cond)}</td>{tds}</tr>')
    return f'<div class="tbl-scroll"><table>{head}{"".join(body)}</table></div>'


def _ckey(d):
    """줄과 변동 항목을 맞추는 키 = diff.row_key 와 같은 축."""
    import diff
    return diff.row_key(d)


def _provider_tables(rows, changes):
    import mailer
    cmap = {_ckey(c): c for c in changes if c["kind"] in ("changed", "added")}
    by = {}
    for r in rows:
        by.setdefault(r["provider"], []).append(r)
    out = []
    for prov in PROVIDER_ORDER + sorted(set(by) - set(PROVIDER_ORDER)):
        rs = by.get(prov)
        if not rs:
            continue
        # 주요 모델 = FEATURED 목록(mailer) + 오늘 값이 바뀌었거나 처음 등장한 줄.
        # 뒤 조건이 없으면 목록 밖 모델의 인하·인상이 접힌 표에 묻힌다(2026-08-16 사용자)
        def _show(r):
            return (mailer.is_featured(prov, r["model"], r.get("category"), r.get("tier"))
                    or _ckey(r) in cmap)
        rep = [r for r in rs if _show(r)]
        rest = [r for r in rs if not _show(r)]
        url = rs[0].get("source_url") or ""
        title = (f'<h3><span>{_esc(PROVIDER_LABEL.get(prov, prov))} '
                 f'<span class="cnt">주요 모델 {len(rep)}건 (전체 {len(rs)}건)</span></span>'
                 + (f'<a href="{_esc(url)}" target="_blank" rel="noopener">공식 페이지 ↗</a>' if url else "")
                 + "</h3>")
        more = ""
        if rest:
            cats = sorted({r.get("category") or "" for r in rest} - {""})
            hint = ", ".join(cats[:4]) + (" 외" if len(cats) > 4 else "")
            more = (f'<details class="more"><summary>나머지 {len(rest)}건 펼치기'
                    f'<span class="hint">{_esc(hint)}</span></summary>{_pivot(rest, cmap)}</details>')
        out.append(f'<div class="card">{title}{_pivot(rep, cmap) if rep else _pivot(rest, cmap)}'
                   f'{more if rep else ""}</div>')
    return '<div class="eyebrow section">제공사별 단가</div>' + "".join(out)


def _kpi(n, label, hot=False):
    return (f'<div class="kpi{" hot" if hot else ""}">'
            f'<div class="n">{n}</div><div class="l">{_esc(label)}</div></div>')


def _banner(changes, prev_date, date_str=None):
    if prev_date is None:
        return ('<div class="banner calm"><span class="dot"></span>'
                f'{_esc(date_str or "")} 처음 수집. 변동 알림은 다음 수집부터 표시됩니다.</div>')
    if not changes:
        return ('<div class="banner calm"><span class="dot"></span>'
                f'직전 수집({_esc(prev_date)}) 대비 가격 변동이 없습니다.</div>')
    return ('<div class="banner alert"><span class="dot"></span>'
            f'직전 수집({_esc(prev_date)}) 대비 변동 {len(changes)}건 감지. 아래 변경 이력을 확인하세요.</div>')


def _feed_order(c):
    # 큰 변동(0) -> 상승(1) -> 하락(2) -> 신규(3) -> 사라짐(4). 같은 묶음 안에서는 큰 폭 먼저.
    if c["kind"] == "changed":
        pct = c.get("pct") or 0
        if c.get("spike"):
            return (0, -abs(pct))
        return (1 if pct > 0 else 2, -abs(pct))
    return (3 if c["kind"] == "added" else 4, 0)


def _line_desc(c):
    """변동 한 건의 줄 설명: 모델 · 등급 · 조건 · 열 이름."""
    bits = [_esc(c["model"])]
    if (c.get("tier") or "standard") != "standard":
        bits.append(_esc(c["tier"]))
    cond = _cond(c)
    if cond:
        bits.append(_esc(cond))
    bits.append(_esc(_col_label(_col_key(c))))
    return " · ".join(bits)


MAX_FEED = 60
# [추가 2026-08-17 사용자] 처음부터 펼쳐 두는 변경 이력 건수. 나머지는 접는다 -
# 2026-08-17 DeepSeek 요금제 개편 때 30건이 한 번에 나와 화면이 이력으로 찼다.
# 접히는 쪽이 항상 덜 중요하다: 정렬이 큰 변동 -> 오른 것 -> 내린 것 -> 새로 등록 순
FEED_OPEN = 5


def _feed_item(c):
    """변동 한 건의 <li>."""
    spike = ' <span class="pill spike">큰 변동</span>' if c.get("spike") else ""
    desc = _line_desc(c)
    prov = f'<span class="prov">{_esc(c["provider"])}</span>'
    if c["kind"] == "changed":
        cls = "up" if (c.get("pct") or 0) > 0 else "down"
        return (f'<li class="{cls}">{prov} · {desc} <span class="old">{_fmt(c["old_value"])}</span> → '
                f'<span class="new-v">{_fmt(c["new_value"])}</span> {_delta(c.get("pct"))}{spike}</li>')
    if c["kind"] == "added":
        return f'<li class="new">{prov} · {desc} 새로 생김 ({_fmt(c["new_value"])})</li>'
    return f'<li class="rm">{prov} · {desc} 사라짐 (직전 {_fmt(c["old_value"])})</li>'


def _feed(changes):
    if not changes:
        return ""
    shown = sorted(changes, key=_feed_order)[:MAX_FEED]
    items = [_feed_item(c) for c in shown]
    over = len(changes) - len(shown)          # 상한을 넘어 아예 안 적는 건수
    head, rest = items[:FEED_OPEN], items[FEED_OPEN:]
    out = ('<div class="eyebrow section">변경 이력</div>'
           f'<ul class="feed">{"".join(head)}</ul>')
    if rest or over:
        tail = "".join(rest)
        if over:
            tail += f'<li>외 {over}건 (아래 표와 DB price_change 표에 전부 있음)</li>'
        out += (f'<details class="more feed-more">'
                f'<summary>나머지 {len(rest) + over}건 펼치기</summary>'
                f'<ul class="feed">{tail}</ul></details>')
    return out


def _status(status):
    chips = []
    for s in status:
        if s.get("ok"):
            warn = " ⚠" if s.get("warns") else ""
            chips.append(f'<span class="chip">{_esc(s["provider"])} <b>{s["count"]}</b>건{warn}</span>')
        else:
            chips.append(f'<span class="chip err">{_esc(s["provider"])} 수집 실패</span>')
    return ('<div class="eyebrow section">수집 상태</div><div class="status">' + "".join(chips) + "</div>")


MAX_REVIEW_ROWS = 8      # 확인 목록에 직접 적는 최대 줄 수. 넘으면 '외 N건'


def _review_banner(review, changes=()):
    """사람 확인이 필요한 항목을 화면 맨 위에 나열한다."""
    if not review or not review.get("required"):
        return ""
    spikes = [c for c in changes if c.get("spike")]
    rows = []
    for c in spikes[:MAX_REVIEW_ROWS]:
        pct = c.get("pct")
        move = f'{_fmt(c.get("old_value"))} → {_fmt(c.get("new_value"))} '
        move += '<span class="nopct">이전 값 없음</span>' if pct is None else _delta(pct)
        rows.append(f'<li><b>{_esc(c["provider"])}</b> {_line_desc(c)} {move}</li>')
    if len(spikes) > MAX_REVIEW_ROWS:
        rows.append(f'<li class="more-n">외 {len(spikes) - MAX_REVIEW_ROWS}건</li>')
    listing = f'<ul class="wlist">{"".join(rows)}</ul>' if rows else ""
    other = [r for r in (review.get("reasons") or []) if not r.startswith("큰 변동")]
    note = f'<div class="wnote">{_esc(" · ".join(other))}</div>' if other else ""
    return ('<div class="banner warn"><span class="dot"></span>'
            f'<div class="wbody"><div class="wtitle">확인 필요</div>{listing}{note}</div></div>')


CSS = """
:root{
  --bg:#eaeef2; --surface:#ffffff; --surface2:#f5f8fa; --ink:#151a20; --muted:#5a6672;
  --faint:#8b95a1; --line:#e0e5ea; --accent:#0e7490; --accent-soft:#e0f2f4;
  --up:#c2410c; --up-bg:#fbe9df; --down:#0e7490; --down-bg:#e3f2e7; --neutral:#5f6cc4; --neutral-bg:#eaecf8;
  --alert-bg:#fdeede; --alert-ink:#9a4a12; --calm-bg:#e7f1ec; --calm-ink:#256b48;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans KR","Malgun Gothic",sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#0f1319; --surface:#171d25; --surface2:#1d242e; --ink:#e6eaef; --muted:#98a3b0;
    --faint:#66717d; --line:#28313c; --accent:#2dd4bf; --accent-soft:#123b3a;
    --up:#fb923c; --up-bg:#3a2416; --down:#2dd4bf; --down-bg:#16301f; --neutral:#9aa6f0; --neutral-bg:#262a44;
    --alert-bg:#33230f; --alert-ink:#f6b871; --calm-bg:#132a20; --calm-ink:#5fd3a0; }
}
:root[data-theme="dark"]{ --bg:#0f1319; --surface:#171d25; --surface2:#1d242e; --ink:#e6eaef; --muted:#98a3b0;
  --faint:#66717d; --line:#28313c; --accent:#2dd4bf; --accent-soft:#123b3a;
  --up:#fb923c; --up-bg:#3a2416; --down:#2dd4bf; --down-bg:#16301f;
  --neutral:#9aa6f0; --neutral-bg:#262a44;
  --alert-bg:#33230f; --alert-ink:#f6b871; --calm-bg:#132a20; --calm-ink:#5fd3a0; }
:root[data-theme="light"]{ --bg:#eaeef2; --surface:#ffffff; --surface2:#f5f8fa; --ink:#151a20; --muted:#5a6672;
  --faint:#8b95a1; --line:#e0e5ea; --accent:#0e7490; --accent-soft:#e0f2f4;
  --up:#c2410c; --up-bg:#fbe9df; --down:#0e7490; --down-bg:#e3f2e7; --neutral:#5f6cc4; --neutral-bg:#eaecf8;
  --alert-bg:#fdeede; --alert-ink:#9a4a12; --calm-bg:#e7f1ec; --calm-ink:#256b48; }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);
  line-height:1.5;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1080px;margin:0 auto;padding:34px 20px 64px;}
.eyebrow{font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);
  font-weight:700;margin-bottom:6px;}
h1{font-size:1.6rem;line-height:1.2;margin:0 0 6px;letter-spacing:-.02em;text-wrap:balance;}
.sub{color:var(--muted);font-size:.9rem;margin-bottom:24px;}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px;}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;}
.kpi .n{font-size:1.9rem;font-weight:750;font-variant-numeric:tabular-nums;letter-spacing:-.01em;}
.kpi .l{color:var(--muted);font-size:.78rem;margin-top:2px;letter-spacing:.02em;}
.kpi.hot{border-color:var(--up);}
.kpi.hot .n{color:var(--up);}
.banner{border-radius:14px;padding:15px 18px;margin-bottom:24px;font-weight:600;
  display:flex;gap:10px;align-items:baseline;font-size:.94rem;}
.banner .dot{width:9px;height:9px;border-radius:50%;flex:none;align-self:center;}
.banner.calm{background:var(--calm-bg);color:var(--calm-ink);}
.banner.calm .dot{background:var(--calm-ink);}
.banner.alert{background:var(--alert-bg);color:var(--alert-ink);}
.banner.alert .dot{background:var(--up);}
.eyebrow.section{color:var(--faint);margin:30px 0 12px;}
.feed{list-style:none;padding:0;margin:0 0 8px;}
.feed li{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--muted);
  border-radius:0 10px 10px 0;padding:11px 15px;margin-bottom:8px;font-size:.9rem;}
.feed li.up{border-left-color:var(--up);}
.feed li.down{border-left-color:var(--down);}
.feed li.new{border-left-color:var(--neutral);}
.feed li.rm{border-left-color:var(--neutral);}
.feed .prov{font-weight:700;}
.feed .old{color:var(--faint);margin:0 3px;font-variant-numeric:tabular-nums;font-size:.9em;}
.feed .new-v{font-weight:700;font-variant-numeric:tabular-nums;}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:16px;}
.card h3{margin:0;padding:13px 18px;border-bottom:1px solid var(--line);font-size:.98rem;
  display:flex;justify-content:space-between;align-items:center;gap:12px;}
.card h3 .cnt{color:var(--faint);font-weight:400;font-size:.8rem;}
.card h3 a{color:var(--muted);font-size:.76rem;font-weight:500;text-decoration:none;white-space:nowrap;}
.card h3 a:hover{color:var(--accent);}
.tbl-scroll{overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:.9rem;}
th,td{text-align:left;padding:10px 18px;border-bottom:1px solid var(--line);white-space:nowrap;}
tr:last-child td{border-bottom:none;}
th{color:var(--faint);font-weight:600;font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;}
th.num,td.num{text-align:right;}
td.num{font-variant-numeric:tabular-nums;font-size:.88rem;}
tr.changed td{background:var(--surface2);}
tr.changed td:first-child{box-shadow:inset 3px 0 0 var(--up);}
tr.added td:first-child{box-shadow:inset 3px 0 0 var(--neutral);}
.pill{display:inline-block;border-radius:6px;padding:1px 7px;font-size:.68rem;font-weight:700;
  margin-left:8px;vertical-align:middle;letter-spacing:.02em;}
.pill.chg{background:var(--up-bg);color:var(--up);}
.pill.new{background:var(--neutral-bg);color:var(--neutral);}
.pill.spike{background:var(--up);color:#fff;}
.banner.warn{background:var(--alert-bg);color:var(--alert-ink);border:1px solid var(--up);
  align-items:flex-start;}
.banner.warn .dot{background:var(--up);margin-top:7px;align-self:flex-start;}
.wbody{flex:1;min-width:0;}
.wtitle{font-weight:700;}
.wlist{list-style:none;margin:7px 0 0;padding:0;display:flex;flex-direction:column;gap:4px;}
.wlist li{font-weight:500;font-size:.88rem;font-variant-numeric:tabular-nums;}
.wlist li b{font-weight:700;}
.wlist .nopct{font-size:.76rem;opacity:.8;margin-left:4px;}
.wlist .more-n{opacity:.75;font-size:.83rem;}
.wnote{margin-top:7px;font-size:.86rem;font-weight:500;}
/* 부가 단가 펼침 (JS 없이 HTML details) */
td details>summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;gap:2px;}
td details>summary::-webkit-details-marker{display:none;}
td details>summary::before{content:"›";color:var(--faint);font-weight:700;
  display:inline-block;transform:rotate(0deg);transition:transform .12s;margin-right:5px;}
td details[open]>summary::before{transform:rotate(90deg);}
.mwrap{display:inline-flex;flex-direction:column;vertical-align:middle;}
/* 모델명 뒤에 붙는 적용 시기. 이름과 같은 줄에 두면 어디까지가 이름인지 안 읽힌다 */
.period{font-size:.72rem;color:var(--faint);letter-spacing:.01em;font-weight:400;margin-top:1px;}
.feed .per{font-size:.78rem;color:var(--faint);}
.extras{display:flex;flex-wrap:wrap;gap:6px 14px;padding:7px 0 2px 13px;}
.extras .ex{font-size:.8rem;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;}
.extras .ex i{font-style:normal;color:var(--faint);margin-right:4px;}
/* 접힌 나머지 모델 묶음 */
details.more{border-top:1px solid var(--line);}
/* 변경 이력 접기(2026-08-17). 카드 안이 아니라 홀로 놓이므로 테두리를 줄에 맞춘다 */
details.more.feed-more{border-top:none;margin:0 0 8px;}
details.more.feed-more>summary{border:1px solid var(--line);border-radius:10px;
  background:var(--surface);padding:10px 15px;}
details.more>summary{cursor:pointer;list-style:none;padding:11px 18px;font-size:.83rem;
  color:var(--muted);display:flex;align-items:center;gap:8px;}
details.more>summary::-webkit-details-marker{display:none;}
details.more>summary::before{content:"+";font-weight:700;color:var(--accent);}
details.more[open]>summary::before{content:"−";}
details.more>summary:hover{color:var(--ink);background:var(--surface2);}
/* 접힌 묶음의 성격 표시. 옆 문구와 섞이지 않게 오른쪽 끝에 테두리를 둘러 뗀다 */
details.more .hint{margin-left:auto;color:var(--faint);font-size:.72rem;
  border:1px solid var(--line);border-radius:20px;padding:2px 10px;white-space:nowrap;}
.delta{font-size:.76rem;font-weight:700;margin-left:6px;font-family:var(--font);}
.delta.up{color:var(--up);}
.delta.down{color:var(--down);}
.status{display:flex;gap:8px;flex-wrap:wrap;}
.chip{font-size:.78rem;border:1px solid var(--line);background:var(--surface);border-radius:20px;
  padding:5px 12px;color:var(--muted);}
.chip b{color:var(--ink);font-variant-numeric:tabular-nums;}
.chip.err{color:var(--up);border-color:var(--up);}
.foot{color:var(--faint);font-size:.78rem;margin-top:34px;border-top:1px solid var(--line);padding-top:16px;line-height:1.6;}
"""

TREND_CSS = """
.trend{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:16px 18px;margin-bottom:8px;position:relative;}
.trend .hit{fill:transparent;}
.trend .tip{position:absolute;display:none;pointer-events:none;z-index:5;
  background:#fff;color:#111;border:1px solid #cbd5e1;border-radius:8px;
  padding:6px 9px;font-size:.8rem;line-height:1.5;white-space:nowrap;
  box-shadow:0 4px 14px rgba(15,23,42,.18);}
.trend .ctl{display:flex;flex-wrap:wrap;gap:16px;align-items:center;margin-bottom:12px;
  font-size:.84rem;}
.trend .ctl b{color:var(--muted);font-weight:600;margin-right:6px;}
.trend .seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;}
.trend .seg button{border:0;background:var(--surface2);color:var(--muted);font:inherit;
  padding:5px 11px;cursor:pointer;border-right:1px solid var(--line);}
.trend .seg button:last-child{border-right:0;}
.trend .seg button[aria-pressed="true"]{background:var(--accent);color:#fff;}
.trend label.rad{margin-right:10px;cursor:pointer;color:var(--muted);}
.trend label.rad input{margin-right:4px;}
.trend .plot{width:100%;height:auto;display:block;}
.trend .grid{stroke:var(--line);stroke-width:1;}
.trend .axis{fill:var(--faint);font-size:11px;font-family:var(--mono);}
.trend .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round;}
.trend .legend{display:flex;flex-wrap:wrap;gap:10px 16px;margin-top:10px;font-size:.8rem;}
.trend .legend i{width:12px;height:3px;border-radius:2px;display:inline-block;
  margin-right:5px;vertical-align:middle;}
.trend .pick{margin-top:12px;}
.trend .pick summary{cursor:pointer;color:var(--accent);font-size:.84rem;font-weight:600;}
.trend .pick .grp{margin:10px 0 0;}
.trend .pick .grp>b{display:block;color:var(--muted);font-size:.76rem;margin-bottom:4px;}
.trend .pick label{display:inline-block;margin:0 12px 5px 0;font-size:.82rem;cursor:pointer;}
.trend .pick label input{margin-right:4px;}
.trend .note{color:var(--faint);font-size:.78rem;margin-top:8px;}
"""

# 그래프 그리는 코드. 즉시 실행 함수로 감싸고 이름에 apm- 접두사를 둔다.
# 이 화면을 다른 페이지 본문에 끼워 넣을 때 그쪽 코드와 이름이 부딪히지 않게 하려는 것이다.
# 외부에서 가져오는 것은 없다(자족형 조건 유지).
TREND_JS = """
(function(){
  // 이 스크립트 바로 앞의 자료 덩어리, 그 앞의 그래프 상자를 찾는다.
  // 고정 id로 찾으면 이 화면을 한 페이지에 두 번 끼워 넣었을 때 둘 다 첫 번째를
  // 잡는다(2026-08-01 Codex 적대 검증 지적). 라디오 이름은 여전히 문서 단위라
  // 두 번 끼워 넣으면 항목 선택이 서로 엮이는데, 그때도 안 죽게 아래에서 받는다.
  var me=document.currentScript;
  var dataEl=me && me.previousElementSibling;
  var box=dataEl && dataEl.previousElementSibling;
  if(!box || !dataEl) return;
  var DATA=JSON.parse(dataEl.textContent);
  var KIND={input:'입력',output:'출력',cache_read:'캐시 읽기',cache_write:'캐시 쓰기',
            batch_input:'배치 입력',batch_output:'배치 출력'};
  var COLORS=['#0e7490','#c2410c','#5f6cc4','#2f855a','#b45309','#9333ea',
              '#0891b2','#be123c','#4d7c0f','#7c3aed'];
  var W=760,H=320,L=66,R=14,T=14,B=34;
  var days=0;   // 0 = 전체
  // 모델 이름은 회사 페이지에서 긁어온 글자다. 화면에 그대로 넣으면 그 안의
  // <, > 같은 글자가 태그로 읽힌다. 값이 아니라 글자로만 보이게 바꾼다
  function esc(s){return String(s).replace(/[&<>"']/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function sel(){var o={};box.querySelectorAll('.pick input:checked').forEach(function(c){o[c.value]=1});return o;}
  function item(){var r=box.querySelector('input[name=apm-item]:checked');
                  return r?r.value:'input';}
  function fmt(v){
    if(v===0) return '$0';
    var s=v>=1?v.toFixed(2):v.toFixed(v>=0.01?3:4);
    return '$'+s.replace(/0+$/,'').replace(/\\.$/,'');
  }
  // 말풍선. SVG 기본 <title>은 뜨는 데 1초쯤 걸리고 글씨가 작은 회색이라 직접 그린다.
  // 점(r=3)은 맞추기 어려워 투명한 판정 원(r=9)을 겹쳐 두고 거기에 반응한다.
  var tip=document.createElement('div'); tip.className='tip'; box.appendChild(tip);
  function showTip(t){
    tip.innerHTML='<b>'+esc(t.getAttribute('data-l'))+'</b><br>'+
      esc(t.getAttribute('data-d'))+' · <b>'+esc(t.getAttribute('data-v'))+'</b>';
    tip.style.display='block';
    var cr=t.getBoundingClientRect(), br=box.getBoundingClientRect();
    var x=cr.left+cr.width/2-br.left, y=cr.top-br.top;
    var lx=x-tip.offsetWidth/2, ly=y-tip.offsetHeight-8;
    if(lx<4) lx=4;
    if(lx+tip.offsetWidth>br.width-4) lx=br.width-tip.offsetWidth-4;
    if(ly<0) ly=y+16;
    tip.style.left=lx+'px'; tip.style.top=ly+'px';
  }
  function draw(){
    tip.style.display='none';
    var it=item(), on=sel(), D=DATA.dates;
    var last=D[D.length-1], from=null;
    if(days>0){var d=new Date(last+'T00:00:00Z'); d.setUTCDate(d.getUTCDate()-(days-1));
               from=d.toISOString().slice(0,10);}
    // 값은 날짜 배열과 자리를 맞춰 담겨 있다. 없는 날은 null이라 걸러 낸다
    var lines=DATA.series.filter(function(s){return s.i===it && on[s.p+'|'+s.m];})
      .map(function(s){
        var pts=[];
        for(var k=0;k<D.length;k++){
          if(s.v[k]==null) continue;
          if(from && D[k]<from) continue;
          pts.push([D[k],s.v[k]]);
        }
        return {label:s.l, prov:s.p, m:s.m, pts:pts};
      }).filter(function(l){return l.pts.length>0;});

    var svg=box.querySelector('.plot'), leg=box.querySelector('.legend'),
        note=box.querySelector('.note');
    if(!lines.length){
      var msg=Object.keys(on).length
        ? '선택한 모델에는 '+KIND[it]+' 단가가 없습니다'
        : '보고 싶은 모델을 아래에서 고르세요';
      svg.innerHTML='<text class="axis" x="'+(W/2)+'" y="'+(H/2)+'" text-anchor="middle">'+
        msg+'</text>';
      leg.innerHTML=''; note.textContent=''; return;
    }
    var xs=[],vs=[];
    lines.forEach(function(l){l.pts.forEach(function(p){xs.push(p[0]);vs.push(p[1]);});});
    xs.sort(); var x0=Date.parse(xs[0]+'T00:00:00Z'), x1=Date.parse(xs[xs.length-1]+'T00:00:00Z');
    var lo=Math.min.apply(null,vs), hi=Math.max.apply(null,vs);
    // 값 폭이 20배를 넘으면 로그 눈금으로 바꾼다. 안 그러면 작은 값들이 바닥에 붙어 안 보인다
    var log = lo>0 && hi/lo>20;
    var ylo=log?Math.log10(lo):Math.min(0,lo), yhi=log?Math.log10(hi):hi;
    if(yhi===ylo){yhi=ylo+1;}
    else{var pad=(yhi-ylo)*0.08; ylo-=pad; yhi+=pad; if(!log&&ylo<0)ylo=0;}
    function px(d){return x1===x0?(L+(W-L-R)/2):L+(Date.parse(d+'T00:00:00Z')-x0)/(x1-x0)*(W-L-R);}
    function py(v){var y=log?Math.log10(v):v; return T+(1-(y-ylo)/(yhi-ylo))*(H-T-B);}

    var out='';
    for(var i=0;i<=4;i++){
      var v=ylo+(yhi-ylo)*i/4, y=T+(1-i/4)*(H-T-B);
      out+='<line class="grid" x1="'+L+'" y1="'+y+'" x2="'+(W-R)+'" y2="'+y+'"/>';
      out+='<text class="axis" x="'+(L-8)+'" y="'+(y+4)+'" text-anchor="end">'+
           fmt(log?Math.pow(10,v):v)+'</text>';
    }
    var uniq=[]; xs.forEach(function(d){if(uniq[uniq.length-1]!==d)uniq.push(d);});
    var step=Math.max(1,Math.ceil(uniq.length/6));
    uniq.forEach(function(d,i){
      if(i%step && i!==uniq.length-1) return;
      out+='<text class="axis" x="'+px(d)+'" y="'+(H-12)+'" text-anchor="middle">'+d.slice(5)+'</text>';
    });
    lines.forEach(function(l,i){
      var c=COLORS[i%COLORS.length];
      out+='<polyline class="ln" stroke="'+c+'" points="'+
           l.pts.map(function(p){return px(p[0])+','+py(p[1]);}).join(' ')+'"/>';
      l.pts.forEach(function(p){
        var cx=px(p[0]), cy=py(p[1]);
        out+='<circle cx="'+cx+'" cy="'+cy+'" r="3" fill="'+c+'"/>';
        out+='<circle class="hit" cx="'+cx+'" cy="'+cy+'" r="9" fill="transparent" '+
             'data-l="'+esc(l.label)+'" data-d="'+p[0]+'" data-v="'+fmt(p[1])+'"/>';
      });
    });
    svg.innerHTML=out;
    leg.innerHTML=lines.map(function(l,i){
      return '<span><i style="background:'+COLORS[i%COLORS.length]+'"></i>'+esc(l.label)+'</span>';
    }).join('');
    var drawn={}; lines.forEach(function(l){drawn[l.prov+'|'+l.m]=1;});
    var miss=Object.keys(on).length-Object.keys(drawn).length;
    note.textContent=(log?'값 폭이 커서 로그 눈금으로 그렸습니다. ':'')+
      (miss>0?'선택한 모델 중 '+miss+'개는 '+KIND[it]+' 단가가 없거나 기간 안에 값이 없어 빠졌습니다. ':'')+
      '점 위에 마우스를 올리면 그날 값이 나옵니다. 백만 토큰당 단가입니다.';
  }
  var plot=box.querySelector('.plot');
  plot.addEventListener('mouseover',function(e){
    var t=e.target;
    if(t.getAttribute && t.getAttribute('class')==='hit') showTip(t);
  });
  plot.addEventListener('mouseout',function(e){
    var t=e.target;
    if(t.getAttribute && t.getAttribute('class')==='hit') tip.style.display='none';
  });
  box.addEventListener('change',draw);
  box.querySelectorAll('.seg button').forEach(function(b){
    b.addEventListener('click',function(){
      box.querySelectorAll('.seg button').forEach(function(o){o.setAttribute('aria-pressed','false');});
      b.setAttribute('aria-pressed','true'); days=parseInt(b.dataset.days,10); draw();
    });
  });
  draw();
})();
"""


def _trend(history):
    """가격 추이 절. history 가 비면(DB를 못 읽었거나 값이 없으면) 아무것도 안 넣는다."""
    if not history:
        return ""
    # 기본으로 켜 둘 모델 = 기간 안에서 값이 실제로 움직인 것. 하나도 없으면 앞 넷.
    moved, keys = [], []
    for s in history:
        k = f"{s['provider']}|{s['model']}"
        if k not in keys:
            keys.append(k)
        if len({p[1] for p in s["points"]}) > 1 and k not in moved:
            moved.append(k)
    dflt = set(moved[:6] or keys[:4])

    groups = ""
    for prov in PROVIDER_ORDER:
        ks = [k for k in keys if k.startswith(prov + "|")]
        if not ks:
            continue
        boxes = "".join(
            f'<label><input type="checkbox" value="{_esc(k)}"'
            f'{" checked" if k in dflt else ""}>{_esc(k.split("|", 1)[1])}</label>'
            for k in ks)
        groups += (f'<div class="grp"><b>{_esc(PROVIDER_LABEL.get(prov, prov))}</b>'
                   f"{boxes}</div>")

    # 날짜를 한 번만 적고 계열마다 값 배열만 둔다(파일 크기. 2026-08-01 실측 36KB→76KB 교훈)
    dates = sorted({p[0] for s in history for p in s["points"]})
    idx = {d: i for i, d in enumerate(dates)}
    compact = {"dates": dates, "series": []}
    for s in history:
        vals = [None] * len(dates)
        for d, v in s["points"]:
            vals[idx[d]] = v
        compact["series"].append({"p": s["provider"], "m": s["model"],
                                  "l": s["label"], "i": s["item"], "v": vals})
    data = json.dumps(compact, ensure_ascii=False,
                      separators=(",", ":")).replace("</", "<\\/")
    return (
        '<div class="eyebrow section">가격 추이</div>'
        '<div class="trend" id="apm-trend">'
        '<div class="ctl">'
        '<span><b>항목</b>'
        + "".join(
            f'<label class="rad"><input type="radio" name="apm-item" value="{v}"'
            f'{" checked" if v == "input" else ""}>{FIELD_KO[v]}</label>'
            for v in ("input", "output", "cache_read", "cache_write",
                      "batch_input", "batch_output"))
        + "</span>"
        '<span><b>기간</b><span class="seg">'
        '<button type="button" data-days="7" aria-pressed="false">7일</button>'
        '<button type="button" data-days="30" aria-pressed="false">30일</button>'
        '<button type="button" data-days="0" aria-pressed="true">전체</button>'
        "</span></span>"
        "</div>"
        f'<svg class="plot" viewBox="0 0 760 320" role="img" aria-label="가격 추이 그래프"></svg>'
        '<div class="legend"></div>'
        '<div class="note"></div>'
        f'<details class="pick"><summary>모델 고르기</summary>{groups}</details>'
        "</div>"
        f'<script type="application/json" id="apm-data">{data}</script>'
        f"<script>{TREND_JS}</script>"
    )


def render_body(date_str, rows, changes, status, prev_date, collected_at=None,
                review=None, history=None):
    stamp = collected_at or date_str
    n_prov = len({r["provider"] for r in rows})
    n_model = len({(r["provider"], r["model"]) for r in rows})
    # [수정 2026-08-17 사용자] 단가 줄 수는 위쪽 숫자에서 뺀다(회사별 카드 제목에 남는다)
    kpis = (_kpi(n_prov, "제공사") + _kpi(n_model, "모델")
            + _kpi(len(changes), "변동 건수", hot=bool(changes)))
    return (
        f"<style>{CSS}{TREND_CSS}{EXTRA_CSS}</style>"
        f'<div class="wrap">'
        f'<div class="eyebrow">API Price Monitor</div>'
        f"<h1>LLM API 가격 모니터</h1>"
        f'<div class="sub">마지막 수집: {_esc(stamp)}</div>'
        f'<div class="kpis">{kpis}</div>'
        f"{_review_banner(review, changes)}"
        f"{_banner(changes, prev_date, date_str)}"
        f"{_feed(changes)}"
        f"{_trend(history)}"
        f"{_provider_tables(rows, changes)}"
        f"{_status(status)}"
        f'<div class="foot">각 제공사 공식 가격 페이지(마크다운 제공 회사는 마크다운)에서 자동 수집한 값입니다. '
        f"값은 100만 토큰당 미국 달러이고, 다른 단위는 칸 안에 적혀 있습니다. ×0.8 standard 처럼 적힌 값은 "
        f"회사가 금액 없이 배수로만 공지한 요금입니다. 회사마다 주요 모델만 펼쳐 두었고 나머지는 접혀 있습니다. "
        f"수집 특성상 페이지 개편 시 일부 값이 누락될 수 있으니, 중요한 판단 전에는 각 카드의 공식 페이지 링크로 확인하세요.</div>"
        f"</div>"
    )


# 새 표에만 쓰는 스타일(값 칸 안 단위·조건 열)
EXTRA_CSS = """
td .unit{color:var(--faint);font-size:.74rem;margin-left:3px;}
td.cond{color:var(--muted);font-size:.82rem;}
td.chg .old{color:var(--faint);text-decoration:line-through;margin-right:4px;font-size:.84em;}
"""


def render_page(date_str, rows, changes, status, prev_date, collected_at=None,
                review=None, history=None):
    body = render_body(date_str, rows, changes, status, prev_date, collected_at,
                       review, history)
    # noindex = 검색엔진 결과에 안 뜨게(2026-07-30 결정). robots.txt와 두 겹으로 막는다.
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="robots" content="noindex, nofollow">'
            f"<title>LLM API 가격 모니터</title></head><body>{body}</body></html>")


def build(date_str, rows, changes, status, prev_date, collected_at=None,
          out_path=None, review=None, history=None):
    """화면 파일 생성. 임시 파일에 다 쓴 뒤 이름을 바꾼다(잘린 HTML 이 남지 않게).

    history: 가격 추이 그래프에 쓸 시계열(`db.history()`). None이면 그 절을 안 넣는다.
    """
    os.makedirs(PUBLIC, exist_ok=True)
    out_path = out_path or os.path.join(PUBLIC, "index.html")
    html = render_page(date_str, rows, changes, status, prev_date,
                       collected_at, review, history)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out_path), prefix=".tmp_", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return out_path
