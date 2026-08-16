"""날짜별 스냅샷 저장/로드 (JSON 기반 저장소).

하루 1회 수집 결과를 data/snapshots/YYYY-MM-DD.json 으로 남긴다.
변동 감지는 이 스냅샷들을 비교해서 수행한다.

설계 근거(2026-07-25, 일부 2026-07-27 개정):
- 실패한 제공사는 직전 값으로 채우지 않는다. 실패를 성공으로 덮으면
  '그날 가격이 안 바뀌었다'는 거짓 기록이 되어 시계열이 오염된다.
- 변동 비교 기준은 '직전 날짜'가 아니라 '읽을 수 있는 직전 수집분'이다.
- 저장은 임시 파일에 쓴 뒤 이름을 바꾼다. 잘린 파일이 정본 자리에 남지 않게.
- ~~관측과 승인을 나눈다~~ (2026-07-27 폐기) 큰 변동·실패가 있어도 다음 수집의
  비교 기준으로 그대로 쓴다. 매일 사람이 확인하지 않을 것이므로, 승인 여부로
  기준을 미루면 기준이 계속 과거에 머문다. 큰 변동은 알림·화면 표시·종료 코드로 남긴다.
"""
from __future__ import annotations
import os
import json
import glob
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(BASE, "data", "snapshots")
CHANGE_DIR = os.path.join(BASE, "data", "changes")


def _ensure():
    os.makedirs(SNAP_DIR, exist_ok=True)
    os.makedirs(CHANGE_DIR, exist_ok=True)


def write_json(path, payload):
    """임시 파일에 다 쓴 뒤 이름을 바꿔 옮긴다.

    파일에 직접 쓰면 디스크가 차거나 쓰는 도중 전원이 끊겼을 때 잘린 파일이
    정본 자리에 남는다. 그 파일은 다음 실행이 읽다가 죽고, 사람이 치우기 전까지
    매일 같은 자리에서 죽는다(2026-07-27 검증에서 재현).
    이름 바꾸기는 같은 파일 시스템 안에서 원자적이라 중간 상태가 남지 않는다.
    """
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


FORMAT = "rows/1"     # 2026-08-15 저장본 형식 표지. 옛 형식(records)은 표지가 없다


def save_snapshot(date_str, rows, meta=None, status=None, review=None, dry=False):
    """스냅샷 저장. rows = PriceRow 목록(또는 그 dict). 한 줄이 한 값.

    2026-08-15 형식 전환: 'records'(모델당 여섯 칸) -> 'rows'(값마다 한 줄, 축 12개).
    옛 형식 파일은 data/archive_옛경로_20260727-20260815/ 로 옮겨 보존한다(읽는 코드 없음).

    status: 제공사별 수집 성공/실패 기록. 실패한 제공사를 나중에 비교에서 제외하는 근거.
    review: {'required': bool, 'reasons': [...]} 사람이 볼 필요가 있는지와 그 사유.
    dry: 시험 실행(저장본으로 수집). 정본과 다른 디렉토리에 쓴다.

    ★dry 격리 이유(2026-07-27 실제 사고): `run.py --offline --date 2026-07-27`을
    시험으로 돌렸다가 그날의 라이브 수집분(58개 모델)이 저장본 값(55개)으로
    덮였다. 라이브 재수집으로 복구했지만, 수집이 안 되는 시점이었다면 그날
    기록이 사라졌을 것이다. 시험은 정본에 쓰지 않는다.
    """
    _ensure()
    payload = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),   # 기록은 UTC
        "meta": meta or {},
        "status": status or [],
        "review": review or {"required": False, "reasons": []},
        "format": FORMAT,
        "rows": [r.to_dict() if hasattr(r, "to_dict") else r for r in rows],
        **({"dry_run": True} if dry else {}),
    }
    d = os.path.join(SNAP_DIR, "dry") if dry else SNAP_DIR
    os.makedirs(d, exist_ok=True)
    return write_json(os.path.join(d, f"{date_str}.json"), payload)


def list_dates():
    _ensure()
    files = glob.glob(os.path.join(SNAP_DIR, "*.json"))
    return sorted(os.path.splitext(os.path.basename(f))[0] for f in files)


def load_snapshot(date_str):
    path = os.path.join(SNAP_DIR, f"{date_str}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def update_review(date_str, review):
    """저장본의 확인 필요 표시만 바꿔 다시 쓴다. 값(records)은 건드리지 않는다.

    DB 적재는 저장본을 쓴 뒤에 돈다. 그래서 DB가 실패해도 그 사유가 저장본에
    안 남고, 나중에 `run.py --rebuild 날짜`로 화면 파일을 새로 만들면 DB 실패
    표시가 사라진다(2026-07-29 적대 검증에서 나온 것).
    저장본이 없으면 아무것도 안 한다.
    """
    path = os.path.join(SNAP_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    payload["review"] = review
    return write_json(path, payload)


def failed_providers(snapshot):
    """그 스냅샷에서 수집에 실패한 제공사 이름들."""
    return [s.get("provider") for s in snapshot.get("status", []) if not s.get("ok")]


def previous_date(current):
    """current 직전 날짜의 스냅샷 날짜. 없으면 None."""
    ds = [d for d in list_dates() if d < current]
    return ds[-1] if ds else None


def previous_readable(current, provider=None):
    """비교 기준이 될 직전 스냅샷의 날짜와 내용. 없으면 (None, None).

    파일이 깨져 있으면 건너뛰고 그 앞 날짜를 본다. 파일에 직접 쓰던 시절에
    잘린 파일이 남으면 다음 실행이 그걸 읽다 죽고, 사람이 치우기 전까지
    매일 같은 자리에서 죽었다(2026-07-27 검증에서 재현). 지금은 저장을
    임시 파일 + 이름 바꾸기로 하지만, 옛 파일이나 사람이 손댄 파일이 있을 수
    있으므로 읽는 쪽에서도 막는다.

    provider를 주면 그 제공사가 성공한 가장 최근 스냅샷을 찾는다.
    (2026-07-27: 사람 승인 단계를 뺐다. 매일 확인하지 않을 것이므로 승인 여부로
     비교 기준을 미루면 기준이 계속 과거에 머문다. 큰 변동은 알림과 화면 표시로만 남긴다.)
    """
    for d in reversed([d for d in list_dates() if d < current]):
        try:
            snap = load_snapshot(d)
        except Exception as e:
            print(f"  [건너뜀] {d} 스냅샷을 읽을 수 없음({e.__class__.__name__}) → 그 앞 날짜로")
            continue
        # 옛 형식(records) 저장본은 기준선으로 안 쓴다. 축이 달라 비교하면 전부
        # 삭제+추가로 나온다(전환 첫날 기준선 없음 - 수집기준_결정항목.md 검증 반영 보강 절)
        if "rows" not in snap:
            continue
        if provider is not None:
            if provider in failed_providers(snap):
                continue
            # 실패 기록이 없다고 성공한 것은 아니다. 그 회사가 등록되기 전의 스냅샷은
            # status에 아예 안 들어 있어 '실패 아님'으로 통과한다. 줄이 실제로
            # 있는 날만 기준선으로 삼는다 (2026-07-28 Codex 적대검증 지적).
            if not any(r.get("provider") == provider for r in snap.get("rows", [])):
                continue
        return d, snap
    return None, None


def save_changes(date_str, changes):
    _ensure()
    return write_json(os.path.join(CHANGE_DIR, f"{date_str}.json"), changes)


def load_changes(date_str):
    """그날 저장해 둔 변동 목록. 비교 대상이 없던 날이나 파일이 깨진 경우는 빈 목록."""
    path = os.path.join(CHANGE_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [건너뜀] {date_str} 변동 기록을 읽을 수 없음({e.__class__.__name__})")
        return []
