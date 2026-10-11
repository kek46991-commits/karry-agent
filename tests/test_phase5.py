from types import SimpleNamespace

from tools import AGENT_TOOLS, generate_image_mock
from karry_agent.core import AgentCore
from karry_agent.tools import build_default_registry


def test_generate_image_mock_returns_expected_result():
    result = generate_image_mock("青い空を飛ぶ白い鳥")
    assert result.startswith("[Image Generated]:")
    assert "青い空を飛ぶ白い鳥" in result
    assert "mock_image_path.png" in result


def test_generate_image_mock_is_registered_for_function_calling():
    assert generate_image_mock in AGENT_TOOLS
    registry = build_default_registry()
    assert "generate_image_mock" in registry.tools
    declaration = registry.tools["generate_image_mock"].declaration
    assert declaration["name"] == "generate_image_mock"
    assert "prompt" in declaration["parameters"]["required"]


def test_generate_image_mock_handles_empty_prompt():
    result = generate_image_mock("")
    assert "[Image Generated]:" in result
    assert "mock_image_path.png" in result


def _function_call_response(name, args):
    call = SimpleNamespace(name=name, args=args)
    part = SimpleNamespace(text=None, function_call=call)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def _text_response(text):
    part = SimpleNamespace(text=text, function_call=None)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class ImageFunctionCallingModels:
    def __init__(self):
        self.calls = 0
        self.requests = []

    def generate_content(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.calls == 1:
            return _function_call_response("generate_image_mock", {"prompt": "夕焼けの富士山"})
        return _text_response("画像生成モックを実行しました")


def test_agent_executes_image_generation_tool_and_continues():
    models = ImageFunctionCallingModels()
    agent = AgentCore(client=SimpleNamespace(models=models), sleep_fn=lambda _: None)

    result = agent.run("夕焼けの富士山の画像を作って")

    assert result.text == "画像生成モックを実行しました"
    assert result.tool_calls == 1
    assert models.calls == 2
    tool_response = models.requests[1]["contents"][-2].parts[0].function_response
    assert tool_response.name == "generate_image_mock"
    assert "mock_image_path.png" in str(tool_response.response)
