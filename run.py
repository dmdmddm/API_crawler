"""오케스트레이터: 회사별 수집 코드 전부 실행 -> 스냅샷 저장 -> 변동 감지 -> 대시보드 -> 알림.

2026-08-15 새 경로 전환: 어댑터 run_rows()(값마다 한 줄 PriceRow) · 저장본 'rows' 형식 ·
diff.compute_row_changes(축 12개) · db.load(줄 그대로 적재) · 알림은 대표 라인업만
(mailer.is_representative). 옛 경로(PriceRecord·parse()·여섯 칸 diff)는 이날 삭제했다.

사용:
  python run.py                     # 오늘 날짜로 실시간 수집
  python run.py --offline           # 보관해 둔 페이지로 수집(시험용. data/dry/ 에만 씀)
  python run.py --date 2026-07-15   # 특정 날짜 지정
  python run.py --rebuild 2026-07-25  # 저장된 값으로 화면만 다시 그림(재수집 안 함)

설계 근거(2026-07-25, 일부 2026-07-27 개정):
- cron에는 재시도 기능이 없으므로 제공사별 재시도를 여기서 한다.
- 같은 시각에 두 번 돌지 않도록 파일 잠금을 건다(앞 실행이 재시도로 길어질 때 대비).
- 수집 실패는 실패로 남기고 직전 값으로 채우지 않는다. 실패한 제공사는 그날 변동
  비교에서 제외한다(그러지 않으면 그 제공사 모델이 전부 '사라짐'으로 기록된다).
- 큰 변동이나 수집 실패가 있으면 '확인 필요'로 표시하고 종료 코드를 1로 낸다.
  ~~값은 저장하되 사람이 확인하기 전까지 확정으로 취급하지 않는다~~ (2026-07-27 폐기:
  사람 승인 단계를 뺐다. 매일 확인하지 않을 것이므로 승인으로 비교 기준을 미루면
  기준이 계속 과거에 머문다. 알림·화면 표시·종료 코드로만 남긴다.)
- 수집된 모델이 0개면 화면을 다시 그리지 않는다. 저장은 실패를 실패로 남기지만
  화면은 덮어쓰기라, 그냥 두면 빈 페이지가 마지막 화면을 지운다.
"""
from __future__ import annotations
import argparse
import datetime
import fcntl
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters.registry import ALL_ADAPTERS
from adapters.base import EmptyParseError
import storage
import diff
import dashboard
import pages

BASE = os.path.dirname(os.path.abspath(__file__))
PAGE_DIR = os.path.join(BASE, "data", "pages")
LOCK_PATH = os.path.join(BASE, "data", ".run.lock")
KST = datetime.timezone(datetime.timedelta(hours=9))

RETRIES = 3          # 제공사별 재시도 횟수
RETRY_DELAY = 5      # 재시도 간격(초)

# 아는 모델 목록을 몇 일치까지 볼 것인가. 이보다 오래 안 보인 모델은 목록에서 뺀다.
# 목록이 무한히 커지는 것을 막는 자리다. 매일 오는 경고를 멈추는 일은 이 상수가
# 아니라 missing_models() 의 '안 온 첫날만' 걸러내기가 한다(2026-08-10).
KNOWN_WINDOW_DAYS = 30

# 종료 코드. 사유가 겹칠 수 있어 자리별로 뜻을 나눠 더한다(최대 127).
# 가장 심각한 것 하나만 내면 나머지가 가려지기 때문.
EXIT_SPIKE   = 1     # 큰 변동(2배 이상)
EXIT_FETCH   = 2     # 수집 실패(연결 안 됨)
EXIT_EMPTY   = 4     # 0개 파싱(응답은 받았는데 한 건도 못 뽑음)
EXIT_MISSING = 8     # 아는 모델이 안 옴
EXIT_REMOVED = 16    # 기준선 대비 모델 사라짐
EXIT_DB      = 32    # DB 적재 실패
EXIT_WARN    = 64    # 수집 코드 경고(값 범위·항목 누락·교차 대조 불일치)
EXIT_LOCKED  = 128   # 중복 실행 차단. 위 자리들과 안 겹치게 128 하나로 둔다
EXIT_CRASH   = 255   # 잡히지 않은 예외로 중단. 비트 조합(최대 127)·LOCKED 와 안 겹치는 최댓값

EXIT_LABEL = {
    EXIT_SPIKE: "큰 변동", EXIT_FETCH: "수집 실패", EXIT_EMPTY: "0개 파싱",
    EXIT_MISSING: "모델 안 옴", EXIT_REMOVED: "모델 사라짐",
    EXIT_DB: "DB 적재 실패", EXIT_WARN: "수집 경고",
}


def code_text(code):
    """종료 코드를 사람이 읽는 말로. 숫자만 남기면 나중에 뜻을 찾아봐야 한다."""
    if code == EXIT_LOCKED:
        return "중복 실행 차단"
    if code == EXIT_CRASH:
        return "잡히지 않은 예외로 중단"
    if not code:
        return "정상"
    return " + ".join(t for b, t in sorted(EXIT_LABEL.items()) if code & b)


def acquire_lock():
    """중복 실행 방지. 이미 돌고 있으면 None."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def crawl(offline=False, page_dir=None):
    """제공사별 수집. 실패는 재시도 후에도 안 되면 실패로 기록(직전 값으로 채우지 않음).

    page_dir: 라이브 수집일 때 받은 페이지를 남길 디렉토리.
    fail_kind: 실패를 'fetch'(연결 안 됨)와 'empty'(0개 파싱)로 나눠 적는다.
      둘은 원인도 대응도 달라서, 종료 코드와 로그에서 구분되어야 한다.
    """
    rows, status = [], []
    for ad in ALL_ADAPTERS:
        attempts = 1 if offline else RETRIES
        last_err, kinds = None, set()
        for attempt in range(1, attempts + 1):
            try:
                if offline:
                    recs, warns = ad.run_rows(text=pages.fixture_source(ad))
                else:
                    recs, warns = ad.run_rows(page_dir=page_dir)
                rows.extend(recs)
                status.append({"provider": ad.provider, "ok": True,
                               "count": len(recs), "warns": warns,
                               "attempts": attempt, **_fetch_fields(ad)})
                print(f"  [OK ] {ad.provider}: {len(recs)}줄"
                      + (f"  (재시도 {attempt}회째)" if attempt > 1 else "")
                      + (f"  경고 {warns}" if warns else ""))
                break
            except Exception as e:
                last_err = e
                kinds.add("empty" if isinstance(e, EmptyParseError) else "fetch")
                if attempt < attempts:
                    print(f"  [..] {ad.provider}: 실패({e}) → {RETRY_DELAY}초 후 재시도")
                    time.sleep(RETRY_DELAY)
        else:
            # 여러 번 시도하며 실패 종류가 섞일 수 있다. 한 번이라도 0개 파싱이
            # 있었으면 그쪽을 대표로 삼는다. 페이지에는 닿았는데 못 뽑았다는 뜻이라
            # 연결 실패보다 진단 가치가 크고, 뒤 시도의 연결 실패에 덮이면 안 된다
            # (2026-07-28 Codex 적대검증에서 실제로 덮이는 것이 확인됨).
            last_kind = "empty" if "empty" in kinds else "fetch"
            status.append({"provider": ad.provider, "ok": False, "count": 0,
                           "error": str(last_err), "fail_kind": last_kind,
                           "fail_kinds": sorted(kinds),
                           "attempts": attempts, **_fetch_fields(ad)})
            kind_ko = "0개 파싱" if last_kind == "empty" else "연결 실패"
            print(f"  [ERR] {ad.provider}: {attempts}회 시도 실패({kind_ko}) - {last_err}")
    return rows, status


def _fetch_fields(ad):
    """그 회사에서 받은 페이지의 정황(응답 코드·크기·보관 경로)만 추려 status에 넣는다."""
    info = getattr(ad, "fetch_info", None) or {}
    return {k: v for k, v in info.items() if v is not None}


def trend_history():
    """가격 추이 그래프에 쓸 시계열. DB를 못 읽으면 None 을 돌려준다.

    ★그래프 하나 때문에 화면 전체를 못 만들면 안 된다. DB가 죽어 있어도 그날 값과
    변동 목록은 나와야 하므로, 여기서 막고 그래프 절만 빠지게 한다.
    """
    try:
        import db
        conn = db.connect()
        try:
            return db.history(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"[그래프] 추이를 못 읽음({type(e).__name__}) - 그래프 없이 만듭니다")
        return None


def in_effect(rows, date_str):
    """그날 실제로 적용되는 단가 줄만 남긴다. 반환: (남긴 것, 뺀 것의 설명 목록)

    ★회사가 인상을 미리 공지하면 같은 모델에 단가가 두 벌 올라온다. 2026-08-10
    Anthropic 페이지에 이렇게 적혀 있었다.
      Claude Sonnet 5 through August 31, 2026     $2 / MTok
      Claude Sonnet 5 starting September 1, 2026  $3 / MTok
    둘 다 받으면 "오늘 얼마인가"에 답이 둘이 된다. 아직 시작 안 한 단가는 받지
    않는다(2026-08-10 사용자 결정).

    적용 시기는 모델 이름에 붙어 온다('... through August 31, 2026'). 이름에서
    떼어 그날과 견준다. 날짜는 YYYY-MM-DD 라 글자 그대로 비교해도 순서가 맞는다.

    ★회사별 수집 코드가 아니라 여기서 거른다. 어느 회사가 예고하든 같이 걸린다.
    ★이미 끝난 단가도 뺀다. 회사가 지난 줄을 안 지우고 두는 경우를 막는다.
    ★판정 규칙은 db.applies_on() 한 곳에 있다. 여기서 다시 적으면 적재·대조 쪽과
      따로 놀게 된다(2026-08-10).
    """
    import db
    keep, dropped = [], []
    for r in rows:
        ef = getattr(r, "effective_from", None)
        if db.applies_on(r.model, date_str, ef):
            keep.append(r)
        else:
            _, name_ef, _ = db.split_period(r.model)
            why = "아직 시작 안 함" if (ef or name_ef) else "이미 끝남"
            dropped.append(f"{r.provider} {r.model} {r.tier} {r.item} ({why})")
    return keep, dropped


def known_models(date_str):
    """DB에 쌓인 '지금까지 본 모델' 목록. 반환: {제공사: {이름: 마지막으로 본 날}}

    마지막으로 본 지 KNOWN_WINDOW_DAYS 넘은 모델은 뺀다.
    ★날짜를 같이 돌려주는 이유 = missing_models() 가 '안 온 첫날'을 가려내는 데 쓴다.
    """
    import db
    cutoff = (datetime.date.fromisoformat(date_str)
              - datetime.timedelta(days=KNOWN_WINDOW_DAYS)).isoformat()
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT p.name, m.name, m.last_seen FROM model m "
                    "JOIN provider p ON p.provider_id = m.provider_id "
                    "WHERE m.last_seen >= %s", (cutoff,))
        out = {}
        for prov, name, last_seen in cur.fetchall():
            out.setdefault(prov, {})[name] = last_seen
        return out
    finally:
        conn.close()


def missing_models(known, row_dicts, skip):
    """아는 모델 중 오늘 처음 안 온 것. {제공사: [이름, ...]}

    DB는 모델명에서 적용 시기 문구를 떼고 저장한다('Claude Sonnet 5 through
    August 31, 2026' -> 'Claude Sonnet 5'). 오늘 값도 같은 규칙으로 떼고 비교해야
    Anthropic이 매일 어긋나지 않는다.

    ★안 온 첫날 한 번만 알린다(2026-08-10 확정). 그 회사를 마지막으로 수집한 날
    = 그 회사 모델들의 last_seen 중 가장 늦은 날이고, 안 온 모델의 last_seen 이
    그 날과 같으면 오늘이 첫날이다. 그보다 오래됐으면 지난번에 이미 알린 모델이다.
    이 걸러내기가 없으면 같은 경고가 KNOWN_WINDOW_DAYS 인 30일 동안 매일 온다.
    """
    import db
    today = {}
    for r in row_dicts:
        today.setdefault(r["provider"], set()).add(db.split_period(r["model"])[0])
    out = {}
    for prov, seen in known.items():
        if prov in skip:
            continue
        gone = sorted(set(seen) - today.get(prov, set()))
        if not gone:
            continue
        last_run = max(seen.values())
        first_day = [n for n in gone if seen[n] == last_run]
        if first_day:
            out[prov] = first_day
    return out


def split_by_page(date_str, by_provider):
    """'없어졌다'는 판정을 그날 페이지로 되짚는다.

    반환: (의심 {제공사: [이름]}, 진짜 없음 {제공사: [이름]}, 못 읽은 사유 목록)

    ★2026-08-12 사고를 막는 자리. OpenAI 가 가격 데이터를 표에서 <astro-island>
      속성 안 JSON 으로 옮기자 수집 코드가 9개 중 4개만 읽었다. 4개는 읽혔으니
      수집은 성공으로 남았고, 나머지 6개가 '사라진 모델'로 판정됐다. 메일이 잘못
      나가고 model.note 에 "2026-08-12부터 API 가격이 공지되지 않음" 이 6건 잘못
      적혔다. 그날 보관 페이지에는 6개 이름이 3-4회씩 그대로 있었다.

    회사가 정말 내린 모델과 우리가 못 읽은 모델은 대응이 다르다. 앞은 남길 사실,
    뒤는 고칠 코드다. 페이지에 이름이 남아 있으면 뒤로 본다.

    ★페이지에 이름이 있어도 가격표가 아닌 자리(변경 안내 글 등)일 수 있다. 그래서
      의심도 사람에게 알리고 종료 코드를 올린다. 다만 DB 에 "공지되지 않음"이라는
      사실로는 적지 않는다. 잘못 알리는 것은 되돌릴 수 있지만 잘못 적은 기록은
      남는다.
    ★페이지를 못 읽으면 종전대로 '진짜 없음'으로 둔다. 확인을 못 했다고 해서
      알림을 줄이면, 진짜로 사라진 날을 조용히 넘긴다.
    """
    suspect, real, errs = {}, {}, []
    for prov, names in sorted(by_provider.items()):
        names = sorted(set(names))
        found, err = pages.seen_in_page(date_str, prov, names)
        if err:
            errs.append(f"{prov} 페이지를 다시 못 읽어 확인 못 함({err})")
            real[prov] = names
            continue
        still = [n for n in names if n in found]
        gone = [n for n in names if n not in found]
        if still:
            suspect[prov] = still
        if gone:
            real[prov] = gone
    return suspect, real, errs


def _model_sig(items, value_key):
    """한 모델의 줄 묶음 -> (모델 이름을 뺀 축들, 값) 정렬 튜플. 이름만 다른 짝 찾기용."""
    axes = [a for a in diff.ROW_AXES if a != "model"]
    return tuple(sorted((tuple(c.get(a) or "" for a in axes), c.get(value_key))
                        for c in items))


def rename_pairs(changes):
    """사라진 모델과 새로 생긴 모델 중 줄 구성·값이 전부 같은 짝. 반환: [(제공사, 옛 이름, 새 이름)]

    회사가 표기를 바꾸면(예: 'Claude Opus 5' -> 'Claude Opus 5 (Standard)')
    우리 눈에는 삭제 + 추가로 보인다. 줄이 전부 같으면 같은 모델일 가능성이 높다.
    진단용 표시일 뿐이고, 자동으로 같은 모델로 합치지는 않는다.
    """
    removed, added = {}, {}
    for c in changes:
        if c["kind"] == "removed":
            removed.setdefault((c["provider"], c["model"]), []).append(c)
        elif c["kind"] == "added":
            added.setdefault((c["provider"], c["model"]), []).append(c)
    used, pairs = set(), []
    for (prov, old), items in removed.items():
        sig = _model_sig(items, "old_value")
        for (p2, new), items2 in added.items():
            if p2 != prov or (p2, new) in used:
                continue
            if _model_sig(items2, "new_value") == sig:
                used.add((p2, new))
                pairs.append((prov, old, new))
                break
    return pairs


def removed_models(changes, row_dicts):
    """줄이 사라진 모델 중 오늘 줄이 하나도 없는 것 = 모델 자체가 목록에서 빠진 것.
    반환: [(제공사, 모델)]. 줄 몇 개만 사라진 모델(등급 하나가 없어진 것 등)은 여기 안 든다."""
    today = {(r["provider"], r["model"]) for r in row_dicts}
    gone = {(c["provider"], c["model"]) for c in changes if c["kind"] == "removed"}
    return sorted(gone - today)


def notify(date_str, changes, summary, prev_date, review, prev_by_provider=None):
    """알림 훅. 지금은 콘솔 출력. 이메일/슬랙/웹훅은 여기에 연결.

    prev_by_provider: {제공사: 그 회사의 기준선 날짜}. 회사마다 마지막 성공일이
    다를 수 있으므로(어느 날 한 곳만 실패하면 그때부터 갈린다) 다르면 같이 적는다.
    ★'비교할 기준선이 없음'과 '비교했는데 변동이 없음'을 구분해서 적는다. 그전에는
    둘 다 "변동 없음"으로 나와서, 전부 실패한 날 다음의 실제 가격 인상이 묻혔다.
    """
    prev_by_provider = prev_by_provider or {}
    if not prev_by_provider:
        print("[알림] 비교할 기준선 없음 → 최초 기준선. 변동 비교는 다음 수집부터.")
    elif summary["total"] == 0:
        print(f"[알림] 변동 없음 (vs {prev_date}).")
    else:
        print(f"[알림] 변동 {summary['total']}건 (vs {prev_date}): "
              f"추가 {summary['added']} / 변경 {summary['changed']} / 삭제 {summary['removed']}"
              + (f" / 큰 변동 {summary['spikes']}" if summary.get("spikes") else ""))
        for c in changes[:80]:
            where = " ".join(x for x in (c["model"], c.get("tier"), c.get("context"),
                                          c.get("modality"), c.get("variant"),
                                          c.get("cache_ttl"), c["item"]) if x
                             and x not in ("standard", "default"))
            if c["kind"] == "changed":
                mark = " ★큰 변동" if c.get("spike") else ""
                pct = c.get("pct")
                rate = f" ({pct:+}%)" if pct is not None else ""
                print(f"    · {c['provider']} {where}: "
                      f"{c['old_value']} → {c['new_value']}{rate}{mark}")
            else:
                print(f"    · [{c['kind']}] {c['provider']} {where}")
        if len(changes) > 80:
            print(f"    · ... 외 {len(changes) - 80}줄 (data/changes/ 파일에 전부)")

    # 회사마다 기준선이 갈렸으면 어느 날과 비교한 것인지 적는다
    if len(set(prev_by_provider.values())) > 1:
        bits = ", ".join(f"{p} vs {d}" for p, d in sorted(prev_by_provider.items()))
        print(f"[알림] 비교 기준선이 회사마다 다름: {bits}")

    for prov, old, new in rename_pairs(changes):
        print(f"[알림] 이름만 바뀐 것으로 보임: {prov} {old} → {new}")
    if review["required"]:
        print(f"[확인 필요] {' / '.join(review['reasons'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="스냅샷 날짜(기본 오늘)")
    ap.add_argument("--offline", action="store_true",
                    help="보관해 둔 페이지로 수집(pages.FIXTURE_DATE 날짜)")
    ap.add_argument("--rebuild", metavar="DATE", default=None,
                    help="저장된 값으로 화면만 다시 그림(재수집 안 함)")
    args = ap.parse_args()

    if args.rebuild:
        # 화면 표시 규칙(주력 목록 등)만 바꿨을 때 쓴다. 공식 페이지를 다시 부르지 않는다.
        snap = storage.load_snapshot(args.rebuild)
        prev_date, _ = storage.previous_readable(args.rebuild)
        changes = storage.load_changes(args.rebuild)
        # 저장본에 적힌 수집 시각을 그대로 쓴다. 안 넘기면 화면이 날짜만 적어
        # '마지막 수집'이 몇 시였는지 사라진다
        collected_at = None
        gen = snap.get("generated_at")
        if gen:
            try:
                collected_at = (datetime.datetime.fromisoformat(gen).astimezone(KST)
                                .strftime("%Y-%m-%d %H:%M:%S (KST)"))
            except ValueError:
                pass
        out_path = dashboard.build(args.rebuild, snap["rows"], changes,
                                   snap.get("status", []), prev_date, collected_at,
                                   review=snap.get("review"), history=trend_history())
        print(f"[재생성] {out_path} · {len(snap['rows'])}줄 · 변동 {len(changes)}건")
        return 0

    lock = acquire_lock()
    if lock is None:
        print("[중단] 이미 실행 중입니다(중복 실행 방지).")
        print(f"[종료] 코드 {EXIT_LOCKED} ({code_text(EXIT_LOCKED)})")
        return EXIT_LOCKED

    try:
        date_str = args.date or datetime.datetime.now(KST).date().isoformat()
        print(f"[수집] {date_str} (offline={args.offline})")
        # 시험 실행은 페이지를 안 남긴다(정본 격리 규칙과 같다)
        page_dir = None if args.offline else os.path.join(PAGE_DIR, date_str)
        rows, status = crawl(offline=args.offline, page_dir=page_dir)
        # 아직 시작 안 했거나 이미 끝난 단가는 여기서 뺀다. 뒤로 넘어가면
        # 저장본·변동 비교·화면·DB 가 전부 그것을 오늘 값으로 다룬다
        rows, not_yet = in_effect(rows, date_str)
        if not_yet:
            for m in not_yet:
                print(f"  [제외] {m}")
            # 회사별 개수를 거른 뒤 값으로 맞춘다. 안 맞추면 메일·화면·provider_run
            # 이 '12개 받음'이라 적는데 실제로 들어간 것은 11개가 된다
            kept = {}
            for r in rows:
                kept[r.provider] = kept.get(r.provider, 0) + 1
            for s in status:
                if s.get("ok"):
                    s["count"] = kept.get(s["provider"], 0)
        row_dicts = [r.to_dict() for r in rows]
        failed = [s["provider"] for s in status if not s["ok"]]

        # 비교 기준선을 회사마다 따로 잡는다. 하나로 두면 그 하나가 망가진 날
        # (예: 여섯 곳 전부 실패해 모델 0개로 저장된 날) 전체 비교가 무력해진다.
        # 그러면 다음 날 진짜 가격 인상이 나도 '변동 없음'으로 나간다(2026-07-28 재현).
        prev_by_provider, prev_rows = {}, []
        for ad in ALL_ADAPTERS:
            if ad.provider in failed:
                continue                      # 오늘 실패한 곳은 비교 자체를 안 한다
            d, snap = storage.previous_readable(date_str, provider=ad.provider)
            if d is None:
                continue                      # 그 회사의 성공 이력이 아직 없음(옛 형식 저장본은 안 씀)
            prev_by_provider[ad.provider] = d
            prev_rows.extend(r for r in snap["rows"]
                             if r.get("provider") == ad.provider)

        # 기준선이 없는 회사의 모델은 전부 '추가'로 잡힌다. 그게 맞는 동작이지만
        # (회사를 새로 붙이면 그 모델은 우리에게 진짜로 처음이다) 로그에 안 적으면
        # '진짜 신규'와 '비교할 게 없어서 신규로 보이는 것'이 구분되지 않는다
        no_base = [ad.provider for ad in ALL_ADAPTERS
                   if ad.provider not in failed and ad.provider not in prev_by_provider]
        if no_base and prev_by_provider:
            print(f"  [기준선 없음] {', '.join(no_base)} - 이 회사 모델은 전부 '추가'로 기록됩니다")

        changes = []
        if prev_by_provider:
            changes = diff.compute_row_changes(prev_rows, row_dicts,
                                               failed_providers=set(failed))
        summary = diff.summarize(changes)
        prev_date = max(prev_by_provider.values()) if prev_by_provider else None

        # 변동이 있는 모델마다 그날 페이지의 원문 조각을 뽑아 전날과 대조한다
        # (2026-08-10). 글자만 찾아 오려내므로 표 고르기가 틀려도 안 끌려간다
        # = 파서와 독립된 경로. 조각은 data/excerpts/날짜.json 에만 남기고
        # DB 에는 안 넣는다(사용자 결정).
        # ★시험 실행은 건너뛴다 - 페이지를 안 남기므로 그날 조각 자체가 없고,
        #   보관 페이지로 뽑으면 정본과 날짜가 안 맞는다(아는 모델 대조와 같은 이유).
        # 조각을 못 남겨도 그날 수집은 실패가 아니다(DB·메일과 같은 원칙).
        # 다만 조용히 넘기지 않는다 - 대조를 못 한 것도 알려야 한다.
        # ★뽑기와 파일 쓰기를 따로 잡는다. 한 try 로 묶으면 파일을 못 쓴 순간
        #   대조에서 나온 사유('파서 오류 의심' 등)가 통째로 덮인다
        #   (2026-08-10 검증 지적).
        excerpt_problems, items = [], []
        if changes and not args.offline:
            try:
                items, excerpt_problems = pages.collect(date_str, changes,
                                                        prev_by_provider)
            except Exception as e:
                excerpt_problems = [f"원문 조각을 못 뽑음({type(e).__name__}: {e})"]
            try:
                epath = pages.save(date_str, items, prev_by_provider)
                same = sum(1 for i in items if i.get("same_as_prev"))
                print(f"[원문] {epath} · 모델 {len(items)}개"
                      + (f" · 전날과 같은 것 {same}개" if same else ""))
            except Exception as e:
                excerpt_problems.append(
                    f"원문 조각 파일을 못 남김({type(e).__name__}: {e})")

        # 아는 모델 목록과 대조. 개수가 아니라 이름으로 봐야 무엇이 없어졌는지 안다.
        # ★시험 실행(--offline)은 건너뛴다. 시험이 읽는 페이지는 pages.FIXTURE_DATE
        # 하루로 고정인데 정본 DB·정본 스냅샷은 매일 나아가므로, 날이 갈수록
        # 그 뒤 나온 모델이 전부 '사라졌다'로 뜬다. 시험 실행의 판정이 정본 상태에
        # 끌려가면 안 된다(2026-07-28 Codex 적대검증 지적. 실제로 종료 코드 24가 났다).
        # 2026-08-10 에 시험이 읽는 곳을 data/pages/ 로 옮겼지만 날짜 고정은 그대로다.
        missing, known_err = {}, None
        # 기준선 대비 사라진 모델. 아래 '페이지로 되짚기'가 두 목록을 같이 봐야 해서
        # 여기서 미리 뽑는다(시험 실행은 정본 스냅샷과 비교하게 되므로 건너뛴다)
        # 사라짐 판정은 모델 단위(오늘 줄이 하나도 없는 모델). 등급 하나가 없어진 것 같은
        # 줄 단위 삭제는 변동 목록·메일 본문에 '사라짐' 줄로만 남고 종료 코드는 안 올린다
        removed = [] if args.offline else removed_models(changes, row_dicts)
        suspect, page_errs = {}, []
        if not args.offline:
            try:
                missing = missing_models(known_models(date_str), row_dicts, set(failed))
            except Exception as e:
                known_err = f"{type(e).__name__}: {e}"
            # 없어졌다는 두 판정(아는 모델 대조·기준선 대비)을 그날 페이지로 되짚는다.
            # 페이지에 이름이 남아 있으면 회사가 내린 게 아니라 우리가 못 읽은 것이다.
            # ★이름은 적힌 그대로 찾는다. 시기 문구를 떼면 이름만 바뀐 경우가
            #   파서 오류로 잘못 분류된다. 2026-08-13 실측: Anthropic 이
            #   'Claude Sonnet 5 through August 31, 2026' 을 'Claude Sonnet 5' 로
            #   바꿨는데, 문구를 떼면 둘이 같은 이름이 되어 페이지에서 늘 찾아진다.
            try:
                suspect, real, errs = split_by_page(date_str, missing)
                page_errs.extend(errs)
                missing = real
            except Exception as e:
                page_errs.append(
                    f"안 온 모델을 페이지로 되짚지 못함({type(e).__name__}: {e})")
            try:
                by_prov = {}
                for prov, model in removed:
                    by_prov.setdefault(prov, []).append(model)
                gone_suspect, _, errs = split_by_page(date_str, by_prov)
                page_errs.extend(errs)
                # ★변동 목록 자체에서 뺀다. 사유 문장만 고치면 data/changes·대시보드·
                #   메일 본문·price_change 표에는 "사라짐"이 그대로 남는다
                #   (2026-08-13 Codex 적대검증 지적. 08-12 에 거짓 removed 28줄이
                #   DB 에 들어간 경로가 이것이다).
                if gone_suspect:
                    changes = [c for c in changes
                               if not (c["kind"] == "removed" and c["model"]
                                       in gone_suspect.get(c["provider"], ()))]
                    summary = diff.summarize(changes)
                removed = removed_models(changes, row_dicts)
                for prov, names in gone_suspect.items():
                    suspect[prov] = sorted(set(suspect.get(prov, [])) | set(names))
            except Exception as e:
                page_errs.append(
                    f"사라진 모델을 페이지로 되짚지 못함({type(e).__name__}: {e})")
        else:
            print("  [시험 실행] 아는 모델 대조·모델 사라짐 확인은 건너뜁니다"
                  f" (시험이 읽는 페이지는 {pages.FIXTURE_DATE} 하루로 고정입니다)")

        reasons, code = [], 0
        # 메일 첫 줄에 쓸 사람용 문장. 종료 코드 숫자는 읽는 사람에게 뜻이 없어
        # 사유를 문장으로 적는다(2026-07-30). 개발자용 reasons 와 따로
        # 두는 이유 = reasons 는 로그·화면용이라 짧고 내부 용어를 쓴다.
        # ★이 목록이 비어 있지 않으면 메일을 보낸다. 종료 코드가 아니라 이것을
        #   기준으로 삼는 이유 = 2배 미만 변동은 종료 코드를 올리지 않는데,
        #   실제 가격 인상($2 → $3)은 대부분 거기 들어온다.
        mail_heads = []
        # 회사 공식 페이지 주소. 아래 경고 문장과 발송부가 같이 쓰므로 여기서 한
        # 번만 모은다. ★못 모아도 메일은 주소 없이 나가야 한다(2026-08-10 검증 지적:
        # 인자를 만들다 난 예외가 무인 실행을 죽였다)
        try:
            mail_urls = pages.provider_urls()
        except Exception as e:
            print(f"[메일] 공식 페이지 주소를 못 모음({type(e).__name__}) - 주소 없이 보냅니다")
            mail_urls = {}
        # 알림(메일 머리줄)은 대표 라인업만 센다(2026-08-14 사용자 결정). 종료 코드와
        # 로그 사유는 전체 줄로 센다 - 대표 밖 줄이라도 2배 변동은 파서 고장 신호일 수 있다
        import mailer
        rep_changes = mailer.representative_changes(changes)
        spikes = [c for c in changes if c.get("spike")]
        rep_spikes = [c for c in rep_changes if c.get("spike")]
        rep_changed = [c for c in rep_changes if c["kind"] == "changed"]
        rep_added_models = sorted({(c["provider"], c["model"]) for c in rep_changes
                                   if c["kind"] == "added"}
                                  - {(c["provider"], c["model"]) for c in rep_changes
                                     if c["kind"] != "added"})
        if spikes:
            reasons.append(f"큰 변동 {len(spikes)}줄(2배 이상)")
            code |= EXIT_SPIKE
        if rep_spikes:
            mail_heads.append(f"가격이 2배 이상 바뀐 항목이 {len(rep_spikes)}줄 있습니다.")
        elif rep_changed:
            mail_heads.append(f"가격이 바뀐 항목이 {len(rep_changed)}줄 있습니다.")
        if rep_added_models:
            mail_heads.append(f"새로 등록된 모델이 {len(rep_added_models)}개 있습니다.")
        elif any(c["kind"] == "added" for c in rep_changes):
            mail_heads.append("기존 모델에 새 단가 줄이 생겼습니다.")
        other_n = len(changes) - len(rep_changes)
        if other_n and not mail_heads:
            # 대표 밖에서만 변동이 있는 날. 메일은 안 보내되 로그에는 남긴다
            print(f"[알림] 대표 라인업 밖 변동만 {other_n}줄 - 메일 없음")
        # 연결 실패와 0개 파싱은 원인도 대응도 달라서 나눠 적는다
        fetch_fail = [s["provider"] for s in status
                      if not s["ok"] and s.get("fail_kind") != "empty"]
        empty_fail = [s["provider"] for s in status
                      if not s["ok"] and s.get("fail_kind") == "empty"]
        if fetch_fail:
            reasons.append(f"수집 실패: {', '.join(fetch_fail)}")
            mail_heads.append(f"{len(fetch_fail)}개 회사에서 가격 페이지를 받지 "
                              f"못했습니다 ({', '.join(fetch_fail)}).")
            code |= EXIT_FETCH
        if empty_fail:
            reasons.append(f"0개 파싱: {', '.join(empty_fail)}")
            mail_heads.append(f"{len(empty_fail)}개 회사는 페이지를 받았지만 가격을 "
                              f"한 건도 읽지 못했습니다 ({', '.join(empty_fail)}).")
            code |= EXIT_EMPTY
        if missing:
            bits = [f"{p} {len(v)}개({', '.join(v[:3])}{' 외' if len(v) > 3 else ''})"
                    for p, v in sorted(missing.items())]
            reasons.append("아는 모델이 안 옴 - " + " / ".join(bits))
            mail_heads.append("어제까지 있던 모델이 오늘 목록에 없습니다 - "
                              + " / ".join(bits) + ".")
            code |= EXIT_MISSING
        if known_err:
            # 대조를 못 한 것도 알린다. 조용히 건너뛰면 새 구멍이 된다
            reasons.append(f"모델 목록 대조 못 함({known_err})")
            mail_heads.append("아는 모델 목록과 대조를 못 했습니다. 빠진 모델이 "
                              "있어도 이번에는 확인되지 않았습니다.")
            code |= EXIT_MISSING
        if suspect:
            # 회사가 내린 게 아니라 우리가 못 읽은 것으로 보이는 모델.
            # 고칠 곳이 페이지가 아니라 우리 코드라서 '사라짐'과 따로 적는다
            bits = [f"{p} {len(v)}개({', '.join(v[:3])}{' 외' if len(v) > 3 else ''})"
                    for p, v in sorted(suspect.items())]
            reasons.append("파서 오류 의심(페이지에는 이름이 남아 있음) - "
                           + " / ".join(bits))
            mail_heads.append("목록에서 빠졌지만 회사 페이지에는 이름이 그대로 있는 "
                              "모델이 있습니다 - " + " / ".join(bits)
                              + ". 수집 코드가 페이지 구조 변경을 못 따라간 것으로 "
                                "보입니다. 확인해 보고 회사가 정말 내린 것이면 "
                                "`python db.py --note 제공사 모델 사유` 로 남겨 "
                                "주세요(이 경우에는 비고가 자동으로 안 적힙니다).")
            code |= EXIT_WARN
        if page_errs:
            reasons.extend(page_errs)
            mail_heads.append("사라진 모델을 그날 페이지로 다시 확인하지 못했습니다 - "
                              + " / ".join(page_errs) + ".")
            code |= EXIT_WARN
        if removed:
            pairs = rename_pairs(changes)
            reasons.append(f"모델 사라짐 {len(removed)}건"
                           + (f" (이름만 바뀐 것으로 보이는 짝 {len(pairs)}쌍)"
                              if pairs else ""))
            mail_heads.append(f"직전 수집에 있던 모델 {len(removed)}개가 사라졌습니다"
                              + (f" (이름만 바뀐 것으로 보이는 짝 {len(pairs)}쌍 포함)"
                                 if pairs else "") + ".")
            code |= EXIT_REMOVED
        elif any(c["kind"] == "removed" for c in rep_changes):
            n_rm = sum(1 for c in rep_changes if c["kind"] == "removed")
            mail_heads.append(f"대표 라인업에서 단가 줄 {n_rm}개가 목록에서 빠졌습니다"
                              "(모델은 남아 있음 - 등급이나 항목이 없어진 것).")
        warned = [s["provider"] for s in status if s.get("warns")]
        if warned:
            reasons.append(f"경고: {', '.join(warned)}")
            # 경고가 난 회사는 사람이 원문을 열어 봐야 하므로 주소를 같이 준다
            bits = [p + (f", {mail_urls[p]}" if mail_urls.get(p) else "")
                    for p in warned]
            mail_heads.append(f"수집 중 경고가 있었습니다 ({' · '.join(bits)}). "
                              "아래 수집 결과의 경고 줄을 확인해 주세요.")
            code |= EXIT_WARN
        if excerpt_problems:
            # 종료 코드는 안 건드린다(2026-08-10 사용자 결정). 원문 대조는 값을
            # 못 믿겠다는 신호가 아니라 "사람이 눈으로 봐 달라"는 신호라서,
            # cron 이 종료 코드로 판단하는 자리와 섞지 않는다
            reasons.extend(excerpt_problems)
            mail_heads.append("바뀐 값을 그날 원문과 대조하는 데 문제가 있었습니다 - "
                              + " / ".join(excerpt_problems) + ".")
        review = {"required": bool(reasons), "reasons": reasons}

        n_models = len({(r.provider, r.model) for r in rows})
        snap_path = storage.save_snapshot(
            date_str, rows,
            meta={"providers_ok": sum(1 for s in status if s["ok"]),
                  "models": n_models, "rows": len(rows),
                  # 어느 날과 비교한 것인지 남긴다. 나중에 변동 기록을 되짚을 때 필요
                  "baselines": prev_by_provider,
                  "missing_models": missing},
            status=status, review=review, dry=args.offline)
        print(f"[저장] {snap_path} · 총 {len(rows)}줄 · 모델 {n_models}개"
              + ("  (시험 실행 - 정본 아님)" if args.offline else ""))
        if prev_date and not args.offline:
            storage.save_changes(date_str, changes)

        # DB 적재. 시험 실행은 넣지 않는다(정본 격리).
        # ★DB가 실패해도 그날 수집을 실패로 보지 않는다(2026-07-27 확정).
        # 값은 이미 JSON에 남아 있고 나중에 `python db.py --date 날짜`로 넣을 수 있다.
        # 수집은 놓치면 소급이 안 되고 DB 넣기는 소급이 되므로, 덜 되돌릴 수 있는
        # 쪽을 먼저 지킨다. 실패는 화면과 종료 코드로 알린다.
        if not args.offline and rows:
            n_reasons_before_db = len(review["reasons"])
            try:
                import db
                conn = db.connect()
                try:
                    n_pt, n_chg, db_warns = db.load(conn, date_str)
                    print(f"[DB] 관측 {n_pt}줄"
                          + (f" · 변동 {n_chg}건" if n_chg else ""))
                    for w in db_warns:
                        print("  [경고]", w)
                    # 공지가 끊긴 날·다시 온 날을 model.note 에 남긴다(2026-08-10).
                    # 적재 뒤에 두는 이유 = 오늘 온 모델의 줄이 이미 들어와 있어야 한다.
                    # ★적재 실패와 따로 잡는 이유 = 적재는 성공했다. 적재 실패로
                    #   보고하면 사람이 `db.py --date` 를 헛되이 다시 돌린다.
                    # ★둘을 각각 잡는 이유 = 끊김 기록이 실패했다고 복귀 기록까지
                    #   건너뛰면 '다시 공지됨' 날짜가 하루 밀린 채 남는다.
                    n_stop, n_back, note_errs = 0, 0, []
                    try:
                        n_stop = db.note_missing(conn, missing, date_str)
                    except Exception as e:
                        note_errs.append(f"공지 끊김 비고 기록 실패({type(e).__name__}: {e})")
                    try:
                        n_back = db.note_back(conn, row_dicts, date_str)
                    except Exception as e:
                        note_errs.append(f"다시 공지 비고 기록 실패({type(e).__name__}: {e})")
                    if n_stop or n_back:
                        print(f"[비고] 공지 끊김 {n_stop}건 · 다시 공지 {n_back}건")
                    for m in note_errs:
                        print("  [비고]", m)
                    if db_warns:
                        review["reasons"].extend(db_warns)
                        review["required"] = True
                        mail_heads.append("데이터베이스에 넣는 중 경고가 있었습니다: "
                                          + " / ".join(db_warns))
                        code |= EXIT_DB
                finally:
                    conn.close()
            except Exception as e:
                print(f"[DB] 넣기 실패: {e}")
                review["reasons"].append(f"DB 넣기 실패: {type(e).__name__}")
                review["required"] = True
                mail_heads.append("값은 파일에 저장됐지만 데이터베이스 넣기가 "
                                  f"실패했습니다 ({type(e).__name__}). "
                                  f"`python db.py --date {date_str}` 로 나중에 넣을 수 있습니다.")
                code |= EXIT_DB
            # DB 단계에서 붙은 사유를 저장본에도 반영한다. 저장본은 위에서 이미
            # 썼으므로, 안 하면 나중에 --rebuild 할 때 DB 실패 표시가 사라진다
            # (2026-07-29 적대 검증에서 나온 것)
            if len(review["reasons"]) > n_reasons_before_db:
                try:
                    storage.update_review(date_str, review)
                except Exception as e:
                    print(f"[저장] 확인 필요 표시 갱신 실패: {type(e).__name__}: {e}")

        # 전부 실패했으면 화면을 다시 그리지 않는다. 저장 쪽은 실패를 실패로 남기지만
        # 화면은 덮어쓰기라, 그냥 두면 '제공사 0 / 모델 0'인 빈 페이지가 남는다
        # (2026-07-27 검증에서 재현). 마지막으로 성공한 화면을 그대로 두는 편이 낫다.
        if not rows:
            print("[대시보드] 건너뜀 - 수집된 줄이 0개라 직전 화면을 그대로 둡니다")
        else:
            collected_at = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S (KST)")
            # 시험 실행은 정본 화면(public/index.html)을 덮지 않는다
            dry_out = (os.path.join(BASE, "data", "dry", "index.html")
                       if args.offline else None)
            if dry_out:
                os.makedirs(os.path.dirname(dry_out), exist_ok=True)
            # 시험 실행은 DB에 안 쓰므로 추이도 안 읽는다(그날 값이 DB에 없어
            # 그래프 마지막 점이 실제와 어긋난 채로 보이는 것을 막는다)
            out_path = dashboard.build(date_str, row_dicts, changes, status,
                                       prev_date, collected_at, out_path=dry_out,
                                       review=review,
                                       history=None if args.offline else trend_history())
            print(f"[대시보드] {out_path}"
                  + ("  (시험 실행 - 정본 아님)" if args.offline else ""))
        notify(date_str, changes, summary, prev_date, review, prev_by_provider)

        # 화면 파일을 gh-pages 갈래에 올린다(publish.py). 파일을 보내는 방식은
        # 받은 사람이 보낸 날 값에 멈추므로 주소로 보게 한다(2026-07-30 결정).
        # ★`.gh-pages` 폴더가 있을 때만 돈다. 한 번 `python publish.py --setup` 을
        #   해 둔 기계에서만 올라가고, 안 한 기계에서는 아무 일도 안 일어난다.
        # ★올리기 실패는 수집 실패가 아니다. 값은 이미 저장본과 DB에 들어가 있고
        #   나중에 `python publish.py 날짜` 로 다시 올릴 수 있다. 종료 코드에 자리를
        #   새로 낼 수 없어(1+2+4+8+16+32+64+128 = 255로 꽉 찼다) 로그에만 남긴다.
        if rows and not args.offline and os.path.isdir(os.path.join(BASE, ".gh-pages")):
            try:
                import publish
                _, note = publish.publish(date_str)
                print(f"[올리기] {note}")
            except Exception as e:
                print(f"[올리기] 실패: {type(e).__name__}: {e}")

        # 알림 메일. mail_heads 가 비어 있으면 알릴 일이 없는 날이므로 안 보낸다
        # (매일 오면 안 보게 된다, 2026-07-30 결정). 시험 실행도 안 보낸다.
        # ★메일 실패는 그날 수집을 실패로 만들지 않고 로그에만 남긴다. 종료 코드에
        #   자리를 새로 낼 수 없고(1+2+4+8+16+32+64+128 = 255로 이미 꽉 찼다),
        #   메일이 안 갔다는 사실을 메일로 알릴 수도 없다.
        if mail_heads and not args.offline:
            # ★주소는 mail_heads 를 만들기 전에 이미 모아 뒀다(경고 문장이 같은
            #   값을 쓴다). mailer.send() 의 try 는 함수 안에 있어 인자를 만들다
            #   나는 예외를 못 잡으므로, 그 자리에서 이미 감싸 두었다(2026-08-10 지적).
            _, note = mailer.send(date_str, mail_heads, changes, status, prev_date,
                                  os.path.join(BASE, "public", "index.html"),
                                  hints=review["reasons"], urls=mail_urls)
            print(f"[메일] {note}")
        elif mail_heads:
            # 시험 실행은 안 보내되, 보냈다면 무엇이 갔을지 보여준다. 문장을 고칠 때
            # 실제 발송 없이 확인할 수 있는 자리가 필요하다
            print("[메일] 시험 실행이라 안 보냄. 갔을 첫 줄:")
            for h in mail_heads:
                print(f"    {h}")

        # 로그 마지막 줄에 사람이 읽을 요약을 남긴다. 종료 코드는 cron 로그에
        # 안 남으므로(출력을 전부 파일로 보내 메일도 안 간다) 이 줄이 실질적으로
        # 더 중요하다. 아침에 `tail -3 data/run.log` 한 번으로 그날을 안다.
        counts = " · ".join(f"{s['provider']} {s['count']}줄"
                            + (f"({s['bytes'] // 1024}KB)" if s.get("bytes") else "")
                            for s in status)
        print(f"[종료] 코드 {code} ({code_text(code)}) · {counts}")
        if reasons:
            print(f"[종료] 사유: {' / '.join(reasons)}")
        return code
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # 마지막 안전망. 잡히지 않은 예외가 cron 실행을 죽이면 그날 메일·화면·
        # 요약이 통째로 빠진다(남은작업_0813 §4). 전문을 로그에 남기고, 실제
        # 수집이면 중단 사실만이라도 메일로 알린다. mailer.send 는 예외를 안 올림
        import traceback
        import mailer
        traceback.print_exc()
        today = datetime.date.today().isoformat()
        print(f"[중단] 잡히지 않은 예외로 수집 중단 · {today} · 전문은 위에")
        if "--offline" not in sys.argv and "--rebuild" not in sys.argv:
            _, note = mailer.send(
                today,
                ["수집이 도중에 중단됐습니다 - 서버에서 data/run.log 를 확인해 주세요"],
                [], [], None, os.path.join(BASE, "public", "index.html"))
            print(f"[중단] 알림 메일: {note}")
        sys.exit(EXIT_CRASH)
