import pytest
from app.core.config import settings
from app.services.web_search_service import WebSearchService, web_search_service
from app.services.tool_registry import tool_registry


def test_detect_search_intent():
    service = WebSearchService()
    
    # 1. Explicit prefixes
    assert service.detect_search_intent("/search latest AI news") == "latest AI news"
    assert service.detect_search_intent("!search python release") == "python release"
    assert service.detect_search_intent("/web local llm benchmarks") == "local llm benchmarks"
    
    # 2. Natural language triggers
    assert service.detect_search_intent("what are the latest advances in quantum computing") is not None
    assert service.detect_search_intent("latest news on spacex") is not None
    assert service.detect_search_intent("current weather in Tokyo") is not None
    
    # 3. Non-search queries
    assert service.detect_search_intent("Write a python script that prints hello world") is None
    assert service.detect_search_intent("Explain quicksort in simple terms") is None


def test_format_search_context():
    service = WebSearchService()
    
    empty_ctx = service.format_search_context("test query", [])
    assert "No live search results" in empty_ctx
    
    sample_results = [
        {
            "title": "Python 3.14 Release Notes",
            "url": "https://docs.python.org/3.14/",
            "snippet": "New features in Python 3.14 including JIT and subinterpreters.",
            "source": "docs.python.org"
        },
        {
            "title": "Python (programming language)",
            "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
            "snippet": "Python is a high-level general-purpose programming language.",
            "source": "wikipedia.org"
        }
    ]
    
    ctx = service.format_search_context("Python 3.14 features", sample_results)
    assert "[1] **Python 3.14 Release Notes**" in ctx
    assert "https://docs.python.org/3.14/" in ctx
    assert "[2] **Python (programming language)**" in ctx
    assert "[1], [2]" in ctx


@pytest.mark.asyncio
async def test_tool_registry_web_search_registered():
    defs = {t.name: t for t in tool_registry.get_all_definitions()}
    assert "web_search" in defs
    assert "fetch_web_page" in defs
    
    ws_def = defs["web_search"]
    assert ws_def.is_external is True
    assert ws_def.category == "web"
    assert "query" in ws_def.parameters["properties"]


@pytest.mark.asyncio
async def test_tool_registry_web_search_execution():
    res = await tool_registry.execute_tool("web_search", {"query": "python programming", "max_results": 2})
    assert res.status == "success"
    assert res.result is not None
    assert "results" in res.result
    assert "formatted_context" in res.result


@pytest.mark.asyncio
async def test_tool_registry_privacy_blocked():
    orig = settings.enable_web_search
    try:
        settings.enable_web_search = False
        res = await tool_registry.execute_tool("web_search", {"query": "test query"})
        assert res.status == "blocked"
        assert "disabled in settings for privacy" in (res.error or "")
    finally:
        settings.enable_web_search = orig
