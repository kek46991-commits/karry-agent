from types import SimpleNamespace

from tools import AGENT_TOOLS, generate_image_mock
from karry_agent.core import AgentCore
from karry_agent.tools import build_default_registry


def test_legacy_image_mock_does_not_point_to_missing_file():
    result = generate_image_mock("青い空を飛ぶ白い鳥")
    assert result.startswith("[Image Generated]:")
    assert "青い空を飛ぶ白い鳥" in result
    assert "mock_image_path.png" not in result and ".png" not in result


def test_legacy_mock_stays_available_for_old_tool_list():
    assert generate_image_mock in AGENT_TOOLS


def test_real_image_generation_is_registered_instead_of_mock():
    registry = build_default_registry()
    assert "generate_image_mock" not in registry.tools
    declaration = registry.tools["generate_image"].declaration
    assert declaration["name"] == "generate_image"
    assert "prompt" in declaration["parameters"]["required"]


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
            return _function_call_response("generate_image", {"prompt": "夕焼けの富士山"})
        return _text_response("画像生成を試しました")


def test_image_tool_outside_chat_task_reports_error_without_fake_path():
    models = ImageFunctionCallingModels()
    agent = AgentCore(client=SimpleNamespace(models=models), sleep_fn=lambda _: None)

    result = agent.run("夕焼けの富士山の画像を作って")

    assert result.text == "画像生成を試しました"
    assert result.tool_calls == 1
    tool_response = models.requests[1]["contents"][-2].parts[0].function_response
    assert tool_response.name == "generate_image"
    assert "Web チャット" in str(tool_response.response)
    assert "mock_image_path" not in str(tool_response.response)
