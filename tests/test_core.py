from types import SimpleNamespace

from karry_agent.core import AgentCore
from karry_agent.tools import ToolRegistry


def part_text(text):
    return SimpleNamespace(text=text, function_call=None)


def part_call(name, args):
    return SimpleNamespace(text=None, function_call=SimpleNamespace(name=name, args=args))


def response(parts):
    content = SimpleNamespace(role="model", parts=parts)
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class FakeModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return response([part_call("echo", {"value": "ok"})])
        return response([part_text("完了しました")])


class FakeClient:
    def __init__(self):
        self.models = FakeModels()


def test_react_loop_executes_function_and_returns_text():
    registry = ToolRegistry()
    registry.register("echo", "テスト", {"type": "object", "properties": {"value": {"type": "string"}}}, lambda value: {"echo": value})
    result = AgentCore(client=FakeClient(), registry=registry).run("実行")
    assert result.text == "完了しました"
    assert result.steps == 2
    assert result.tool_calls == 1
