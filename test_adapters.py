"""오프라인 시험 하네스: 보관해 둔 공식 페이지로 회사별 수집 코드 6종 파싱 검증.

읽는 곳 = data/pages/2026-08-15/회사.md.gz(DeepSeek 만 html.gz) — 새 경로 고정본.
2026-08-10 이전에는 data/fixtures/ 에 사본을 따로 뒀는데 갱신이 따로여서 낡아 갔다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_parse_prices():
    """값·단위·자료형을 함께 읽는 새 파서(pricetext.parse_prices) 테스트 코드.

    저장 범위를 "과금되는 항목 전부"로 넓히면서(2026-07-27) 단위가 값에 붙어
    다녀야 한다. 아래 형태는 전부 저장본 6개 페이지에서 실제로 나온 것이다.
    """
    from adapters.pricetext import parse_prices
    cases = [
        # 토큰 단가 - 기존과 같은 값이 나와야 한다
        ("$10 / MTok",                        [(10, "per_1M_tokens", None)]),
        ("$1.25 / 1M tokens",                 [(1.25, "per_1M_tokens", None)]),
        ("<= 200K tokens: $1.25",             [(1.25, "per_1M_tokens", None)]),
        ("$0.15",                             [(0.15, "per_1M_tokens", None)]),
        ("$5,000",                            [(5000, "per_1M_tokens", None)]),
        # 토큰당이 아닌 단위 - 옛 파서는 버리던 것
        ("$0.039 per image",                  [(0.039, "per_image", None)]),
        ("$100.00 / hour",                    [(100, "per_hour", None)]),
        ("$5.00 / 1,000 requests",            [(5, "per_1K_request", None)]),
        ("$0.03 per session",                 [(0.03, "per_session", None)]),
        ("$0.00025 per invocation",           [(0.00025, "per_call", None)]),
        ("$0.01 per CCU",                     [(0.01, "per_CCU", None)]),
        ("$0.08 per session-hour",            [(0.08, "per_session_per_hour", None)]),
        ("$0.022 per 0.5K image *",           [(0.022, "per_500_image", None)]),
        # 복합 단위
        ("$1.00 / 1,000,000 tokens per hour", [(1, "per_1M_tokens_per_hour", None)]),
        ("$0.10 / GB per day",                [(0.1, "per_GB_per_day", None)]),
        # 한 칸에 값이 여럿
        ("$2.00 (text) $12.00 (audio)",       [(2, "per_1M_tokens", "text"),
                                               (12, "per_1M_tokens", "audio")]),
        ("$0.45 ($0.00012 per image)",        [(0.45, "per_1M_tokens", None),
                                               (0.00012, "per_image", None)]),
        ("$0.05 / min ( $3.00 / hr)",         [(0.05, "per_minute", None),
                                               (3, "per_hour", None)]),
        ("$0.01 / sec $0.002 / img",          [(0.01, "per_second", None),
                                               (0.002, "per_image", None)]),
        ("$6 / $10 / $14",                    [(6, "per_1M_tokens", None),
                                               (10, "per_1M_tokens", None),
                                               (14, "per_1M_tokens", None)]),
        # 자료형 목록은 펼친다 (2026-07-27 결정)
        ("$0.05 (text/image/video) $0.10 (audio)",
                                              [(0.05, "per_1M_tokens", "text"),
                                               (0.05, "per_1M_tokens", "image"),
                                               (0.05, "per_1M_tokens", "video"),
                                               (0.1, "per_1M_tokens", "audio")]),
        ("$3.00 (text and thinking)",         [(3, "per_1M_tokens", "text"),
                                               (3, "per_1M_tokens", "thinking")]),
        ("$0.30 (images)",                    [(0.3, "per_1M_tokens", "image")]),
        ("$9.00 (text) $17.50 (video) *",     [(9, "per_1M_tokens", "text"),
                                               (17.5, "per_1M_tokens", "video")]),
        # 설명 문장이 이어져도 단위를 끌어오지 않는다
        ("$10.00 / 1k calls + Search content tokens billed at model rates.",
                                              [(10, "per_1K_call", None)]),
        ("Charged for embeddings at $0.15 / 1M tokens. Retrieved document tokens",
                                              [(0.15, "per_1M_tokens", None)]),
        ("$0.10 / GB-day after 1 GB free per account per month",
                                              [(0.1, "per_GB_per_day", None)]),
        # 값이 아닌 것
        ("$1.25 (20% off)",                   [(1.25, "per_1M_tokens", None)]),
        ("200K tokens",                       []),
        ("Free of charge",                    []),
        ("-",                                 []),
        (None,                                []),
    ]
    fails = []
    for text, want in cases:
        got = [(v.value, v.unit, v.modality) for v in parse_prices(text)]
        if got != want:
            fails.append((text, got, want))
    for t, got, exp in fails:
        print(f"   [FAIL] {t!r}\n          -> {got}\n          기대 {exp}")
    assert not fails, f"parse_prices 어긋남 {len(fails)}건"
    print(f"===== parse_prices 테스트 코드 {len(cases)}건 전부 통과")


def test_rows_all():
    """6개사 파싱 - 2026-08-15 마크다운·HTML 고정본.

    (Google 옛 HTML 경로와의 값 대조는 2026-08-16 옛 경로 삭제와 함께 종료 -
    전환 검증은 2026-08-15 에 106칸 일치로 끝났다. 지금 교차검증 = 하한·계열 키
    중복·spot 값 6개 + Perplexity 계산기 JSON 대조.)
    """
    import gzip
    from adapters.openai import OpenAIAdapter
    from adapters.google import GeminiAdapter
    from adapters.anthropic import AnthropicAdapter
    from adapters.xai import GrokAdapter
    from adapters.perplexity import PerplexityAdapter
    from adapters.deepseek import DeepSeekAdapter

    DATE = "2026-08-15"

    def load(name, kind="md"):
        return gzip.open(f"data/pages/{DATE}/{name}.{kind}.gz", "rt",
                         encoding="utf-8").read()

    plans = [(OpenAIAdapter, "openai", "md", 600),
             (GeminiAdapter, "google", "md", 400),
             (AnthropicAdapter, "anthropic", "md", 100),
             (GrokAdapter, "xai", "md", 90),
             (PerplexityAdapter, "perplexity", "md", 35),
             (DeepSeekAdapter, "deepseek", "html", 18)]
    spot = {("OpenAI", "gpt-5.6-sol", "standard", "input", "short"): 5.0,
            ("Google", "Gemini 3.6 Flash", "standard", "input", "default"): 0.75,
            ("Anthropic", "Claude Opus 5", "standard", "input", "default"): 5.0,
            ("xAI", "grok-4.6", "standard", "input", "short"): 2.0,
            ("Perplexity", "Sonar", "standard", "output", "default"): 1.0,
            ("DeepSeek", "deepseek-v4-flash", "off_peak", "input", "default"): 0.22}
    all_rows = {}
    for cls, name, kind, floor in plans:
        rows, warns = cls().run_rows(text=load(name, kind))
        all_rows[cls.provider] = rows
        assert len(rows) >= floor, f"{cls.provider}: {len(rows)}줄 < 하한 {floor}"
        keys = [r.key() for r in rows]
        assert len(keys) == len(set(keys)), f"{cls.provider}: 계열 키 중복"
        for r in rows:
            assert r.value > 0 and r.item, f"{cls.provider}: 빈 값/항목"
            assert r.unit or r.multiplier, f"{cls.provider}: 단위 없는 금액 줄"
        bad = [w for w in warns if "같은 축" not in w]
        assert not bad, f"{cls.provider}: 경고 {bad[:2]}"
    for (prov, model, tier, item, ctx), want in spot.items():
        got = [r.value for r in all_rows[prov]
               if r.model == model and r.tier == tier and r.item == item
               and r.context == ctx and not r.multiplier]
        assert got == [want], f"{prov} {model} {item}: {got} != [{want}]"
    mult = [r for r in all_rows["xAI"] if r.multiplier]
    assert len(mult) == 33 and {r.value for r in mult} == {0.8, 2.0}, "xAI 배수 줄"

    total = sum(len(v) for v in all_rows.values())
    print(f"===== 새 경로 6개사: 합계 {total}줄")


test_parse_prices()
test_rows_all()
