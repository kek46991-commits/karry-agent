from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from karry_agent.core import AgentCore, _is_website_creation_request
from karry_agent.tools import ToolRegistry


HTML = "<!doctype html><html lang='ja'><head><title>Demo</title></head><body><h1>Preview</h1></body></html>"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("KARRY_API_TOKEN", "PARTNER_NAME", "CORE_USER_NAME", "CORE_PRIVATE_CONTEXT"):
        monkeypatch.delenv(name, raising=False)


def text_part(text):
    return SimpleNamespace(text=text, thought=False, function_call=None)


def function_part(name="deploy_instant_website"):
    return SimpleNamespace(text=None, thought=False, function_call=SimpleNamespace(name=name, args={}))


def model_response(*parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=list(parts)))])


def fake_client(*responses):
    generator = Mock(side_effect=responses)
    return SimpleNamespace(models=SimpleNamespace(generate_content=generator)), generator


def registry_with_prepare(result=None):
    registry = ToolRegistry()
    prepare = Mock(return_value=result if result is not None else {
        "status": "needs_user_action", "approval_id": "private-approval",
        "preview": {"public_url": "https://not-published.example/preview.html"},
    })
    registry.register("deploy_instant_website", "prepare only", {
        "type": "object", "properties": {"html_content": {"type": "string"}}, "required": ["html_content"],
    }, prepare)
    return registry, prepare


def test_explicit_creation_generates_once_and_prepares_once_without_announcing_url():
    registry, prepare = registry_with_prepare()
    client, generator = fake_client(model_response(text_part("```html\n" + HTML + "\n```")))

    result = AgentCore(client=client, registry=registry).run("ホームページを作成して")

    assert result.status == "needs_user_action"
    assert result.text == "HTML作成済み。チャット内プレビューで確認できます。公開は確認後です。"
    assert result.steps == result.tool_calls == result.metrics.api_calls == result.metrics.tool_calls == 1
    assert generator.call_count == prepare.call_count == 1
    assert prepare.call_args.kwargs == {"html_content": HTML}
    assert result.trace[0].action == "deploy_instant_website"
    assert result.trace[0].arguments == {"html_content": HTML}
    assert result.trace[0].observation["status"] == "needs_user_action"
    generated = generator.call_args.kwargs
    assert generated["config"].tools == []
    assert "原文依頼:\nホームページを作成して" in generated["contents"][0].parts[0].text
    assert "https://not-published.example" not in result.text


@pytest.mark.parametrize("response", [
    model_response(text_part("<!doctype html><html><body>unfinished")),
    model_response(text_part(HTML), function_part()),
])
def test_invalid_or_tool_call_html_fails_without_prepare(response):
    registry, prepare = registry_with_prepare()
    client, generator = fake_client(response)

    result = AgentCore(client=client, registry=registry).run("HTMLを生成して")

    assert result.status == "failed" and result.tool_calls == 0
    assert generator.call_count == 1 and prepare.call_count == 0
    assert "公開されていません" in result.text and "http" not in result.text


def test_prepare_error_is_failed_and_never_exposes_its_unpublished_url():
    registry, prepare = registry_with_prepare({"status": "error", "message": "https://not-published.example/secret"})
    client, generator = fake_client(model_response(text_part(HTML)))

    result = AgentCore(client=client, registry=registry).run("Webサイトを作って")

    assert result.status == "failed" and result.tool_calls == 1
    assert generator.call_count == prepare.call_count == 1
    assert result.trace[0].observation["status"] == "error"
    assert "https://not-published.example" not in result.text


def test_general_request_and_current_prompt_url_fast_path_do_not_use_creation_prepare():
    registry, prepare = registry_with_prepare()
    fetch = Mock(return_value={"status": "success", "url": "https://example.com/", "title": "Page", "content_preview": "Body"})
    registry.register("fetch_and_summarize_url", "fetch", {
        "type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"],
    }, fetch)
    client, generator = fake_client(model_response(text_part("一般回答")), model_response(text_part("URLの要約")))
    agent = AgentCore(client=client, registry=registry)

    general = agent.run("ホームページについて説明して")
    url_result = agent.run("会話履歴: HTMLを生成して\n今回の依頼: https://example.com/", current_prompt="https://example.com/")

    assert general.status == url_result.status == "completed"
    assert prepare.call_count == 0 and fetch.call_count == 1 and generator.call_count == 2
    assert generator.call_args_list[0].kwargs["config"].tools
    assert generator.call_args_list[1].kwargs["config"].tools == []


def test_creation_request_keeps_generic_path_when_prepare_tool_is_not_registered():
    client, generator = fake_client(model_response(text_part("通常応答")))
    agent = AgentCore(client=client, registry=ToolRegistry())

    result = agent.run("ホームページを作成して")

    assert result.status == "completed" and result.text == "通常応答"
    assert result.tool_calls == 0 and generator.call_count == 1


def test_creation_keeps_image_parts_private_mode_and_context(monkeypatch):
    monkeypatch.setenv("CORE_PRIVATE_CONTEXT", "private-profile-for-test")
    registry, prepare = registry_with_prepare()
    client, generator = fake_client(model_response(text_part(HTML)))
    image = {"data": b"image-bytes", "mime_type": "image/png"}

    result = AgentCore(client=client, registry=registry).run(
        "会話履歴: cafe\n今回の依頼: HTMLを生成して",
        current_prompt="HTMLを生成して",
        private_mode=True,
        images=[image],
    )

    assert result.status == "needs_user_action" and prepare.call_count == 1
    generated = generator.call_args.kwargs
    assert generated["config"].tools == []
    assert "private-profile-for-test" in generated["config"].system_instruction
    assert generated["contents"][0].parts[1].inline_data.data == image["data"]
    assert "会話コンテキスト" in generated["contents"][0].parts[0].text


@pytest.mark.parametrize("creation_prompt", ["ホームページを作成して", "Webサイトを作って", "HTMLを生成して", "Create a website"])
def test_conservative_website_creation_classification_accepts_explicit_requests(creation_prompt):
    assert _is_website_creation_request(creation_prompt)


@pytest.mark.parametrize("creation_prompt", [
    "https://example.com を解析して",
    "ホームページの改善案をください",
    "HTMLを生成する方法を説明して",
    "「HTMLを生成して」",
    "「HTMLを生成して」と言いました",
    "ホームページを作成しないで",
])
def test_conservative_website_creation_classification_rejects_non_creation_requests(creation_prompt):
    assert not _is_website_creation_request(creation_prompt)
