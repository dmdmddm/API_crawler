"""알림 메일 발송.

받는 쪽 메일 서버에 직접 보낸다. 보내는 쪽 인증(앱 비밀번호 등)이 없다.
2026-07-30 실측으로 받은편지함 도착을 확인했다(처음엔 스팸으로 갔고 사용자가
스팸 해제). 인증 정보를 서버에 두지 않아도 되는 대신, 받는 쪽 정책이 바뀌면
조용히 막힐 수 있다. 막히면 로그와 종료 코드에 남는다.

보내는 조건 = 종료 코드가 0이 아닐 때만. 아무 일 없는 날은 보내지 않는다
(매일 오면 안 보게 된다, 2026-07-30 결정).

설정 = `~/.api_crawler_mail.cnf`. 없으면 발송을 건너뛰고 안내만 남긴다.
코드에 주소를 박지 않는 이유는 받는 사람이 늘거나 바뀔 때 코드를 안 고치려는 것.

메일 본문에 종료 코드 숫자를 넣지 않는다. 프로그램끼리 주고받는 값이라 읽는
사람에게 뜻이 전달되지 않는다(2026-07-30). 대신 왜 이 메일이
왔는지를 문장으로 첫 줄에 적는다.
"""
from __future__ import annotations

import configparser
import os
import smtplib
import socket
import subprocess
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

# 항목·조건의 한국어 표기와 금액 표기. 여기 따로 적으면 대시보드와 어긋난다.
# _fmt 는 저장값을 자리에서 자르지 않는다(캐시 단가가 뭉개지던 것을 고친 함수).
from dashboard import _fmt as fmt_price, _col_label, _col_key, _cond

CNF = os.path.expanduser("~/.api_crawler_mail.cnf")
TIMEOUT = 30
MAX_CHANGE_LINES = 30      # 변동이 많은 날 메일이 끝없이 길어지는 것을 막는다
MAX_SUBJECT = 90           # 제목에 쓰는 사유 부분의 길이 상한
# 회사 머리줄에 붙이는 변동 종류. 나열 순서도 이 순서다
KIND_KO = (("added", "등록"), ("removed", "삭제"), ("changed", "가격 변동"))


# ── 화면 주요 모델·알림 대상 (2026-08-16 사용자 확정, 이름 = 주요 모델로 통일 08-17) ──
# 고른 기준 = **회사가 지금 파는 최신 모델 + 그 직전 판**. 실제로 골라 쓸 만한 것만 남긴다.
#   - 최신만 두면 아직 쓰는 직전 판이 빠진다(Claude Opus 5 를 쓰기 시작해도 4.8 을 계속 쓴다)
#   - 구세대(gpt-4 이하·gpt-3.5·davinci·babbage·Gemini 2.x·grok-4.20)는 뺀다
#   - 특수 목적(로봇 Robotics ER·실시간 Live·실시간 번역·음성 합성 TTS)은 뺀다
#   - 라인(등급)마다 최신과 직전을 하나씩 둔다. Claude Haiku 는 4.5 다음 판이 3.5 라 그 둘이다
# 목록에 없어도 **값이 바뀌었거나 처음 등장한 모델은 그날 화면과 메일에 나온다**(아래 예외).
# 새 모델이 나오면 그날 저절로 보이고, 목록에 넣을지는 그때 사람이 정한다.
FEATURED = {
    "Anthropic": ("Claude Fable 5", "Claude Mythos 5",
                  "Claude Opus 5", "Claude Opus 4.8",
                  "Claude Sonnet 5", "Claude Sonnet 4.6",
                  "Claude Haiku 4.5", "Claude Haiku 3.5"),
    "OpenAI": ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5",
               "gpt-5.5-pro", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5-mini",
               "gpt-5.4-nano", "gpt-5-nano",
               "o4-mini", "o3", "o3-mini", "o3-pro"),
    "Google": ("Gemini 3.7 Flash", "Gemini 3.6 Flash",
               "Gemini 3.5 Flash-Lite", "Gemini 3.1 Flash-Lite",
               "Gemini 3.1 Pro Preview",
               "Gemini Embedding 2", "Gemini Embedding",
               "Gemini 3 Pro Image (Nano Banana Pro)",
               "Gemini 3.1 Flash Image (Nano Banana 2)",
               "Gemini 3.1 Flash Lite Image (Nano Banana 2 Lite)",
               "Imagen 4", "Veo 3.1", "Veo 3", "Lyria 3"),
    "xAI": ("grok-4.6", "grok-4.5", "grok-build-0.1"),
    "Perplexity": ("Sonar", "Sonar Pro", "Sonar Reasoning Pro",
                   "Sonar Deep Research"),
    "DeepSeek": ("deepseek-v4-flash", "deepseek-v4-pro"),
}


def _in_featured_section(provider, category, tier):
    """그 회사에서 기본으로 내는 절·등급인지 판정. 모델 이름과 함께 봐야 판정이 끝난다.

    회사마다 지목이 다르다(수집기준_결정항목.md §category 칸 · 공통 절 "알림 기준",
    2026-08-14/15 사용자). category 는 페이지 절 이름 원문(영어)이라 앞부분 일치로 본다
    (xAI 실측 = 'Text API Pricing').
      OpenAI     = category 'Flagship models' + tier standard
      Anthropic  = category 'Model pricing'
      Google     = tier standard·batch 전부 + category 가 Imagen·Veo 로 시작하는 것
      xAI        = category 'Text API'
      Perplexity = category 'Sonar API Pricing'
      DeepSeek   = 전부
    """
    cat = (category or "").strip()
    tier = tier or "standard"
    if provider == "OpenAI":
        return cat.startswith("Flagship models") and tier == "standard"
    if provider == "Anthropic":
        return cat.startswith("Model pricing")
    if provider == "Google":
        return tier in ("standard", "batch") or cat.startswith(("Imagen", "Veo"))
    if provider == "xAI":
        return cat.startswith("Text API")
    if provider == "Perplexity":
        return cat.startswith("Sonar API Pricing")
    if provider == "DeepSeek":
        return True
    return True     # 모르는 회사는 다 낸다(조용히 빠지는 것보다 낫다)


def is_featured(provider, model, category=None, tier=None):
    """이 줄을 화면 첫 화면과 메일에 낼지 판정 = 위 FEATURED 목록의 모델인지 여부."""
    names = FEATURED.get(provider)
    if names is not None and model not in names:
        return False
    return _in_featured_section(provider, category, tier)


def featured_changes(changes):
    """변동 목록 중 FEATURED 목록에 든 것만."""
    return [c for c in changes
            if is_featured(c["provider"], c["model"], c.get("category"), c.get("tier"))]


def load_config():
    """설정 읽기. 파일이 없거나 필수 항목이 빠지면 None."""
    if not os.path.exists(CNF):
        return None
    cp = configparser.ConfigParser()
    cp.read(CNF, encoding="utf-8")
    if not cp.has_section("mail"):
        return None
    sec = cp["mail"]
    to = [a.strip() for a in sec.get("to", "").split(",") if a.strip()]
    if not to:
        return None
    return {
        "to": to,
        "from": sec.get("from", "api-crawler@claude-code.local"),
        "host": sec.get("smtp_host", "").strip(),
        "port": sec.getint("smtp_port", 25),
    }


def mx_host(address):
    """받는 주소 도메인의 메일 서버. 설정에 안 적혀 있을 때만 쓴다.

    dig 결과에서 우선순위가 가장 낮은 숫자(= 1순위)를 고른다. dig이 없거나
    응답이 비면 None. 설정에 적어 두는 편이 확실하고 이건 보조 경로다.
    """
    domain = address.rsplit("@", 1)[-1]
    try:
        out = subprocess.run(["dig", "+short", "MX", domain],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    best = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            pri, host = int(parts[0]), parts[1].rstrip(".")
            if best is None or pri < best[0]:
                best = (pri, host)
    return best[1] if best else None


def _fmt_value(v):
    """값 표기. 어제 없던 항목은 숫자 대신 '새로 생김'으로."""
    if v is None:
        return "새로 생김"
    return fmt_price(v)


def _desc(c):
    """줄 설명 = 모델 · 등급(표준이 아니면) · 조건 · 항목(자료형·캐시 기간 포함) · 단위."""
    bits = [c["model"]]
    if (c.get("tier") or "standard") != "standard":
        bits.append(c["tier"])
    cond = _cond(c)
    if cond:
        bits.append(cond)
    bits.append(_col_label(_col_key(c)))
    if c.get("multiplier"):
        bits.append(f"({c['multiplier']} 의 배수)")
    elif c.get("unit") and c["unit"] != "per_1M_tokens":
        bits.append(f"({c['unit']})")
    return " · ".join(bits)


def _change_line(c):
    """변동 한 건(= 계열 한 줄)을 사람이 읽는 한 줄로. 아는 종류가 아니면 None.

    회사 이름은 안 넣는다. 회사별 머리줄에 한 번 적혀 있다.
    """
    if c["kind"] == "changed":
        mark = "  [2배 이상]" if c.get("spike") else ""
        pct = c.get("pct")
        rate = f" ({pct:+g}%)" if pct is not None else ""
        return (f"{_desc(c)}: {_fmt_value(c.get('old_value'))} → "
                f"{_fmt_value(c.get('new_value'))}{rate}{mark}")
    if c["kind"] == "added":
        return f"{_desc(c)}: 새로 생김 ({_fmt_value(c.get('new_value'))})"
    if c["kind"] == "removed":
        return f"{_desc(c)}: 목록에서 사라짐 (직전 {_fmt_value(c.get('old_value'))})"
    return None


def _kinds_label(items):
    """그 회사에 무엇이 있었는지. '등록, 삭제 및 가격 변동' 꼴.

    머리줄만 읽고도 회사별로 무슨 일이 있었는지 알게 한다(2026-08-14 사용자).
    아래 줄이 상한(MAX_CHANGE_LINES)에 걸려 잘려도 이 표기는 그 회사의 변동
    전체를 센다 - 머리줄은 목록 요약이 아니라 그날 사실이다.
    """
    kinds = {c.get("kind") for c in items}
    names = [ko for k, ko in KIND_KO if k in kinds]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " 및 " + names[-1]


def build_body(date_str, headlines, changes, status, prev_date, dash_path,
               urls=None, upcoming=None):
    """메일 본문. 첫 줄 = 왜 왔는지, 그다음 변동 내역과 수집 결과.

    변동 내역은 **회사별로 묶는다**(2026-08-10 사용자 지시). 줄마다 회사 이름을
    되풀이하면 같은 글자가 세로로 쌓여 무엇이 바뀌었는지가 안 보인다 - 2026-08-01
    은 변동 12건이 전부 'OpenAI · ' 로 시작했다.

    urls: {회사: 공식 가격 페이지 주소}. 주면 회사 머리줄에 한 번 붙인다. 받는
    사람이 메일에서 바로 원문을 열어 확인할 수 있게.

    절마다 '# ' 머리줄을 단다(2026-08-14 사용자). 비교 기준 날짜는 회사마다
    되풀이하지 않고 맨 위 요약 제목에 한 번만 적는다.
    """
    base = f"({prev_date} 값과 비교)" if prev_date else "(비교 기준 없음)"
    # 메일에 내는 것 = FEATURED 목록의 변동(회사별 절) + 목록 밖 변동(뒤에 한 절).
    # 2026-08-16 사용자: 목록 밖이라도 값이 바뀌었으면 알린다. 옛 규칙은 건수만 적었는데,
    # 그러면 구세대 모델 인하·인상이 메일에서 사라진다
    featured = featured_changes(changes)
    feat_ids = {id(c) for c in featured}
    others = [c for c in changes if id(c) not in feat_ids]
    # 변동이 없는 날에도 수집 실패·경고로 메일이 나간다. 그런 날 '변동 내역'이라
    # 적으면 아래에 변동 절이 없어 읽는 사람이 잘린 메일로 본다
    lines = ["# 변동 내역 요약 " + base if changes else "# 수집 상태 요약"]
    lines += list(headlines) + [""]

    shown = 0
    if featured:
        by_prov = {}
        for c in featured:
            by_prov.setdefault(c["provider"], []).append(c)
        for prov in sorted(by_prov):
            if shown >= MAX_CHANGE_LINES:
                break
            if shown:
                lines.append("")
            url = (urls or {}).get(prov)
            kinds = _kinds_label(by_prov[prov])
            lines.append(f"# 변동 내역 - {prov}" + (f", {kinds}" if kinds else "")
                         + (f" ({url})" if url else ""))
            for c in by_prov[prov]:
                if shown >= MAX_CHANGE_LINES:
                    break
                one = _change_line(c)
                if one:
                    lines.append("  " + one)
                    shown += 1
        if shown < len(featured):
            lines.append(f"  ... {len(featured) - shown}건 더 (대시보드에서 전체 확인)")
        lines.append("")
    if others:
        left = max(MAX_CHANGE_LINES - shown, 0)
        lines.append(f"# 주요 모델 밖 변동 {len(others)}건"
                     " (구세대·특수 목적 모델. 화면에서는 접힌 표에 있습니다)")
        for c in others[:left]:
            one = _change_line(c)
            if one:
                lines.append("  " + one)
        if len(others) > left:
            lines.append(f"  ... {len(others) - left}건 더 (대시보드에서 전체 확인)")
        lines.append("")

    if upcoming:
        # [추가 2026-08-17] 예고는 DB·화면에 없으므로 메일이 유일한 통로다(사용자
        # 결정, 명세 = 수집기준_결정항목.md 공통 절). 원문을 그대로 실어 받는
        # 사람이 페이지를 열어 수집 코드 수정 필요를 판단할 근거를 준다
        lines.append("# 가격 변동 예고 (시행 전 - 오늘 값에는 안 들어감)")
        for u in upcoming:
            tag = " - 새 예고" if u.get("new") else ""
            lines.append(f"  {u['provider']}: 예고 단가 {u['lines']}건{tag}")
            if u.get("note"):
                lines.append(f"    원문: {u['note']}")
        lines.append("  시행 날 페이지 구조가 같이 바뀔 수 있습니다. 원문 페이지를"
                     " 열어 수집 코드 수정이 필요할지 봐 주세요.")
        lines.append("")

    ok = [s for s in status if s.get("ok")]
    bad = [s for s in status if not s.get("ok")]
    total = sum(s.get("count") or 0 for s in status)
    lines.append("# 수집 결과")
    lines.append(f"  {len(ok)}개 회사 성공 · 단가 {total}건")
    lines.append("  " + " · ".join(f"{s['provider']} {s.get('count') or 0}"
                                   for s in status))
    for s in bad:
        why = s.get("error") or "사유 기록 없음"
        lines.append(f"  실패: {s['provider']} - {why}")
    for s in status:
        for w in s.get("warns") or []:
            lines.append(f"  경고: {s['provider']} - {w}")
    lines.append("")
    lines.append(f"대시보드 파일: {dash_path}")
    return "\n".join(lines) + "\n"


def _ko_date(date_str):
    """2026-07-29 -> 7월 29일. 형식이 다르면 원문 그대로."""
    parts = date_str.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return date_str
    return f"{int(parts[1])}월 {int(parts[2])}일"


def _change_digest(changes):
    """[추가 2026-08-17 사용자] 변동만 있고 문제는 없는 날의 제목 속.

    실물: 'DeepSeek 단가 30건 바뀜'. 회사가 둘·셋이면 이름을 잇고, 넷부터는
    '4개사' 로 줄인다. 제목에 회사가 있으면 열기 전에 어디 일인지 알 수 있다.
    건수는 그날 바뀐 전부(주요 모델 밖 포함)를 센다 - 본문이 둘로 나눠 적는다.
    """
    if not changes:
        return ""
    provs = sorted({c["provider"] for c in changes})
    if len(provs) == 1:
        who = provs[0]
    elif len(provs) <= 3:
        who = " · ".join(provs)
    else:
        who = f"{len(provs)}개사"
    return f"{who} 단가 {len(changes)}건 바뀜"


def build_subject(date_str, headlines, hints=None, changes=None):
    """제목. 날짜 + 사유 요약.

    hints = 짧은 사유 목록(run.py 의 reasons). 본문 첫 줄용 문장은 완결된
    문장이라 제목에 넣으면 길다. 메일함 목록에서 한눈에 보이게 짧은 쪽을 쓴다.
    수집이 다 잘된 날은 짧은 사유가 안 만들어지므로 변동 요약을 대신 쓴다
    (2026-08-17 전에는 그런 날 본문 문장이 통째로 제목에 올라가 200자가 넘었다).
    """
    when = _ko_date(date_str)
    src = [h for h in (hints or []) if h]
    if not src:
        digest = _change_digest(changes)
        if digest:
            return f"[LLM 가격 모니터] {when} · {digest}"
        src = [h.rstrip(".") for h in (headlines or [])]
    if not src:
        return f"[LLM 가격 모니터] {when} · 확인 필요"
    head = " / ".join(src[:2])
    if len(src) > 2:
        head += f" 외 {len(src) - 2}건"
    # 사유 문장이 길면(안 온 모델 이름을 늘어놓는 경우 등) 제목이 수백 자가 된다.
    # 메일함 목록에서 잘려 보이므로 여기서 끊는다
    if len(head) > MAX_SUBJECT:
        head = head[:MAX_SUBJECT].rstrip() + "..."
    return f"[LLM 가격 모니터] {when} · {head}"


def send(date_str, headlines, changes, status, prev_date, dash_path,
         hints=None, dry=False, urls=None, upcoming=None):
    """알림 메일 발송. 반환 = (보냈나, 사람이 읽을 결과 한 줄).

    예외를 밖으로 올리지 않는다. 메일이 실패해도 그날 수집은 실패가 아니다
    (DB와 같은 원칙). 호출부는 결과를 로그에만 반영한다.

    ★감싸는 범위가 발송뿐이면 안 된다(2026-07-30 Codex 적대검증 지적). 설정
    파일이 깨졌을 때(`smtp_port = abc` 등)와 본문·헤더 조립에서 나는 예외가
    그대로 밖으로 새어 수집 프로세스를 죽이는 것을 실제로 재현했다.
    """
    try:
        return _prepare_and_send(date_str, headlines, changes, status, prev_date,
                                 dash_path, hints, dry, urls, upcoming)
    except Exception as e:
        return False, f"예외: {type(e).__name__}: {e}"


def _prepare_and_send(date_str, headlines, changes, status, prev_date, dash_path,
                      hints, dry, urls=None, upcoming=None):
    """설정 읽기 -> 본문 조립 -> 발송. 예외 처리는 send() 가 맡는다."""
    cfg = load_config()
    if not cfg:
        # 파일이 없는 것과 내용이 모자란 것을 나눠 적는다. 로그만 보고 원인을
        # 찾아야 하므로 "없어서 건너뜀"으로 뭉치면 설정을 잘못 적은 것을 못 찾는다
        if not os.path.exists(CNF):
            return False, f"설정 파일이 없어 건너뜀 ({CNF})"
        return False, (f"설정에 받는 주소나 [mail] 절이 없어 건너뜀 ({CNF})")

    host = cfg["host"] or mx_host(cfg["to"][0])
    if not host:
        return False, "받는 쪽 메일 서버를 찾지 못함 (설정의 smtp_host 확인)"

    msg = EmailMessage()
    msg["Subject"] = build_subject(date_str, headlines, hints, changes)
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["from"].rsplit("@", 1)[-1])
    msg.set_content(build_body(date_str, headlines, changes, status,
                               prev_date, dash_path, urls, upcoming))

    if dry:
        print("----- 보내지 않고 내용만 출력 -----")
        print(f"To: {msg['To']}\nFrom: {msg['From']}\nSubject: {msg['Subject']}\n")
        print(msg.get_content())
        return False, f"시험 출력만 함 (보낼 곳 {host}:{cfg['port']})"

    try:
        with smtplib.SMTP(host, cfg["port"], timeout=TIMEOUT) as s:
            helo = cfg["from"].rsplit("@", 1)[-1]
            s.ehlo(helo)
            if s.has_extn("starttls"):     # 전송 구간 암호화. 받는 쪽이 지원할 때만
                s.starttls()
                s.ehlo(helo)
            refused = s.send_message(msg)
        if refused:
            return False, f"일부 수신자 거부: {refused}"
        return True, f"보냄 → {', '.join(cfg['to'])}"
    except smtplib.SMTPResponseException as e:
        detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) \
            else e.smtp_error
        return False, f"거부됨 (코드 {e.smtp_code}): {detail}"
    except (socket.timeout, OSError) as e:
        return False, f"연결 실패: {type(e).__name__}: {e}"
    except Exception as e:
        return False, f"예외: {type(e).__name__}: {e}"


if __name__ == "__main__":
    # 손으로 시험할 때. 저장본과 변동 기록을 읽어 그날 메일을 만들어 본다.
    import argparse
    import json

    import storage
    import pages

    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="날짜 (YYYY-MM-DD)")
    ap.add_argument("--send", action="store_true",
                    help="실제로 보낸다. 없으면 내용만 출력")
    a = ap.parse_args()

    snap = storage.load_snapshot(a.date)
    cpath = os.path.join(storage.CHANGE_DIR, f"{a.date}.json")
    chg = json.load(open(cpath, encoding="utf-8")) if os.path.exists(cpath) else []
    heads = snap.get("review", {}).get("reasons") or ["시험 발송"]
    base = (snap.get("meta") or {}).get("baselines") or {}
    ok, note = send(a.date, heads, chg, snap.get("status") or [],
                    sorted(base.values())[-1] if base else None,
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "public", "index.html"),
                    hints=heads, dry=not a.send, urls=pages.provider_urls())
    print(f"\n결과: {note}")
