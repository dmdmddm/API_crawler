"""회사별 수집 코드 전체 등록.

새 제공사를 추가하려면: 회사별 수집 코드 파일 작성 -> 여기 ALL_ADAPTERS에 등록.

오프라인 시험이 읽을 페이지는 여기서 다루지 않는다(2026-08-10). 매일 받아 두는
data/pages/날짜/회사.html.gz 를 그대로 쓰고, 날짜는 pages.FIXTURE_DATE 한 줄이다.
"""
from .anthropic import AnthropicAdapter
from .openai import OpenAIAdapter
from .google import GeminiAdapter
from .xai import GrokAdapter
from .perplexity import PerplexityAdapter
from .deepseek import DeepSeekAdapter

ALL_ADAPTERS = [
    AnthropicAdapter(),
    OpenAIAdapter(),
    GeminiAdapter(),
    GrokAdapter(),
    PerplexityAdapter(),
    DeepSeekAdapter(),
]
