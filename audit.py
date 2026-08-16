"""온디맨드 적대적(adversarial) 감사(audit): 공식 페이지를 다시 읽어 우리 값을 반박 시도.

원칙: LLM이 '기억'으로 답하지 않는다. 공식 페이지 원문 텍스트를 근거로 줘서
      그 안에서만 단가를 '독립적으로' 재추출하게 하고(앵커링 방지), 우리 값과 대조한다.
      우리 값과 다르면 'differ(반박됨)'로 표시 -> 사람이 검토.
백엔드: Codex CLI(codex exec). 없거나 실패하면 해당 건은 'skipped'.
      (독립 Claude 에이전트로 교체 가능: codex_extract만 갈아끼우면 됨)

사용:
  python audit.py --offline                 # 보관해 둔 공식 페이지로 감사
  python audit.py --offline --provider DeepSeek
  python audit.py --limit 5                 # 상위 5건만(라이브)
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bs4 import BeautifulSoup
from adapters.registry import ALL_ADAPTERS
import storage
import pages

BASE = os.path.dirname(os.path.abspath(__file__))
AUDIT_DIR = os.path.join(BASE, "data", "audits")
ADAPTER = {a.provider: a for a in ALL_ADAPTERS}
TOL = 0.03


def _get_html(provider, offline):
    if offline:
        return pages.fixture_html(provider)
    return ADAPTER[provider].fetch()


def page_text(provider, offline):
    """공식 페이지를 헤딩 + 표(행 단위)로 정돈. 모델명 앵커와 가격행이 가깝게 모이도록.
    (표를 통째로 펼치면 모델명↔가격이 멀어져 스니펫에서 잘리는 문제 방지)"""
    soup = BeautifulSoup(_get_html(provider, offline), "lxml")
    for tag in soup(["style", "nav", "header", "footer", "svg"]):
        tag.decompose()
    lines = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "table"]):
        if el.name == "table":
            for r in el.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in r.find_all(["th", "td"])]
                if cells:
                    lines.append(" | ".join(cells))
        else:
            t = el.get_text(" ", strip=True)
            if t:
                lines.append("### " + t)   # 헤딩(모델명 앵커, 예: Gemini)
    return "\n".join(lines)


def snippet_for(text, model, ctx=20):
    """모델명이 있는 줄 + 이어지는 몇 줄(그 모델의 가격표)만 근거로 잘라낸다."""
    lines = text.split("\n")
    ml = model.lower()
    for i, ln in enumerate(lines):
        if ml in ln.lower():
            return "\n".join(lines[max(0, i - 2): i + ctx])
    key = [k for k in re.split(r"[^a-z0-9]+", ml) if k][:3]
    for i, ln in enumerate(lines):
        low = ln.lower()
        if key and all(k in low for k in key):
            return "\n".join(lines[max(0, i - 2): i + ctx])
    return None


def codex_extract(provider, model, snippet):
    """공식 페이지 스니펫을 근거로 Codex가 단가를 '독립' 재추출. 우리 값은 주지 않는다(앵커링 방지)."""
    prompt = (
        "You are an adversarial price auditor. Below is text from {p}'s OFFICIAL pricing page. "
        "Using ONLY this text (never prior knowledge), independently determine the standard API "
        "token price for the model '{m}'. Use the standard/cache-miss INPUT price. "
        "Give USD per 1,000,000 tokens. If the model or a clear number is not in the text, use null "
        "(do not guess). "
        'Reply with ONLY compact JSON: {{"input": <num|null>, "output": <num|null>, "note": "<=12 words"}}.'
        "\n\n--- PAGE TEXT ---\n{s}\n--- END ---"
    ).format(p=provider, m=model, s=snippet[:4000])
    try:
        proc = subprocess.run(
            ["codex", "exec", "--sandbox", "danger-full-access", "--skip-git-repo-check", prompt],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    except Exception as e:   # noqa: BLE001
        return {"error": f"codex 실행 실패: {e}"}
    for blob in reversed(re.findall(r"\{[^{}]*\}", proc.stdout)):
        try:
            d = json.loads(blob)
            if "input" in d or "output" in d:
                return d
        except json.JSONDecodeError:
            continue
    return {"error": "codex 응답 JSON 파싱 실패", "raw_tail": proc.stdout[-160:]}


def _verdict(ours, agent):
    """우리 값 vs 에이전트 독립 재추출 값. confirm / differ / uncertain / skipped."""
    if agent.get("error"):
        return "skipped"
    ai, ao = agent.get("input"), agent.get("output")
    if ai is None and ao is None:
        return "uncertain"

    def close(a, b):
        if a is None or b is None:
            return None
        if a == 0 or b == 0:
            return abs(a - b) < 1e-9
        return abs(a - b) / max(abs(a), abs(b)) <= TOL
    checks = [c for c in (close(ours["input"], ai), close(ours["output"], ao)) if c is not None]
    if not checks:
        return "uncertain"
    return "confirm" if all(checks) else "differ"


def standard_io(rows):
    """2026-08-15 새 저장본(값마다 한 줄) -> 모델당 표준 등급 입력·출력 한 쌍.
    감사는 모델의 대표값(표준 등급 · 100만 토큰당 · 문맥 구간 없음/짧은 쪽 · 자료형 없음)만 본다.
    반환: [{provider, model, input_usd_per_mtok, output_usd_per_mtok}] (둘 다 없는 모델은 뺀다)"""
    by = {}
    for r in rows:
        if (r.get("tier") or "standard") != "standard" or r.get("unit") != "per_1M_tokens":
            continue
        if r.get("modality") or r.get("variant") or r.get("cache_ttl") or r.get("multiplier"):
            continue
        if (r.get("context") or "default") not in ("default", "short"):
            continue
        if r.get("item") not in ("input", "output"):
            continue
        d = by.setdefault((r["provider"], r["model"]),
                          {"provider": r["provider"], "model": r["model"],
                           "input_usd_per_mtok": None, "output_usd_per_mtok": None})
        key = "input_usd_per_mtok" if r["item"] == "input" else "output_usd_per_mtok"
        if d[key] is None:
            d[key] = r["value"]
    return [d for d in by.values()
            if d["input_usd_per_mtok"] is not None or d["output_usd_per_mtok"] is not None]


def run_audit(records, offline, limit=None, provider=None):
    targets = [r for r in records if not provider or r["provider"] == provider]
    if limit:
        targets = targets[:limit]
    cache, report = {}, []
    for r in targets:
        prov, model = r["provider"], r["model"]
        if prov not in cache:
            cache[prov] = page_text(prov, offline)
        snip = snippet_for(cache[prov], model)
        ours = {"input": r["input_usd_per_mtok"], "output": r["output_usd_per_mtok"]}
        if not snip:
            agent, verdict = {"error": "공식 페이지에서 모델명 미발견"}, "skipped"
        else:
            print(f"  Codex 감사: {prov} / {model} ...", flush=True)
            agent = codex_extract(prov, model, snip)
            verdict = _verdict(ours, agent)
        report.append({"provider": prov, "model": model,
                       "ours": ours, "agent": agent, "verdict": verdict})
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--provider", default=None, help="특정 제공사만 (예: DeepSeek)")
    args = ap.parse_args()

    dates = storage.list_dates()
    date = args.date or (dates[-1] if dates else None)
    if not date:
        print("스냅샷 없음. run.py 먼저 실행."); return
    recs = standard_io(storage.load_snapshot(date)["rows"])

    print(f"[적대적 감사] {date} · 공식 페이지 재열람으로 우리 값 반박 시도 (Codex, 기억 금지)")
    report = run_audit(recs, args.offline, args.limit, args.provider)

    os.makedirs(AUDIT_DIR, exist_ok=True)
    path = os.path.join(AUDIT_DIR, f"{date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    tag = {"confirm": "우리값 확인", "differ": "반박됨(검토!)", "uncertain": "불명확", "skipped": "건너뜀"}
    n = {k: sum(1 for x in report if x["verdict"] == k) for k in tag}
    print(f"\n[감사 결과] {len(report)}건 · 확인 {n['confirm']} / 반박 {n['differ']} / "
          f"불명확 {n['uncertain']} / 건너뜀 {n['skipped']} (저장: {path})")
    for x in report:
        a = x["agent"]
        av = (a.get("error") if a.get("error")
              else f"입력 ${a.get('input')}/출력 ${a.get('output')}"
                   + (f" · {a['note']}" if a.get("note") else ""))
        print(f"  [{tag[x['verdict']]}] {x['provider']} {x['model']} · "
              f"우리 ${x['ours']['input']}/${x['ours']['output']} · 공식재추출 {av}")


if __name__ == "__main__":
    main()
