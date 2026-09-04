"""보관 페이지를 지금 파서로 다시 읽어 그 날짜의 정본(스냅샷·변동 JSON·DB)을 갱신하는 적용기.

reparse_check.py(판정: 값이 달라지는 날짜 찾기)의 짝. 판정에서 달라진 날짜만 여기에 넣는다.
2026-09-04 첫 사용: Anthropic 가격 표의 캐시 읽기 열 이름이 09-02 에 바뀌어 cache_read 17줄이
09-02·09-03 저장본과 DB 에서 빠졌다. 그 두 날 md.gz 에는 값이 남아 있어 다시 읽어 채운다.

run.py 의 정본 쓰기 경로를 그대로 따른다 - 파싱 -> in_effect 거르기 -> 회사별 기준선 ->
diff.compute_row_changes -> storage.save_snapshot / save_changes -> db.load.
원문 조각(excerpts)·메일·gh-pages·아는 모델 대조는 하지 않는다(그날 수집 시점의 일이라 되돌려 만들지 않음).
status(응답 코드·크기·보관 경로)는 그날 저장본의 것을 그대로 두고 줄 수·경고만 새로 적는다.
generated_at 도 그날 것을 보존한다(다시 읽은 시각은 meta.reparsed 에 남긴다).
★날짜는 오름차순으로 넣는다. 뒷날의 변동은 앞날(기준선)이 고쳐진 뒤 다시 계산돼야 한다.
★기본은 미리보기(쓰지 않음). --write 를 줘야 정본에 쓴다. 쓰기 전에 스냅샷·변동 JSON 을 백업해 둘 것.

쓰는 법: .venv/bin/python reparse_apply.py 2026-09-02 2026-09-03 2026-09-04 [--write] [--reason "사유"]
"""
import argparse
import datetime
import gzip
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

from adapters.registry import ALL_ADAPTERS  # noqa: E402
from adapters.base import EmptyParseError  # noqa: E402
import diff  # noqa: E402
import storage  # noqa: E402
import db  # noqa: E402

# 페이지 파일 이름 -> 어댑터 (reparse_check.py 와 같은 대응)
BY_FILE = {ad.__class__.__module__.rsplit(".", 1)[-1]: ad for ad in ALL_ADAPTERS}


def in_effect(rows, date_str):
    """그날 적용되는 단가 줄만 남긴다. 판정 규칙 = db.applies_on (run.py in_effect 와 같음)."""
    keep, dropped = [], []
    for r in rows:
        if db.applies_on(r.model, date_str, getattr(r, "effective_from", None)):
            keep.append(r)
        else:
            dropped.append(f"{r.provider} {r.model} {r.tier} {r.item}")
    return keep, dropped


def parse_day(date, orig_status):
    """그날 보관 페이지 전부를 지금 코드로 읽는다. 반환: (PriceRow 목록, status 목록)."""
    by_prov = {s["provider"]: s for s in orig_status}
    rows, status = [], []
    for stem, ad in BY_FILE.items():
        kind = "md" if getattr(ad, "md_url", "") else "html"
        f = f"data/pages/{date}/{stem}.{kind}.gz"
        base = dict(by_prov.get(ad.provider, {"provider": ad.provider, "attempts": 1}))
        base.pop("error", None)
        base.pop("fail_kind", None)
        base.pop("fail_kinds", None)
        if not os.path.exists(f):
            base.update(ok=False, count=0, error=f"{kind}.gz 없음", fail_kind="fetch")
            status.append(base)
            continue
        text = gzip.open(f, "rt", encoding="utf-8", errors="replace").read()
        try:
            recs, warns = ad.run_rows(text=text)
        except Exception as e:
            base.update(ok=False, count=0, error=str(e),
                        fail_kind="empty" if isinstance(e, EmptyParseError) else "fetch")
            status.append(base)
            continue
        rows.extend(recs)
        base.update(ok=True, count=len(recs), warns=warns)
        status.append(base)
    return rows, status


def kind_counts(changes):
    return Counter((c["provider"], c["kind"]) for c in changes)


def process(date, write, reason):
    snap = storage.load_snapshot(date)
    if "rows" not in snap:
        print(f"[{date}] 옛 형식 저장본 - 건너뜀")
        return
    rows, status = parse_day(date, snap.get("status") or [])
    rows, dropped = in_effect(rows, date)
    kept = Counter(r.provider for r in rows)
    for s in status:
        if s.get("ok"):
            s["count"] = kept.get(s["provider"], 0)
    failed = [s["provider"] for s in status if not s["ok"]]
    row_dicts = [r.to_dict() for r in rows]

    prev_by_provider, prev_rows = {}, []
    for ad in ALL_ADAPTERS:
        if ad.provider in failed:
            continue
        d, psnap = storage.previous_readable(date, provider=ad.provider)
        if d is None:
            continue
        prev_by_provider[ad.provider] = d
        prev_rows.extend(r for r in psnap["rows"] if r.get("provider") == ad.provider)
    changes = []
    if prev_by_provider:
        changes = diff.compute_row_changes(prev_rows, row_dicts, failed_providers=set(failed))

    old_rows = len(snap["rows"])
    old_changes = storage.load_changes(date)
    n_models = len({(r.provider, r.model) for r in rows})
    print(f"\n[{date}] 줄 {old_rows} -> {len(rows)} · 모델 {n_models} · 걸러냄 {len(dropped)}"
          f" · 실패 {failed or '없음'} · 기준선 {prev_by_provider}")
    for s in status:
        if s.get("ok"):
            print(f"    {s['provider']:11s} {s['count']:4d}줄" + (f"  경고 {s['warns']}" if s.get("warns") else ""))
    print(f"    변동 기존 {len(old_changes)}건 -> 다시 계산 {len(changes)}건")
    oc, nc = kind_counts(old_changes), kind_counts(changes)
    for key in sorted(set(oc) | set(nc)):
        if oc.get(key, 0) != nc.get(key, 0):
            print(f"      {key[0]:11s} {key[1]:8s} {oc.get(key, 0):3d} -> {nc.get(key, 0):3d}")
    for c in changes:
        if c["kind"] == "removed":
            print(f"      [removed 남음] {c['provider']} {c['model']} {c.get('tier')} {c['item']}")
    if dropped:
        for t in dropped:
            print(f"      [걸러냄] {t}")

    if not write:
        return
    meta = dict(snap.get("meta") or {})
    meta.update(providers_ok=sum(1 for s in status if s["ok"]), models=n_models,
                rows=len(rows), baselines=prev_by_provider)
    meta["reparsed"] = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "reason": reason, "rows_before": old_rows,
                        "changes_before": len(old_changes)}
    path = storage.save_snapshot(date, rows, meta=meta, status=status,
                                 review=snap.get("review"))
    payload = storage.load_snapshot(date)
    payload["generated_at"] = snap["generated_at"]        # 수집 시각은 그날 것
    storage.write_json(path, payload)
    if prev_by_provider:
        storage.save_changes(date, changes)
    conn = db.connect()
    try:
        n_pt, n_chg, warns = db.load(conn, date)
    finally:
        conn.close()
    print(f"    [씀] 스냅샷 {len(rows)}줄 · 변동 {len(changes)}건 · DB 관측 {n_pt}줄 · 변동 {n_chg}건"
          + (f" · 경고 {warns}" if warns else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dates", nargs="+", help="YYYY-MM-DD (오름차순)")
    ap.add_argument("--write", action="store_true", help="정본(스냅샷·변동·DB)에 쓴다. 없으면 미리보기")
    ap.add_argument("--reason", default="보관 페이지 재파싱(파서 수정 소급)")
    args = ap.parse_args()
    if args.dates != sorted(args.dates):
        print("날짜는 오름차순으로 주세요 - 뒷날 변동은 앞날 기준선이 고쳐진 뒤 계산돼야 합니다")
        return 2
    print("모드:", "쓰기" if args.write else "미리보기(쓰지 않음)")
    for d in args.dates:
        process(d, args.write, args.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
