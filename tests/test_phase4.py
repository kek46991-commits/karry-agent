from types import SimpleNamespace
import re

from tools import AGENT_TOOLS, get_current_datetime, web_search_mock
from karry_agent.core import AgentCore


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


def _function_call_response(name, args):
    call = SimpleNamespace(name=name, args=args)
    part = SimpleNamespace(text=None, function_call=call)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def _text_response(text):
    part = SimpleNamespace(text=text, function_call=None)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class FunctionCallingModels:
    def __init__(self):
        self.calls = 0
        self.requests = []

    def generate_content(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.calls == 1:
            return _function_call_response("web_search_mock", {"query": "Gemini API"})
        return _text_response("検索結果を確認しました")



def test_agent_executes_web_search_tool_and_continues():
    models = FunctionCallingModels()
    client = SimpleNamespace(models=models)
    agent = AgentCore(client=client, sleep_fn=lambda _: None)

    result = agent.run("Gemini APIについて検索して")

    assert result.text == "検索結果を確認しました"
    assert result.tool_calls == 1
    assert models.calls == 2
    declarations = models.requests[0]["config"].tools[0].function_declarations
    names = {declaration.name if hasattr(declaration, "name") else declaration["name"] for declaration in declarations}
    assert {"get_current_datetime", "web_search_mock"}.issubset(names)


def test_agent_executes_datetime_tool():
    class Models:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _function_call_response("get_current_datetime", {})
            return _text_response("現在時刻を取得しました")

    agent = AgentCore(client=SimpleNamespace(models=Models()))
    result = agent.run("今何時？")
    assert result.text == "現在時刻を取得しました"
    assert result.tool_calls == 1
