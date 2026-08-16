"""보관 페이지를 지금 코드로 다시 읽어 그날 저장본과 맞대는 대조기 (2026-08-15 새 경로판).

★파서를 고칠 때마다 돌린다(2026-08-14 사용자 확정 소급 정책). 값이 달라지면
그 날짜는 소급 보정 대상이고, 안 달라지면 코드만 고치고 끝낸다. 고쳤다는
사실이 아니라 값이 달라지는지로 판정한다.
★새 DB 는 2026-08-15 저녁부터 새 형식(rows) 저장본만 있다. 그 앞 날짜의 보관 페이지는
읽어 결과를 남기되(파서 회귀 확인용) 맞댈 저장본이 없어 "기준 없음"으로 적힌다.

쓰는 법: .venv/bin/python reparse_check.py [기준파일.json]
  인자를 주면 그 파일과 이번 결과를 맞대고(코드 수정 전후 비교),
  안 주면 저장본(data/snapshots/*.json, rows 형식)과 맞댄다.
결과는 stdout 요약 + 같은 폴더의 data/reparse_result.json 에 전체.
"""
import glob
import gzip
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

from adapters.registry import ALL_ADAPTERS  # noqa: E402
import diff  # noqa: E402

OUT = os.path.join(BASE, "data", "reparse_result.json")
# 페이지 파일 이름 -> 어댑터. registry 의 provider 이름과 파일명이 달라 직접 맺는다
BY_FILE = {}
for ad in ALL_ADAPTERS:
    BY_FILE[ad.__class__.__module__.rsplit(".", 1)[-1]] = ad


def _key(d):
    return "::".join(str(x) for x in diff.row_key(d))


def parse_day(date):
    """그날 보관 페이지 전부를 지금 코드로 읽는다. {키(축 12개): 값}

    마크다운 회사(md_url 선언)는 md.gz 를, 아니면 html.gz 를 읽는다. 마크다운 회사인데
    그날 md.gz 가 없으면(2026-08-14 이전 보관분) 건너뛰고 오류에 적는다 - HTML 을 새
    파서에 넣으면 0줄이라 파서 고장과 구분이 안 된다.
    """
    out, errs = {}, []
    for stem, ad in BY_FILE.items():
        kind = "md" if getattr(ad, "md_url", "") else "html"
        f = f"data/pages/{date}/{stem}.{kind}.gz"
        if not os.path.exists(f):
            errs.append(f"{stem}: {kind}.gz 없음")
            continue
        text = gzip.open(f, "rt", encoding="utf-8", errors="replace").read()
        try:
            rows, _ = ad.run_rows(text=text)
        except Exception as e:
            errs.append(f"{stem}: {type(e).__name__}: {e}")
            continue
        for r in rows:
            out[_key(r.to_dict())] = r.value
    return out, errs


def load_snapshot(date):
    p = f"data/snapshots/{date}.json"
    if not os.path.exists(p):
        return None
    snap = json.load(open(p, encoding="utf-8"))
    if "rows" not in snap:
        return None          # 옛 형식 저장본은 맞댈 수 없다
    return {_key(d): d["value"] for d in snap["rows"]}


def compare(mine, theirs):
    """(달라진 것, 새로 생긴 것, 사라진 것)."""
    changed, added, gone = [], [], []
    for k, v in mine.items():
        if k not in theirs:
            added.append(k)
        elif theirs[k] != v:
            changed.append((k, theirs[k], v))
    for k in theirs:
        if k not in mine:
            gone.append(k)
    return changed, added, gone


def main():
    ref = None
    if len(sys.argv) > 1:
        ref = json.load(open(sys.argv[1], encoding="utf-8"))
        print(f"기준 = {sys.argv[1]} (코드 수정 전 결과)")
    else:
        print("기준 = data/snapshots/*.json (그날 저장본, rows 형식만)")

    dates = sorted(os.path.basename(d) for d in glob.glob("data/pages/*")
                   if os.path.isdir(d))
    result, n_chg, n_add, n_gone, n_elem = {}, 0, 0, 0, 0
    for date in dates:
        mine, errs = parse_day(date)
        base = (ref or {}).get(date, {}).get("parsed") if ref else load_snapshot(date)
        n_elem += len(mine)
        row = {"parsed": mine, "errors": errs}
        if base is None:
            row["note"] = "맞댈 기준 없음"
        else:
            chg, add, gone = compare(mine, base)
            row["changed"], row["added"], row["gone"] = chg, add, gone
            n_chg += len(chg)
            n_add += len(add)
            n_gone += len(gone)
        result[date] = row
        mark = ""
        if base is not None:
            mark = (f"  달라짐 {len(row['changed'])} · 새로 {len(row['added'])} · "
                    f"빠짐 {len(row['gone'])}")
        print(f"  {date}  줄 {len(mine)}개{mark}"
              + (f"  [오류·건너뜀 {len(errs)}]" if errs else ""))
        for e in errs:
            print(f"      {e}")

    json.dump({d: {"parsed": r["parsed"]} for d, r in result.items()},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n날짜 {len(dates)}일 · 다시 읽은 값 {n_elem}개")
    print(f"달라진 값 {n_chg} · 기준에 없던 줄 {n_add} · 기준에만 있던 줄 {n_gone}")
    print(f"전체 결과: {OUT}")

    for date, r in result.items():
        for k, a, b in r.get("changed", [])[:200]:
            print(f"  [달라짐] {date} {k}: {a} -> {b}")
        for k in r.get("added", [])[:200]:
            print(f"  [기준에 없음] {date} {k}")
        for k in r.get("gone", [])[:200]:
            print(f"  [기준에만 있음] {date} {k}")


if __name__ == "__main__":
    main()
