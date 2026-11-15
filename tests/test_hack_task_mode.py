from types import SimpleNamespace

from fastapi.testclient import TestClient

import app as app_module
from karry_agent import pentest_orchestra, tools, two_factor_simulation
from karry_agent.core import AgentCore, AgentResult
from karry_agent.hack_task_mode import (
    PENTEST_TOOL_NAME,
    authorize_target,
    explicit_targets,
    is_hack_task,
    requested_target,
    scope,
)


def _tool_names(agent: AgentCore) -> set[str]:
    declarations = agent._config().tools[0].function_declarations
    return {item.name if hasattr(item, "name") else item["name"] for item in declarations}


def test_only_exact_hack_title_enables_mode():
    assert is_hack_task("ハック")
    assert not is_hack_task(" ハック")
    assert not is_hack_task("ハック ")
    assert not is_hack_task("ハック作業")
    assert not is_hack_task("hack")


def test_explicit_targets_extracts_http_urls_without_sentence_punctuation():
    assert explicit_targets("対象は https://sandbox.example.com/api/ です。") == {
        "https://sandbox.example.com/api"
    }


def test_requested_target_requires_one_explicit_url():
    assert requested_target("普通の相談です") is None
    try:
        requested_target("脆弱性を検証して")
    except ValueError as exc:
        assert "URL" in str(exc)
    else:
        raise AssertionError("pentest request without a URL unexpectedly succeeded")
    try:
        requested_target("https://a.example と https://b.example のペンテスト")
    except ValueError as exc:
        assert "1つ" in str(exc)
    else:
        raise AssertionError("multiple targets unexpectedly succeeded")
    assert requested_target("https://sandbox.example.com の脆弱性を検証") == (
        "https://sandbox.example.com"
    )


def test_two_factor_simulation_is_fixed_and_uses_no_external_inputs():
    assert two_factor_simulation.requested("二段階認証の攻撃デモを見せて")
    assert not two_factor_simulation.requested("二段階認証を設定する方法")
    report = two_factor_simulation.render_markdown()
    assert "ワンタイムコード" in report
    assert two_factor_simulation.SYNTHETIC_OTP in report
    assert "実サービスでは使用できません" in report
    assert "レート制限" in report
    assert "実アカウント" in report
    assert "突破方法" in report


def test_target_authorization_is_request_scoped():
    with scope("通常", "https://sandbox.example.com"):
        try:
            authorize_target("https://sandbox.example.com")
        except PermissionError as exc:
            assert "「ハック」" in str(exc)
        else:
            raise AssertionError("normal task unexpectedly authorized")

    with scope("ハック", "https://sandbox.example.com/api を検証"):
        assert authorize_target("https://sandbox.example.com/api/") == "https://sandbox.example.com/api"
        try:
            authorize_target("https://other.example.com")
        except PermissionError as exc:
            assert "メッセージ本文" in str(exc)
        else:
            raise AssertionError("unmentioned target unexpectedly authorized")


def test_pentest_tool_is_hidden_outside_hack_task():
    agent = AgentCore(client=SimpleNamespace(), model_id="test-model")
    assert PENTEST_TOOL_NAME not in _tool_names(agent)
    with scope("通常", "https://sandbox.example.com"):
        assert PENTEST_TOOL_NAME not in _tool_names(agent)


def test_pentest_tool_is_exposed_only_inside_hack_task():
    agent = AgentCore(client=SimpleNamespace(), model_id="test-model")
    with scope("ハック", "https://sandbox.example.com を検証"):
        assert PENTEST_TOOL_NAME in _tool_names(agent)
        assert "任意シェル" in agent._config().system_instruction
    assert PENTEST_TOOL_NAME not in _tool_names(agent)


def test_pentest_tool_rejects_normal_task_without_network(monkeypatch):
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(pentest_orchestra, "run_authorized_pentest", fail_if_called)
    with scope("通常", "https://sandbox.example.com"):
        result = tools.run_authorized_pentest("https://sandbox.example.com")
    assert result["status"] == "error"
    assert not called


def test_pentest_tool_runs_bounded_orchestra_for_explicit_target(monkeypatch):
    seen = []
    report = pentest_orchestra.PentestReport(
        target="https://sandbox.example.com",
        started_at="2026-01-01T00:00:00Z",
        probes_run=1,
        held=1,
        authorization="explicit test authorization",
    )

    def fake_run(target):
        seen.append(target)
        return report

    monkeypatch.setattr(pentest_orchestra, "run_authorized_pentest", fake_run)
    with scope("ハック", "https://sandbox.example.com/ を検証して"):
        result = tools.run_authorized_pentest("https://sandbox.example.com")
    assert seen == ["https://sandbox.example.com"]
    assert result["status"] == "completed"
    assert result["guards_ok"] is True
    assert "自己脆弱性検証レポート" in result["report_markdown"]


def test_chat_request_binds_tool_visibility_to_selected_task_title(monkeypatch):
    monkeypatch.setenv("KARRY_TASK_STORAGE", "memory")
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    for name in (
        "_RUNS", "_SESSIONS", "_PRIVATE_SESSIONS", "_PENDING_CALENDAR",
        "_SESSION_META", "_SESSION_LOCKS", "_SESSION_APPROVALS",
    ):
        monkeypatch.setattr(app_module, name, {})

    observed: list[set[str]] = []

    class CapturingAgent(AgentCore):
        def __init__(self):
            super().__init__(client=SimpleNamespace(), model_id="test-model")

        def run(self, prompt, **kwargs):
            observed.append(_tool_names(self))
            return AgentResult(text="ok", steps=1, tool_calls=0)

    previous = dict(app_module.app.dependency_overrides)
    app_module.app.dependency_overrides[app_module.get_agent] = CapturingAgent
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer synthetic-owner"}
    try:
        normal = client.post("/api/sessions", headers=headers, json={"title": "通常"}).json()
        hack = client.post("/api/sessions", headers=headers, json={"title": "ハック"}).json()
        assert client.post("/api/chat", headers=headers, json={
            "session_id": normal["session_id"],
            "prompt": "https://sandbox.example.com を検証",
        }).status_code == 200
        assert client.post("/api/chat", headers=headers, json={
            "session_id": hack["session_id"],
            "prompt": "https://sandbox.example.com を検証",
        }).status_code == 200
    finally:
        app_module.app.dependency_overrides.clear()
        app_module.app.dependency_overrides.update(previous)

    assert PENTEST_TOOL_NAME not in observed[0]
    assert PENTEST_TOOL_NAME in observed[1]


def test_secret_hack_task_runs_fixed_pentest_without_model_or_persistence(monkeypatch):
    monkeypatch.setenv("KARRY_TASK_STORAGE", "memory")
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    for name in (
        "_RUNS", "_SESSIONS", "_PRIVATE_SESSIONS", "_PENDING_CALENDAR",
        "_SESSION_META", "_SESSION_LOCKS", "_SESSION_APPROVALS",
    ):
        monkeypatch.setattr(app_module, name, {})
    monkeypatch.setattr(app_module.secret_chat, "store", app_module.secret_chat.SecretChatStore())
    monkeypatch.setattr(
        app_module.domain_authorization,
        "target_is_authorized",
        lambda target, *, session_id=None: target == "https://sandbox.example.com",
    )
    seen = []

    def fake_pentest(target):
        seen.append(target)
        return {
            "status": "completed",
            "report_markdown": "# 固定検査\n\n問題なし",
        }

    monkeypatch.setattr(app_module, "run_authorized_pentest", fake_pentest)

    class ModelMustNotStart:
        def __init__(self):
            raise AssertionError("secret hack pentest must not initialize Gemini")

    previous = dict(app_module.app.dependency_overrides)
    app_module.app.dependency_overrides[app_module.get_agent] = ModelMustNotStart
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer synthetic-owner"}
    try:
        hack = client.post("/api/sessions", headers=headers, json={"title": "ハック"}).json()
        response = client.post("/api/chat", headers=headers, json={
            "session_id": hack["session_id"],
            "prompt": "https://sandbox.example.com の脆弱性を検証して",
            "isSecret": True,
            "request_id": "secret-hack-1",
        })
    finally:
        app_module.app.dependency_overrides.clear()
        app_module.app.dependency_overrides.update(previous)

    assert response.status_code == 200
    payload = response.json()
    assert seen == ["https://sandbox.example.com"]
    assert payload["status"] == "completed"
    assert payload["isSecret"] is True
    assert payload["persistence"] == "none"
    assert payload["trace"] == []
    assert payload["tool_calls"] == 1
    assert app_module._SESSIONS[hack["session_id"]].get_history() == []


def test_secret_hack_task_rejects_unverified_remote_target_before_network(monkeypatch):
    called = False

    def fail_if_called(target):
        nonlocal called
        called = True
        raise AssertionError("unverified target must not reach pentest transport")

    monkeypatch.setattr(app_module, "run_authorized_pentest", fail_if_called)
    monkeypatch.setattr(
        app_module.domain_authorization,
        "target_is_authorized",
        lambda target, *, session_id=None: False,
    )
    result = app_module._secret_hack_result(
        "ハック",
        "https://remote.example の脆弱性を検証して",
        session_id="hack-session",
    )
    assert result["status"] == "needs_user_action"
    assert "所有者承認" in result["text"]
    assert result["tool_calls"] == 0
    assert called is False


def test_secret_hack_task_requires_url_without_starting_model(monkeypatch):
    monkeypatch.setenv("KARRY_TASK_STORAGE", "memory")
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    for name in (
        "_RUNS", "_SESSIONS", "_PRIVATE_SESSIONS", "_PENDING_CALENDAR",
        "_SESSION_META", "_SESSION_LOCKS", "_SESSION_APPROVALS",
    ):
        monkeypatch.setattr(app_module, name, {})
    monkeypatch.setattr(app_module.secret_chat, "store", app_module.secret_chat.SecretChatStore())

    class ModelMustNotStart:
        def __init__(self):
            raise AssertionError("missing target must be handled before Gemini")

    previous = dict(app_module.app.dependency_overrides)
    app_module.app.dependency_overrides[app_module.get_agent] = ModelMustNotStart
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer synthetic-owner"}
    try:
        hack = client.post("/api/sessions", headers=headers, json={"title": "ハック"}).json()
        response = client.post("/api/chat", headers=headers, json={
            "session_id": hack["session_id"],
            "prompt": "脆弱性を検証して",
            "isSecret": True,
            "request_id": "secret-hack-no-url",
        })
    finally:
        app_module.app.dependency_overrides.clear()
        app_module.app.dependency_overrides.update(previous)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_user_action"
    assert "URL" in payload["response"]
    assert payload["tool_calls"] == 0


def test_secret_hack_task_runs_synthetic_2fa_demo_without_model_or_network(monkeypatch):
    monkeypatch.setenv("KARRY_TASK_STORAGE", "memory")
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    for name in (
        "_RUNS", "_SESSIONS", "_PRIVATE_SESSIONS", "_PENDING_CALENDAR",
        "_SESSION_META", "_SESSION_LOCKS", "_SESSION_APPROVALS",
    ):
        monkeypatch.setattr(app_module, name, {})
    monkeypatch.setattr(app_module.secret_chat, "store", app_module.secret_chat.SecretChatStore())

    class ModelMustNotStart:
        def __init__(self):
            raise AssertionError("synthetic 2FA demo must not initialize Gemini")

    previous = dict(app_module.app.dependency_overrides)
    app_module.app.dependency_overrides[app_module.get_agent] = ModelMustNotStart
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer synthetic-owner"}
    try:
        hack = client.post("/api/sessions", headers=headers, json={"title": "ハック"}).json()
        response = client.post("/api/chat", headers=headers, json={
            "session_id": hack["session_id"],
            "prompt": "二段階認証の攻撃デモを合成データで見せて。入力値: test@example.invalid",
            "isSecret": True,
            "request_id": "secret-hack-2fa-demo",
        })
    finally:
        app_module.app.dependency_overrides.clear()
        app_module.app.dependency_overrides.update(previous)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["isSecret"] is True
    assert payload["persistence"] == "none"
    assert payload["trace"] == []
    assert payload["tool_calls"] == 1
    assert "合成2FA攻撃・防御デモ" in payload["response"]
    assert "test@example.invalid" not in payload["response"]
    assert app_module._SESSIONS[hack["session_id"]].get_history() == []
