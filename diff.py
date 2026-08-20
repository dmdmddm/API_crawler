"""직전 성공 수집분 대비 변동 감지 (PriceRow 저장본 두 벌 비교).

kind 세 가지: added(새 계열), removed(사라진 계열), changed(값 변경).
한 줄이 한 값이라 항목 여섯 칸을 돌 필요가 없고, 키가 전체 축이라 자료형·변형·
캐시 기간이 다른 줄이 안 뭉개진다(옛 record_key 는 다섯 축뿐이라 gpt-realtime-2.1 의
음성·글자·이미지 세 줄이 한 키로 덮였다 — 2026-08-15 검증 실측).

설계 근거(2026-07-25, 2026-08-15 개정):
- 키에 등급·구간·시기·자료형·변형·캐시 기간·지역·배수·항목·단위를 포함한다.
- 수집에 실패한 제공사는 비교에서 통째로 제외한다. 그러지 않으면 그 제공사의
  줄 전부가 '사라짐'으로 기록되어 시계열이 오염된다.
- 큰 변동(기본 2배 이상)은 따로 표시한다. 파싱이 깨지면 값이 전날에서 크게 벗어나므로,
  이 표시가 조용한 오류를 잡는 장치가 된다. 등록·삭제에는 안 찍는다(새 모델이 나올
  때마다 "큰 변동" 메일이 되므로).
- 2026-08-15 옛 경로(compute_changes · FIELD_LABEL · record_key)는 삭제했다.
"""
from __future__ import annotations

SPIKE_RATIO = 2.0        # 이 배수 이상 변하면 큰 변동으로 표시(2배)

# PriceRow.key() 와 같은 축. 스냅샷(dict)에서 다시 만들 때 쓴다.
# category 는 일부러 뺀다(구역 이동 ≠ 값 변동. gpt-5.5-cyber 실측).
ROW_AXES = ("provider", "model", "tier", "context", "modality", "variant",
            "cache_ttl", "region", "multiplier", "item", "unit",
            "effective_from")
_ROW_DEFAULT = {"tier": "standard", "context": "default", "region": "global"}


def row_key(r):
    """PriceRow dict -> 비교 키."""
    return tuple((r.get(a) or _ROW_DEFAULT.get(a, "")) for a in ROW_AXES)


def _pct(old, new):
    if not old or new is None:
        return None
    return round((new - old) / old * 100, 1)


def _is_spike(old, new):
    """전날 대비 배수가 임계 이상인지 판정."""
    if old is None or new is None:
        return old != new
    if old == 0:
        return new != 0
    ratio = new / old
    return ratio >= SPIKE_RATIO or ratio <= 1 / SPIKE_RATIO


def compute_row_changes(prev_rows, curr_rows, failed_providers=()):
    """PriceRow 스냅샷 두 벌의 변동 목록. 항목 형식은 kind 세 가지 공통:

    {kind, 축 12개(ROW_AXES), category, old_value, new_value, pct, spike}
    added 는 old_value=None, removed 는 new_value=None.
    category 는 알림 필터(대표 라인업)용으로 같이 싣는다 — 키에는 안 들어간다.
    """
    skip = set(failed_providers)
    prev = {row_key(r): r for r in prev_rows if r["provider"] not in skip}
    curr = {row_key(r): r for r in curr_rows if r["provider"] not in skip}
    changes = []

    def entry(kind, key, old, new, src):
        c = dict(zip(ROW_AXES, key))
        c.update({"kind": kind, "category": src.get("category") or "",
                  "old_value": old, "new_value": new,
                  "pct": _pct(old, new),
                  "spike": _is_spike(old, new) if kind == "changed" else False})
        return c

    for k, r in curr.items():
        if k not in prev:
            changes.append(entry("added", k, None, r["value"], r))
        elif prev[k]["value"] != r["value"]:
            changes.append(entry("changed", k, prev[k]["value"], r["value"], r))
    for k, p in prev.items():
        if k not in curr:
            changes.append(entry("removed", k, p["value"], None, p))
    changes.sort(key=lambda c: (c["provider"], c["model"], c["item"]))
    return changes


def summarize(changes):
    return {
        "added": sum(1 for c in changes if c["kind"] == "added"),
        "removed": sum(1 for c in changes if c["kind"] == "removed"),
        "changed": sum(1 for c in changes if c["kind"] == "changed"),
        "spikes": sum(1 for c in changes if c.get("spike")),
        "total": len(changes),
    }
