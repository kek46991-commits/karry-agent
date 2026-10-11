import re

from tools import AGENT_TOOLS, get_current_datetime, web_search_mock


def test_current_datetime_format():
    value = get_current_datetime()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value)


def test_web_search_mock_contains_query():
    result = web_search_mock("test query")
    assert "test query" in result
    assert "Simulated search response" in result


def test_agent_tools_registry_order():
    assert AGENT_TOOLS == [get_current_datetime, web_search_mock]
    assert all(callable(tool) for tool in AGENT_TOOLS)
