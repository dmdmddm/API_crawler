"""날짜별 JSON 저장본(PriceRow 줄 목록)을 MySQL에 넣는다.

사용:
  python db.py --date 2026-08-16      # 하루치 넣기
  python db.py --all                  # 저장본 전부 넣기
  python db.py --verify 2026-08-16    # 넣은 값이 JSON과 같은지 대조
  python db.py --note Google "Gemini X" "이름 변경"   # 모델 비고 적기

수집 코드를 거치지 않고 **이미 저장된 JSON에서 읽어 넣는다.** 그래야 검증이 끝난
값을 그대로 옮기는 것이 되고, 값이 틀리면 넣기 코드만 의심하면 된다.

구조: 조건(계열)과 값(관측)을 나눠 넣는다.
  price_condition  = "이 모델의 이 조건에서 이 항목" 하나. 조건이 새로 나올 때만 늘어남
  daily_price   = 그 계열이 그날 얼마였는가. 매일 쌓임

2026-08-15 3판: 저장본이 PriceRow 줄 목록(한 줄 = 값 하나, 축 12개)이 되어 여섯 칸
순회(FIELD_MAP)를 없애고 줄을 그대로 넣는다. 축에 multiplier 가 들어가고 category 는
계열에 붙되 고유 키에는 안 들어간다(schema.sql 3판 머리 설명).

접속 정보는 코드에 없다. ~/.api_crawler_db.cnf(권한 600)에서 읽는다.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from decimal import Decimal

import pymysql

BASE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(BASE, "data", "snapshots")
CHANGE_DIR = os.path.join(BASE, "data", "changes")
CNF = os.path.expanduser("~/.api_crawler_db.cnf")
SOCKET = "/var/run/mysqld/mysqld.sock"
DATABASE = "api_price"   # 2026-08-07 llm_price 에서 이름 변경

# 모델명 뒤에 적용 시기가 붙는 경우를 떼어내기 위한 패턴(2026-08-15 dashboard 에서 이관).
# 예: 'Claude Sonnet 5 through August 31, 2026' -> 이름 + 시기.
# 날짜 형태가 뒤따를 때만 자른다(모델명에 우연히 들어간 단어를 자르지 않도록).
# 새 어댑터는 시기를 이름에 안 남기지만(effective_from·note 로 분리) 안전망으로 둔다.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_PERIOD = re.compile(
    r"^(.*?)\s+(through|until|starting|from)\s+(" + "|".join(_MONTHS) +
    r")\s+(\d{1,2}),?\s+(\d{4})$", re.I)

# model.note 에 자동으로 적는 문구. note_missing 이 쓰고 note_back 이 찾는다.
# ★두 곳이 같은 글자를 봐야 하므로 여기 한 번만 적는다.
NOTE_STOP = "부터 API 가격이 공지되지 않음"
NOTE_BACK = "부터 다시 공지됨"
NOTE_MAX = 300                 # schema.sql model.note VARCHAR(300)
POINT_NOTE_MAX = 300           # schema.sql daily_price.note VARCHAR(300)

# 단위 표기 통일. 어댑터마다 pricetext 파서 어휘를 쓰는데 같은 뜻이 둘로 갈린 것을
# 적재층에서 하나로(수집기준_결정항목.md 경과 표 0815 밤 행: per_1K_call(xAI) 대
# per_1K_request(Google) 병존 -> ③에서 정규화). 뜻이 다른 것(GB 대 GiB)은 안 합친다.
UNIT_ALIAS = {"per_1K_request": "per_1K_call"}


def connect():
    return pymysql.connect(read_default_file=CNF, unix_socket=SOCKET,
                           database=DATABASE, charset="utf8mb4", autocommit=False)


def git_commit():
    try:
        out = subprocess.run(["git", "-C", BASE, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()[:40] if out.returncode == 0 else ""
    except Exception:
        return ""


def split_period(name):
    """모델명에 박힌 적용 시기를 (이름, 시작일, 종료일)로 나눈다.

    'Claude Sonnet 5 through August 31, 2026' -> ('Claude Sonnet 5', None, '2026-08-31')
    시기를 이름에 두면 9월 1일에 앞 이름이 사라져 모델이 없어진 것처럼 보인다.
    """
    m = _PERIOD.match(name)
    if not m:
        return name, None, None
    base, word, month, day, year = m.groups()
    mon = _MONTHS.index(month.capitalize()) + 1
    date = f"{year}-{mon:02d}-{int(day):02d}"
    if word.lower() in ("through", "until"):
        return base, None, date
    return base, date, None


def applies_on(model_name, date, effective_from=None):
    """그날 적용되는 단가인가. 아직 시작 안 했거나 이미 끝났으면 False.

    적용 시기는 어댑터가 effective_from 에 채우거나(DeepSeek 예고 표 · Anthropic 시기 행)
    모델 이름에 붙어 온다('... through August 31, 2026'). 칸이 채워져 있으면 그쪽이
    우선한다. 날짜는 YYYY-MM-DD 라 글자 그대로 비교해도 순서가 맞는다.

    ★회사가 인상을 미리 공지하면 같은 모델에 단가가 두 벌 올라온다. 2026-08-10
      Anthropic 페이지에 이렇게 적혀 있었다.
        Claude Sonnet 5 through August 31, 2026     $2 / MTok
        Claude Sonnet 5 starting September 1, 2026  $3 / MTok
      둘 다 받으면 "오늘 얼마인가"에 답이 둘이 된다. 아직 시작 안 한 단가는 받지
      않는다(2026-08-10 사용자 결정 · 2026-08-14 재확인).
    ★규칙을 여기 한 곳에만 둔다. 거르는 자리가 셋(run.py 수집 직후 · to_db_rows 적재
      · verify_raw 대조)이라 규칙이 흩어지면 셋이 따로 놀게 된다.
    """
    _, ef, et = split_period(model_name)
    ef = effective_from or ef
    return not ((ef and ef > date) or (et and et < date))


# 저장본 줄(PriceRow.to_dict) 과 변동 항목(diff.compute_row_changes) 이 같이 쓰는 축 이름.
# 저장본은 context·modality 를 'default'·None 으로 두는데 DB 는 빈칸으로 둔다
# ('default'는 "구간 구분 없음"이라 빈칸. 구간이 있는 것과 구분).
SERIES_AXES = ("item", "unit", "tier", "context_label", "modality",
               "variant", "cache_ttl", "region", "multiplier")


def db_axes(d):
    """저장본 줄 또는 변동 항목 -> DB 계열 축 dict (SERIES_AXES + effective_from/to + 이름).

    한 곳에서만 변환한다. 적재(to_db_rows)와 변동 적재(load_changes)가 서로 다른 규칙으로
    바꾸면 변동 기록이 자기 계열을 못 찾는다.
    """
    name, ef, et = split_period(d["model"])
    if d.get("effective_from"):
        ef = d["effective_from"]
    ctx = d.get("context") or ""
    unit = d.get("unit") or ""
    return {
        "provider": d["provider"], "model": name,
        "item": d["item"],
        "unit": UNIT_ALIAS.get(unit, unit),
        "tier": d.get("tier") or "standard",
        "context_label": "" if ctx == "default" else ctx,
        "modality": d.get("modality") or "",
        "variant": d.get("variant") or "",
        "cache_ttl": d.get("cache_ttl") or "",
        "region": d.get("region") or "global",
        "multiplier": d.get("multiplier") or "",
        "effective_from": ef, "effective_to": et,
    }


def to_db_rows(rows, date=None):
    """저장본 줄 목록 -> 넣을 줄 목록. 한 줄이 한 값.

    date: 주면 그날 적용되는 단가만 남긴다(applies_on).

    ★run.py 가 수집 직후에도 같은 걸 거르는데 여기에도 두는 이유 = 저장본 JSON 에는
      회사 페이지에 있던 것이 그대로 들어 있다. `python db.py --date 날짜` 로 옛
      날짜를 다시 넣으면 run.py 를 안 거치므로 아직 시작 안 한 단가가 되살아난다
      (2026-08-10). 넣는 길이 둘이면 걸러내기도 둘이어야 한다.

    반환: [{provider, model, SERIES_AXES..., category, currency, effective_from, effective_to,
            value, note, source_url}]
    """
    out = []
    for r in rows:
        if date and not applies_on(r["model"], date, r.get("effective_from")):
            continue
        d = db_axes(r)
        d.update({
            "category": (r.get("category") or "")[:80],
            "currency": r.get("currency") or "USD",
            "value": r["value"],
            "note": (r.get("note") or "")[:POINT_NOTE_MAX],
            "source_url": r.get("source_url") or "",
        })
        out.append(d)
    return out


def axis_key(row):
    """계열을 가르는 축 조합. 이게 같으면 같은 계열이다. (category 는 일부러 뺀다)"""
    return (row["provider"], row["model"], *[row[a] for a in SERIES_AXES],
            row["effective_from"] or "", row["effective_to"] or "")


def find_collisions(rows):
    """같은 축에 값이 둘 이상 오는 것을 찾는다.

    표의 고유 키가 막아 주지만, 넣다가 실패하면 그날 적재가 반쪽이 된다.
    넣기 전에 미리 세어 해당 제공사만 빼고 넣는다(설계 3절).
    """
    seen = {}
    bad = {}
    for r in rows:
        k = axis_key(r)
        if k in seen and seen[k] != r["value"]:
            bad.setdefault(r["provider"], []).append((k, seen[k], r["value"]))
        seen.setdefault(k, r["value"])
    return bad


def dedupe(rows):
    """같은 축에 같은 값이 두 번 온 것을 한 줄로 합친다.

    값이 다른 겹침은 find_collisions가 제공사째로 빼지만, 값까지 같으면
    거기서 안 걸린다. 그대로 두면 넣을 때 고유 키에 막혀 그날 적재가 통째로
    실패한다. 페이지에 같은 값이 두 번 적힌 것뿐이므로 합치고 경고만 낸다.
    (어댑터 쪽 mdtable.dedupe_rows 가 먼저 거르지만 적재층에서도 한 번 더 막는다.)

    합칠 때 출처와 원문 조각은 비어 있지 않은 쪽을 살린다. 먼저 온 줄만 남기면
    그쪽이 빈칸이고 뒤 줄에 값이 있을 때 출처를 잃는다.
    """
    kept, out, dups = {}, [], 0
    for r in rows:
        k = axis_key(r)
        if k in kept:
            dups += 1
            prev = kept[k]
            for fld in ("source_url", "note", "category"):
                if not prev.get(fld) and r.get(fld):
                    prev[fld] = r[fld]
            continue
        kept[k] = r
        out.append(r)
    return out, dups


# ----------------------------------------------------------------------
# 있으면 번호를 가져오고 없으면 만든다
# ----------------------------------------------------------------------

def provider_id(cur, name, url=""):
    cur.execute("SELECT provider_id FROM provider WHERE name=%s", (name,))
    got = cur.fetchone()
    if got:
        if url:
            cur.execute("UPDATE provider SET pricing_url=%s WHERE provider_id=%s",
                        (url, got[0]))
        return got[0]
    cur.execute("INSERT INTO provider(name, pricing_url) VALUES(%s,%s)", (name, url))
    return cur.lastrowid


def model_id(cur, prov_id, name, date):
    cur.execute("SELECT model_id FROM model WHERE provider_id=%s AND name=%s",
                (prov_id, name))
    got = cur.fetchone()
    if got:
        # 처음 본 날은 앞으로만, 마지막 본 날은 뒤로만 넓힌다(옛 날짜를 나중에 넣어도 안전)
        cur.execute("UPDATE model SET first_seen=LEAST(first_seen,%s), "
                    "last_seen=GREATEST(last_seen,%s) WHERE model_id=%s",
                    (date, date, got[0]))
        return got[0]
    cur.execute("INSERT INTO model(provider_id,name,first_seen,last_seen) "
                "VALUES(%s,%s,%s,%s)", (prov_id, name, date, date))
    return cur.lastrowid


def condition_id(cur, mid, row, date):
    where = " AND ".join(f"{a}=%s" for a in SERIES_AXES)
    # <=> 는 NULL끼리도 같다고 보는 비교. 적용 시기가 없는 계열을 찾으려면 필요하다
    cur.execute(
        f"SELECT condition_id FROM price_condition WHERE model_id=%s AND {where} "
        "AND effective_from<=>%s AND effective_to<=>%s",
        (mid, *[row[a] for a in SERIES_AXES], row["effective_from"], row["effective_to"]))
    got = cur.fetchone()
    if got:
        # 출처가 빈칸이면 덮어쓰지 않는다. 있던 주소가 빈칸으로 지워지는 것을 막는다.
        # category 는 마지막 본 절로 갱신한다(모델이 절을 옮겨도 계열은 유지 - 고유 키 밖)
        cur.execute("UPDATE price_condition SET first_seen=LEAST(first_seen,%s), "
                    "last_seen=GREATEST(last_seen,%s), "
                    "source_url=IF(%s='', source_url, %s), "
                    "category=IF(%s='', category, %s) WHERE condition_id=%s",
                    (date, date, row["source_url"], row["source_url"],
                     row["category"], row["category"], got[0]))
        return got[0]
    cols = ", ".join(SERIES_AXES)
    marks = ", ".join(["%s"] * len(SERIES_AXES))
    cur.execute(
        f"INSERT INTO price_condition(model_id,{cols},category,currency,"
        "effective_from,effective_to,first_seen,last_seen,source_url) "
        f"VALUES(%s,{marks},%s,%s,%s,%s,%s,%s,%s)",
        (mid, *[row[a] for a in SERIES_AXES], row["category"], row["currency"],
         row["effective_from"], row["effective_to"], date, date, row["source_url"]))
    return cur.lastrowid


def lookup_series(cur, axes):
    """이미 있는 계열의 번호. 없으면 None(여기서는 만들지 않는다).

    axes: db_axes() 결과. 변동 기록은 이미 관측된 계열에만 붙는다.
    사라진 계열도 과거 관측이 있으므로 표에 남아 있다.
    """
    where = " AND ".join(f"s.{a}=%s" for a in SERIES_AXES)
    cur.execute(f"""
        SELECT s.condition_id FROM price_condition s
          JOIN model m USING(model_id)
          JOIN provider p USING(provider_id)
         WHERE p.name=%s AND m.name=%s AND {where}
           AND s.effective_from<=>%s AND s.effective_to<=>%s""",
                (axes["provider"], axes["model"], *[axes[a] for a in SERIES_AXES],
                 axes["effective_from"], axes["effective_to"]))
    got = cur.fetchone()
    return got[0] if got else None


def load_changes(cur, date, baselines=None):
    """그날 변동 기록을 넣는다. 변동 파일이 없으면 아무것도 안 한다.

    변동 항목 = diff.compute_row_changes 형식(축 12개 + kind + old_value/new_value/pct/spike).
    한 항목이 계열 하나에 바로 대응한다(옛 경로처럼 모델 단위를 여섯 칸으로 펼 일이 없다).

    baselines: {제공사: 비교 기준 날짜}. 저장본의 meta.baselines에서 온다.
    run.py는 회사마다 각자의 마지막 성공일과 비교하므로(2026-07-28), 비교 대상
    날짜도 회사마다 다를 수 있다.

    ★2026-08-17 4판: 실행 번호가 없어져 비교 대상을 날짜로 바로 적는다.
    """
    path = os.path.join(CHANGE_DIR, f"{date}.json")
    if not os.path.exists(path):
        return 0, []
    changes = json.load(open(path, encoding="utf-8"), parse_float=Decimal)
    if not changes:
        return 0, []

    cur.execute("SELECT run_date FROM crawling_run WHERE run_date<%s "
                "ORDER BY run_date DESC LIMIT 1", (date,))
    got = cur.fetchone()
    fallback_date = got[0].isoformat() if got else None

    known = baselines is not None
    n, missing = 0, 0
    bad_base = set()
    for c in changes:
        cid = lookup_series(cur, db_axes(c))
        if cid is None:
            missing += 1     # 계열을 못 찾음. 조용히 버리지 않고 센다
            continue
        base = (baselines or {}).get(c["provider"])
        if base and base >= date:
            bad_base.add(c["provider"])
            base = None
        prev_date = base if base else (None if known else fallback_date)
        old, new, pct = c.get("old_value"), c.get("new_value"), c.get("pct")
        cur.execute(
            "INSERT INTO price_change(run_date,prev_run_date,condition_id,change_type,"
            "old_price,new_price,change_pct,is_big_change) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE change_type=VALUES(change_type), "
            "old_price=VALUES(old_price), new_price=VALUES(new_price), "
            "change_pct=VALUES(change_pct), is_big_change=VALUES(is_big_change)",
            (date, prev_date, cid, c["kind"],
             None if old is None else Decimal(str(old)),
             None if new is None else Decimal(str(new)),
             None if pct is None else Decimal(str(round(float(pct), 4))),
             1 if c.get("spike") else 0))
        n += 1
    warns = []
    if missing:
        warns.append(f"변동 {missing}건이 해당 계열을 못 찾아 안 들어감")
    if bad_base:
        warns.append(f"비교 기준일이 그날 이후임({', '.join(sorted(bad_base))}) "
                     f"- 비교 대상을 안 적음. 저장본 meta.baselines 확인 필요")
    return n, warns


# ----------------------------------------------------------------------

def _snapshot_rows(snap):
    """저장본의 줄 목록. 옛 형식(records = 여섯 칸 레코드)은 넣지 않는다 - 새 DB 는
    새 기준 데이터만 담기로 했다(2026-08-14 갈아엎기 확정)."""
    if "rows" not in snap:
        raise ValueError("옛 형식 저장본(records)입니다. 새 DB 에는 넣지 않습니다")
    return snap["rows"]


def load(conn, date, source="live"):
    """하루치 저장본을 넣는다. 같은 날짜가 이미 있으면 지우고 다시 넣는다."""
    path = os.path.join(SNAP_DIR, f"{date}.json")
    # parse_float=Decimal: JSON 숫자를 근사값(float)으로 거치지 않고 그대로 읽는다
    snap = json.load(open(path, encoding="utf-8"), parse_float=Decimal)
    rows = to_db_rows(_snapshot_rows(snap), date)

    bad = find_collisions(rows)
    warns = []
    if bad:
        for prov, items in bad.items():
            warns.append(f"{prov}: 같은 조건에 값이 둘 이상 {len(items)}건 - 이 제공사는 안 넣음")
        rows = [r for r in rows if r["provider"] not in bad]
    rows, dups = dedupe(rows)
    if dups:
        warns.append(f"같은 조건에 같은 값이 두 번 {dups}건 - 한 줄로 합침(수집 코드 점검 필요)")

    # 적재 단계에서 뺀 것이 있으면 그날 기록에 남긴다. 남기지 않으면 DB만 보는 사람은
    # 그날이 정상 수집인 줄 안다
    review = snap.get("review") or {}
    reasons = list(review.get("reasons") or []) + warns
    required = 1 if (review.get("required") or warns) else 0

    # ★모델 수·줄 수는 저장본이 아니라 **실제로 넣은 줄**에서 센다(2026-08-10 실측 교훈).
    per_prov_models, per_prov_rows = {}, Counter()
    for r in rows:
        per_prov_models.setdefault(r["provider"], set()).add(r["model"])
        per_prov_rows[r["provider"]] += 1
    model_total = sum(len(v) for v in per_prov_models.values())

    with conn.cursor() as cur:
        # 같은 날짜 재실행 = 그날 것을 지우고 새로 넣기(JSON 파일 덮어쓰기와 같은 동작).
        # 관측·제공사별 결과·변동은 딸려서 함께 지워진다(ON DELETE CASCADE)
        cur.execute("DELETE FROM crawling_run WHERE run_date=%s", (date,))
        cur.execute(
            "INSERT INTO crawling_run(run_date,source,started_at,finished_at,"
            "git_commit,snapshot_path,model_count,price_count,"
            "check_needed,check_reasons) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (date, source, snap["generated_at"][:19].replace("T", " "),
             snap["generated_at"][:19].replace("T", " "), git_commit(), path,
             model_total, len(rows),
             required, " · ".join(reasons)[:500]))

        for st in snap.get("status") or []:
            pid = provider_id(cur, st["provider"])
            ok = bool(st.get("ok"))
            cur.execute(
                "INSERT INTO crawling_run_provider(run_date,provider_id,success,"
                "model_count,price_count,tries,warnings,error) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (date, pid, 1 if ok else 0,
                 len(per_prov_models.get(st["provider"], ())) if ok else 0,
                 per_prov_rows.get(st["provider"], 0) if ok else 0,
                 st.get("attempts") or 1,
                 "\n".join(st.get("warns") or []) or None, st.get("error")))

        pcache, cache = {}, {}
        for r in rows:
            if r["provider"] not in pcache:   # 제공사는 6개뿐이라 한 번만 조회
                pcache[r["provider"]] = provider_id(cur, r["provider"], r["source_url"])
            pid = pcache[r["provider"]]
            mkey = (pid, r["model"])
            if mkey not in cache:
                cache[mkey] = model_id(cur, pid, r["model"], date)
            cid = condition_id(cur, cache[mkey], r, date)
            # 소수를 문자열로 거쳐 넣는다. 근사값(float)이 그대로 들어가는 것을 막는다
            cur.execute(
                "INSERT INTO daily_price(condition_id,observed_date,price,note) "
                "VALUES(%s,%s,%s,%s)",
                (cid, date, Decimal(str(r["value"])), r["note"]))

        # 그날 변동 기록. 계열이 다 만들어진 뒤에 넣는다
        n_chg, chg_warns = load_changes(cur, date,
                                        (snap.get("meta") or {}).get("baselines"))
        warns.extend(chg_warns)

        # 관측이 하나도 안 남은 계열·모델은 지운다(같은 날짜를 다시 넣으면서 빠진 것)
        cur.execute("""
            DELETE s FROM price_condition s
              LEFT JOIN daily_price  p USING(condition_id)
              LEFT JOIN price_change c USING(condition_id)
             WHERE p.condition_id IS NULL AND c.condition_id IS NULL""")
        cur.execute("""
            DELETE m FROM model m
              LEFT JOIN price_condition s USING(model_id)
             WHERE s.model_id IS NULL""")

        # 처음·마지막 본 날을 관측에서 다시 계산한다
        cur.execute("""
            UPDATE price_condition s
              JOIN (SELECT condition_id, MIN(observed_date) a, MAX(observed_date) b
                      FROM daily_price GROUP BY condition_id) t USING(condition_id)
               SET s.first_seen=t.a, s.last_seen=t.b""")
        cur.execute("""
            UPDATE model m
              JOIN (SELECT model_id, MIN(first_seen) a, MAX(last_seen) b
                      FROM price_condition GROUP BY model_id) t USING(model_id)
               SET m.first_seen=t.a, m.last_seen=t.b""")

        cur.execute("UPDATE crawling_run SET check_needed=%s, check_reasons=%s "
                    "WHERE run_date=%s",
                    (1 if (review.get("required") or warns) else 0,
                     " · ".join(list(review.get("reasons") or []) + warns)[:500],
                     date))
    conn.commit()
    return len(rows), n_chg, warns


def verify_raw(conn, date):
    """변환 함수를 거치지 않는 대조.

    아래 verify()는 적재와 같은 to_db_rows()로 기대값을 만든다. 그래서 **변환 규칙
    자체가 틀리면 검증도 똑같이 틀린다.** 그 한계를 메우려고, 여기서는 JSON의
    value 를 직접 세어 제공사별 값 목록을 만들고 DB 값 목록과 견준다.
    축을 어디에 넣었는지는 안 보고 "값이 하나도 안 빠지고 안 변했는가"만 본다.

    ★그날 적용되는 단가만 센다. 적재가 안 넣기로 한 것은 대조에서도 빼야 한다.
    반환: (다른 것 목록, JSON 값 개수, DB 값 개수)
    """
    path = os.path.join(SNAP_DIR, f"{date}.json")
    snap = json.load(open(path, encoding="utf-8"), parse_float=Decimal)

    want = Counter()
    for r in _snapshot_rows(snap):
        if not applies_on(r["model"], date, r.get("effective_from")):
            continue
        want[(r["provider"], Decimal(str(r["value"])))] += 1

    got = Counter()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pr.name, p.price
              FROM daily_price p
              JOIN price_condition s USING(condition_id)
              JOIN model m USING(model_id)
              JOIN provider pr USING(provider_id)
             WHERE p.observed_date=%s""", (date,))
        for name, val in cur.fetchall():
            got[(name, val)] += 1

    diffs = [(k, want.get(k, 0), got.get(k, 0))
             for k in set(want) | set(got) if want.get(k, 0) != got.get(k, 0)]
    return diffs, sum(want.values()), sum(got.values())


# 그래프에 싣는 단가 종류 = (요금 종류, 항목, 화면 코드). 배치는 요금 종류(tier)로,
# 캐시는 항목(item)으로 저장돼 있어 짝으로 고른다.
HISTORY_KINDS = (
    ("standard", "input", "input"),
    ("standard", "output", "output"),
    ("standard", "cache_read", "cache_read"),
    ("standard", "cache_write", "cache_write"),
    ("batch", "input", "batch_input"),
    ("batch", "output", "batch_output"),
    # [추가 2026-08-17] 시간대 요금제(DeepSeek). 안 넣으면 2026-08-17 개편 뒤로
    # DeepSeek 선이 그래프에서 끊긴다 - 그 회사의 상시 등급이 peak·off_peak 뿐이라서다.
    # 같은 화면 항목(입력 등)에 등급이 둘이므로 아래에서 계열 키와 라벨에 등급을 넣는다
    ("peak", "input", "input"),
    ("off_peak", "input", "input"),
    ("peak", "output", "output"),
    ("off_peak", "output", "output"),
    ("peak", "cache_read", "cache_read"),
    ("off_peak", "cache_read", "cache_read"),
)
# 화면 항목 하나에 등급이 여럿일 수 있는 것 = 라벨에 등급 이름을 적는다
TIER_KO = {"peak": "피크", "off_peak": "오프피크"}


def history(conn):
    """화면 그래프에 쓸 시계열. 반환: [{provider, model, label, item, points}, ...]
       points = [[날짜, 값], ...] 날짜 오름차순. item = HISTORY_KINDS 의 화면 코드.

    백만 토큰당 단가만 뽑는다. 장당·초당 단가·배수 줄을 같이 그리면 한 축에 안 들어간다.
    문맥 구간은 빈칸과 'short'(짧은 구간)만 쓴다. 자료형·변형·캐시 기간·지역이 갈리면
    라벨에 붙여 구분한다(축을 하나라도 빼면 서로 다른 두 값이 한 계열로 묶여 뒤엣값이
    앞엣값을 덮는다 - 2026-08-01·08-03 Codex 적대 검증에서 재현).
    """
    kind = {(t, i): k for t, i, k in HISTORY_KINDS}
    pairs = " OR ".join(["(s.tier=%s AND s.item=%s)"] * len(HISTORY_KINDS))
    rows = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pr.name, m.name, s.tier, s.item, s.effective_from, s.effective_to,
                   s.context_label, s.modality, s.variant, s.cache_ttl, s.region,
                   p.observed_date, p.price
              FROM daily_price p
              JOIN price_condition s USING(condition_id)
              JOIN model m USING(model_id)
              JOIN provider pr USING(provider_id)
             WHERE s.unit='per_1M_tokens' AND s.multiplier=''
               AND s.context_label IN ('', 'short')
               AND (""" + pairs + """)
             ORDER BY pr.name, m.name, s.tier, s.item, p.observed_date
        """, tuple(x for t, i, _ in HISTORY_KINDS for x in (t, i)))
        for prov, model, tier, item, ef, et, ctx, mo, va, tt, rg, date, value \
                in cur.fetchall():
            rows.append((prov, model, kind[(tier, item)], tier, ef, et, ctx,
                         (mo, va, tt, rg), date, float(value)))

    # ★계열 키에 등급(tier)이 들어간다. 빼면 같은 화면 항목의 피크·오프피크가
    #   한 계열로 묶여 하루에 값이 둘 붙는다(2026-08-17 시간대 요금제 추가)
    # ★[수정 2026-08-17] 시기 표시(effective_from·to)는 먼저 빼고 묶는다. 회사가
    #   시행 전에 붙여 둔 시기 표시를 시행 뒤에 떼면 같은 값이 계열 둘로 갈려
    #   선이 조각난다(DeepSeek 피크 단가가 08-16 예고분과 08-17 상시분으로 갈렸다).
    #   단 한 날짜에 값이 둘 붙으면 그건 진짜 다른 단가이므로 시기별로 되돌린다
    #   (Anthropic '8월 31일까지 / 9월 1일부터'처럼 겹쳐 오는 경우 방어)
    merged = {}
    for prov, model, k, tier, ef, et, ctx, rest, date, value in rows:
        merged.setdefault((prov, model, k, tier, ctx, rest), {}).setdefault(
            (ef, et), []).append([date.isoformat(), value])

    series = {}
    for (prov, model, k, tier, ctx, rest), by_period in merged.items():
        pts = [p for lst in by_period.values() for p in lst]
        if len({d for d, _ in pts}) == len(pts):          # 날짜가 안 겹친다 = 한 선
            series[(prov, model, k, tier, None, None, ctx, rest)] = pts
        else:
            for (ef, et), lst in by_period.items():
                series[(prov, model, k, tier, ef, et, ctx, rest)] = lst

    # 무엇이 갈리는지 세어, 갈리는 축만 라벨에 적는다
    axes = {}
    for prov, model, k, tier, ef, et, ctx, rest in series:
        a = axes.setdefault((prov, model, k), {"period": set(), "ctx": set(),
                                               "rest": set(), "tier": set()})
        a["period"].add((ef, et))
        a["ctx"].add(ctx)
        a["rest"].add(rest)
        a["tier"].add(tier)

    out = []
    for (prov, model, k, tier, ef, et, ctx, rest), points in series.items():
        a = axes[(prov, model, k)]
        tags = []
        if len(a["tier"]) > 1 and tier in TIER_KO:
            tags.append(TIER_KO[tier])
        if len(a["period"]) > 1:
            if et:
                tags.append(f"{et.isoformat()}까지")
            elif ef:
                tags.append(f"{ef.isoformat()}부터")
        if len(a["ctx"]) > 1:
            tags.append("짧은 구간" if ctx == "short" else "구간 구분 없음")
        if len(a["rest"]) > 1:
            mo, va, tt, rg = rest
            bits = [x for x in (mo, va, tt) if x] + ([rg] if rg and rg != "global" else [])
            tags.append(" ".join(bits) if bits else "기본")
        label = f"{model} ({' · '.join(tags)})" if tags else model
        out.append({"provider": prov, "model": model, "label": label,
                    "item": k, "points": sorted(points)})
    out.sort(key=lambda s: (s["provider"], s["model"], s["item"], s["label"]))
    return out


def verify(conn, date):
    """넣은 값이 JSON 저장본과 같은지 대조(축까지). 다르면 목록을 돌려준다."""
    path = os.path.join(SNAP_DIR, f"{date}.json")
    snap = json.load(open(path, encoding="utf-8"), parse_float=Decimal)
    want = {}
    for r in to_db_rows(_snapshot_rows(snap), date):
        want[axis_key(r)] = Decimal(str(r["value"]))

    got = {}
    with conn.cursor() as cur:
        cols = ", ".join(f"s.{a}" for a in SERIES_AXES)
        cur.execute(f"""
            SELECT pr.name, m.name, {cols},
                   s.effective_from, s.effective_to, p.price
            FROM daily_price p
            JOIN price_condition s USING(condition_id)
            JOIN model m USING(model_id)
            JOIN provider pr USING(provider_id)
            WHERE p.observed_date=%s""", (date,))
        n = len(SERIES_AXES)
        for row in cur.fetchall():
            key = (*row[:2 + n],
                   row[2 + n].isoformat() if row[2 + n] else "",
                   row[3 + n].isoformat() if row[3 + n] else "")
            got[key] = row[4 + n]

    diffs = []
    for k in set(want) | set(got):
        a, b = want.get(k), got.get(k)
        if a is None or b is None or a != b:
            diffs.append((k, a, b))
    return len(want), len(got), diffs


def set_note(conn, provider, model, note):
    """모델의 비고를 사람이 손으로 적는다. 반환: (바뀐 줄 수, 덮어쓰기 전 비고).

    자동 기록(note_missing·note_back)과 같은 칸을 쓰고 덮어쓴다. 이름 변경처럼
    페이지만 봐서는 알 수 없는 사유를 적을 때 쓴다.
    """
    if len(note) > NOTE_MAX:
        raise ValueError(f"비고가 {len(note)}자입니다. {NOTE_MAX}자까지만 들어갑니다")
    cur = conn.cursor()
    cur.execute("SELECT m.model_id, m.note FROM model m "
                "JOIN provider p ON p.provider_id = m.provider_id "
                "WHERE p.name = %s AND m.name = %s", (provider, model))
    row = cur.fetchone()
    if not row:
        return 0, ""
    cur.execute("UPDATE model SET note = %s WHERE model_id = %s", (note, row[0]))
    conn.commit()
    return cur.rowcount, row[1] or ""


def stop_open(note):
    """비고의 마지막 줄이 아직 안 닫힌 '공지되지 않음' 줄인가.

    닫혔다 = 뒤에 '(날짜부터 다시 공지됨)' 이 붙었다. 끝 공백·줄바꿈은 무시한다.
    """
    return (note or "").rstrip().endswith(NOTE_STOP)


def note_missing(conn, missing, date_str):
    """공지가 끊긴 첫날 model.note 에 한 줄 적는다. 반환: 적은 모델 수.

    문구 = '2026-04-01부터 API 가격이 공지되지 않음'. '모델이 없어졌다'가 아니라
    '가격이 공지되지 않는다'로 적는다 - 우리가 관측한 것만 말하고 그 모델이 실제로
    어떻게 됐는지는 단정하지 않는다(2026-08-10 확정).

    ★이미 안 닫힌 끊김 줄이 있으면 안 적는다. 끊김 한 번에 줄 하나여야 끊긴 횟수를 셀 수 있다.
    """
    line = f"{date_str}{NOTE_STOP}"
    cur = conn.cursor()
    n = 0
    for prov, names in sorted(missing.items()):
        for name in names:
            cur.execute("SELECT m.model_id, m.note FROM model m "
                        "JOIN provider p ON p.provider_id = m.provider_id "
                        "WHERE p.name = %s AND m.name = %s", (prov, name))
            row = cur.fetchone()
            if not row:
                continue
            mid, note = row[0], (row[1] or "").rstrip()
            if stop_open(note):
                continue        # 끊긴 것으로 이미 적혀 있다
            new = (note + "\n" + line) if note else line
            if len(new) > NOTE_MAX:
                continue        # 칸이 참. 넘치면 적지 않고 넘어간다
            cur.execute("UPDATE model SET note = %s WHERE model_id = %s", (new, mid))
            n += 1
    conn.commit()
    return n


def note_back(conn, rows, date_str):
    """다시 공지된 모델의 마지막 줄 뒤에 '(날짜부터 다시 공지됨)' 을 붙인다.
    반환: 붙인 모델 수. rows = 오늘 저장본 줄 목록(dict).

    지우지 않고 붙이는 이유 = 끊긴 횟수가 그대로 남는다(2026-08-10 사용자 결정).
    """
    today = {}
    for r in rows:
        today.setdefault(r["provider"], set()).add(split_period(r["model"])[0])
    cur = conn.cursor()
    cur.execute("SELECT p.name, m.name, m.model_id, m.note FROM model m "
                "JOIN provider p ON p.provider_id = m.provider_id "
                "WHERE m.note LIKE %s", ("%" + NOTE_STOP + "%",))
    found = cur.fetchall()
    n = 0
    for prov, name, mid, note in found:
        note = (note or "").rstrip()
        if not stop_open(note):
            continue            # 이미 복귀 표시가 붙었거나 다른 내용이다
        if name not in today.get(prov, set()):
            continue
        new = f"{note} ({date_str}{NOTE_BACK})"
        if len(new) > NOTE_MAX:
            continue
        cur.execute("UPDATE model SET note = %s WHERE model_id = %s", (new, mid))
        n += 1
    conn.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="넣을 날짜 (YYYY-MM-DD)")
    ap.add_argument("--all", action="store_true", help="저장본 전부 넣기")
    ap.add_argument("--verify", help="넣은 값과 JSON 대조 (YYYY-MM-DD)")
    ap.add_argument("--note", nargs=3, metavar=("제공사", "모델명", "사유"),
                    help="모델 비고를 적는다(덮어씀). 이름 변경처럼 사람만 아는 사유용")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.note:
            prov, model, note = args.note
            try:
                n, old = set_note(conn, prov, model, note)
            except ValueError as e:
                print(f"[실패] {e}")
                sys.exit(1)
            if not n:
                print(f"[실패] 그런 모델이 DB에 없습니다: {prov} / {model}")
                sys.exit(1)
            if old:
                print("[비고] 덮어쓰기 전 값:")
                for ln in old.splitlines():
                    print("    " + ln)
            print(f"[비고] {prov} / {model}: {note}")
            sys.exit(0)

        if args.verify:
            bad = False
            raw_diffs, n_raw_json, n_raw_db = verify_raw(conn, args.verify)
            print(f"[대조 1: 값만] JSON 값 {n_raw_json}개 · DB {n_raw_db}개")
            if raw_diffs:
                bad = True
                print(f"  개수가 다른 것 {len(raw_diffs)}건")
                for k, a, b in raw_diffs[:20]:
                    print(f"   {k} JSON={a}개 DB={b}개")
            else:
                print("  전부 일치")

            n_json, n_db, diffs = verify(conn, args.verify)
            print(f"[대조 2: 축까지] JSON {n_json}줄 · DB {n_db}줄")
            if diffs:
                bad = True
                print(f"  다른 것 {len(diffs)}건")
                for k, a, b in diffs[:20]:
                    print(f"   {k} JSON={a} DB={b}")
            else:
                print("  전부 일치")
            sys.exit(1 if bad else 0)

        dates = []
        if args.all:
            dates = sorted(f[:-5] for f in os.listdir(SNAP_DIR) if f.endswith(".json"))
        elif args.date:
            dates = [args.date]
        else:
            ap.error("--date 또는 --all 또는 --verify 중 하나가 필요합니다")

        for d in dates:
            n, n_chg, warns = load(conn, d)
            print(f"[적재] {d}: 관측 {n}줄" + (f" · 변동 {n_chg}건" if n_chg else ""))
            for w in warns:
                print("  [경고]", w)
    except Exception:
        # 실패하면 그날 것을 지운 상태로 두지 않는다(지우기와 넣기가 한 묶음)
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
