import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import core, tools
from karry_agent.healing_client import HealingAPIClient
from karry_agent.repair_pipeline import (GitHubRepairPublisher, RepairPipeline,
    configure_repair_pipeline, validated_changes)
from karry_agent.self_healing import RepairStrategy, SelfHealingEngine, redact


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key in ("KARRY_API_TOKEN", "KARRY_ALLOW_SHELL", "KARRY_SELF_HEALING_GENERATE_PATCHES", "KARRY_SELF_HEALING_CREATE_PRS"):
        monkeypatch.delenv(key, raising=False)
    engine = SelfHealingEngine(cooldown=0)
    monkeypatch.setattr(web, "healing_engine", engine)
    monkeypatch.setattr(tools, "healing_engine", engine)
    return engine


def error_with_trace():
    try:
        raise RuntimeError("example failure")
    except Exception as exc:
        return exc


def response(status=200, data=None):
    obj = Mock(status_code=status)
    obj.json.return_value = data if data is not None else {"tools": []}
    obj.raise_for_status.side_effect = None if status < 400 else RuntimeError("remote failed")
    return obj


def test_bounded_logs_and_trace_outside_except():
    engine = SelfHealingEngine(max_logs=2, cooldown=0)
    result = engine.analyze_and_heal(error_with_trace(), {"source": "test"})
    assert result["status"] == "diagnosed"
    assert engine.error_logs[-1]["trace"][-1]["function"] == "error_with_trace"
    for _ in range(4):
        engine.analyze_and_heal(RuntimeError("other"))
    assert len(engine.error_logs) == 2
    logs = engine.error_logs
    logs[0]["error"] = "mutated"
    assert engine.error_logs[0]["error"] != "mutated"


def test_secret_redaction_and_no_request_context(monkeypatch, caplog):
    monkeypatch.setenv("KARRY_API_TOKEN", "private-key")
    engine = SelfHealingEngine()
    engine.analyze_and_heal(RuntimeError("Bearer private-key at https://host/path?secret=raw"),
                            {"headers": {"Authorization": "Bearer private-key"}, "prompt": "private prompt", "source": "test"})
    combined = json.dumps(engine.error_logs) + caplog.text
    assert "private-key" not in combined and "private prompt" not in combined and "secret=raw" not in combined
    assert redact({"client_secret": "x"})["client_secret"] == "[redacted]"


def test_duplicate_cooldown_and_expiry():
    clock = [0.0]
    engine = SelfHealingEngine(cooldown=5, clock=lambda: clock[0])
    first = engine.analyze_and_heal(RuntimeError("same"))
    second = engine.analyze_and_heal(RuntimeError("same"))
    assert second["status"] == "suppressed" and first["incident_id"] == second["incident_id"]
    clock[0] = 6
    assert engine.analyze_and_heal(RuntimeError("same"))["status"] == "diagnosed"


def test_actual_repair_and_verification():
    engine = SelfHealingEngine()
    state = {"working": False}
    engine.register(RepairStrategy("reset_owned_cache", lambda *_: True,
        lambda *_: state.update(working=True), lambda *_: state["working"]))
    result = engine.analyze_and_heal(RuntimeError("broken"))
    assert state["working"] is True
    assert result["status"] == "repaired" and result["verified"] is True


def test_verification_failure_rolls_back():
    engine = SelfHealingEngine()
    state = []
    engine.register(RepairStrategy("reset", lambda *_: True, lambda *_: state.append(1),
                                   lambda *_: False, lambda *_: state.clear()))
    result = engine.analyze_and_heal(RuntimeError("broken"))
    assert result["status"] == "repair_failed" and not state


def test_strategy_error_does_not_replace_original():
    engine = SelfHealingEngine()
    engine.register(RepairStrategy("bad", lambda *_: True, Mock(side_effect=RuntimeError("secondary")), lambda *_: True))
    assert engine.analyze_and_heal(RuntimeError("original"))["status"] == "repair_failed"
    assert engine.error_logs[0]["error"] == "original"


def test_recursive_healing_is_suppressed():
    engine = SelfHealingEngine()
    observed = []
    def apply(*_):
        observed.append(engine.analyze_and_heal(RuntimeError("secondary")))
    engine.register(RepairStrategy("recursive", lambda *_: True, apply, lambda *_: True))
    engine.analyze_and_heal(RuntimeError("first"))
    assert observed[0]["reason"] == "recursive_healing_suppressed"
    assert len(engine.error_logs) == 1


def test_inbound_401_is_not_repaired_and_does_not_call_patch():
    engine = SelfHealingEngine()
    hook = Mock()
    engine.set_patch_hook(hook)
    result = engine.analyze_and_heal(RuntimeError("Invalid bearer token"), {"direction": "inbound"})
    assert result["status"] == "diagnosed" and result["authentication_bypassed"] is False
    hook.assert_not_called()


def test_outbound_auth_repair_is_verified():
    engine = SelfHealingEngine()
    headers = {"authorization": "broken", "Other": "kept"}
    verify = Mock(return_value=True)
    result = engine.analyze_and_heal(RuntimeError("Invalid bearer token"),
        {"direction": "outbound", "headers": headers, "credential_provider": lambda: "owned-token", "verify": verify})
    assert result["status"] == "repaired"
    assert headers == {"Authorization": "Bearer owned-token", "Other": "kept"}
    assert "owned-token" not in str(engine.error_logs)


@pytest.mark.parametrize("verify", [lambda _: False, Mock(side_effect=RuntimeError("failed"))])
def test_outbound_auth_failure_restores_headers(verify):
    engine = SelfHealingEngine()
    headers = {"Authorization": "old"}
    result = engine.analyze_and_heal(RuntimeError("bearer token invalid"),
        {"direction": "outbound", "headers": headers, "credential_provider": lambda: "new", "verify": verify})
    assert result["status"] == "repair_failed" and headers["Authorization"] == "old"


def test_classifies_structured_http_status():
    engine = SelfHealingEngine()
    assert engine.classify(RuntimeError("remote"), {"status_code": 401}) == "authentication"
    assert engine.classify(RuntimeError("remote"), {"status_code": 429}) == "rate_limit"


def test_client_success_single_call():
    transport = Mock(return_value=response())
    client = HealingAPIClient(credential_provider=lambda: "owned", transport=transport)
    assert client.request("GET", "/api/agent/tools")["status"] == "success"
    assert transport.call_count == 1
    assert transport.call_args.kwargs["allow_redirects"] is False


def test_client_auth_read_recovery_once():
    transport = Mock(side_effect=[response(401), response(200)])
    client = HealingAPIClient(credential_provider=lambda: "owned", engine=SelfHealingEngine(), transport=transport)
    result = client.request("GET", "/api/agent/tools", headers={"Authorization": "wrong"})
    assert result["status"] == "success" and result["healing_status"]["verified"] is True
    assert transport.call_count == 2
    assert transport.call_args.kwargs["headers"]["Authorization"] == "Bearer owned"


def test_client_does_not_retry_writes():
    transport = Mock(return_value=response(401))
    client = HealingAPIClient(credential_provider=lambda: "owned", transport=transport, engine=SelfHealingEngine())
    result = client.request("POST", "/api/agent/tools/execute", json={"tool_name": "add_calendar_event"})
    assert result["status"] == "error" and transport.call_count == 1


def test_client_repair_failures_are_not_success():
    transport = Mock(side_effect=[response(401), response(401)])
    client = HealingAPIClient(credential_provider=lambda: "owned", transport=transport, engine=SelfHealingEngine())
    assert client.request("GET", "/api/agent/tools")["status"] == "error"
    assert transport.call_count == 2


@pytest.mark.parametrize("path", ["//evil/x", "https://evil/x", "/x?token=secret", "/x#fragment"])
def test_client_cannot_forward_credential_to_arbitrary_url(path):
    transport = Mock()
    client = HealingAPIClient(credential_provider=lambda: "owned", transport=transport)
    with pytest.raises(ValueError):
        client.request("GET", path)
    transport.assert_not_called()


def test_client_result_error_inside_200():
    transport = Mock(return_value=response(data={"result": {"status": "error", "error": "tool error"}}))
    client = HealingAPIClient(credential_provider=lambda: "owned", transport=transport, engine=SelfHealingEngine())
    assert client.request("POST", "/api/agent/tools/execute")["status"] == "error"
    assert transport.call_count == 1


def test_gemini_existing_retry_recovers_and_marks_verified():
    engine = SelfHealingEngine(cooldown=60)
    answer = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="ok")]))])
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=[TimeoutError("timeout"), answer])))
    result = core.AgentCore(client=client, sleep_fn=lambda _: None, healer=engine).run("hello")
    assert result.text == "ok" and result.metrics.retries == 1
    assert engine.error_logs[0]["healing_status"]["status"] == "repaired"


def test_gemini_retry_exhaustion_not_repaired():
    engine = SelfHealingEngine()
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=TimeoutError("timeout"))))
    result = core.AgentCore(client=client, sleep_fn=lambda _: None, max_retries=2, healer=engine).run("hello")
    assert result.status == "failed"
    assert engine.error_logs[0]["healing_status"]["action"] == "retry_budget_exhausted"


def test_registry_exception_records_trace_without_replaying(isolated):
    registry = tools.ToolRegistry()
    handler = Mock(side_effect=RuntimeError("bug"))
    registry.register("example", "x", {}, handler)
    result = registry.execute("example", {})
    assert result["status"] == "error" and "healing_status" in result
    assert isolated.error_logs[-1]["trace"]
    assert handler.call_count == 1


def test_registry_structured_errors_are_observed(isolated):
    registry = tools.ToolRegistry()
    registry.register("example", "x", {}, lambda: {"status": "error", "error": "failure"})
    assert registry.execute("example", {})["healing_status"]["status"] == "diagnosed"
    assert isolated.error_logs[-1]["trace"] == []


def test_fastapi_global_handler_records_original_trace(isolated):
    from fastapi import FastAPI
    app = FastAPI()
    app.add_exception_handler(Exception, web.global_exception_handler)
    @app.get("/failure")
    def fail():
        raise RuntimeError("private failure")
    result = TestClient(app, raise_server_exceptions=False).get("/failure")
    assert result.status_code == 500
    assert result.json()["detail"] == "Internal Agent Error Handled by Self-Healing Core"
    assert isolated.error_logs[-1]["trace"]
    assert "private failure" not in result.text


def test_http_auth_stays_401_and_owner_can_inspect(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "owner")
    with TestClient(web.app) as client:
        failed = client.get("/api/agent/healing")
        assert failed.status_code == 401
        assert failed.json()["healing_status"]["status"] == "diagnosed"
        headers = {"Authorization": "Bearer owner"}
        assert client.get("/api/agent/healing", headers=headers).json()["enabled"] is True
        logs = client.get("/api/agent/healing/incidents", headers=headers).json()["incidents"]
        assert len(logs) == 1


def test_healing_admin_endpoints_fail_closed():
    with TestClient(web.app) as client:
        assert client.get("/api/agent/healing").status_code == 503


def test_request_validation_no_body_secrets(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "owner")
    with TestClient(web.app) as client:
        result = client.post("/api/agent/tools/execute", headers={"Authorization": "Bearer owner"},
                             json={"tool_name": {"secret": "private body"}})
    assert result.status_code == 422
    assert "private body" not in result.text


def test_api_caught_failure_and_sse_use_engine(isolated):
    class BrokenAgent:
        def run(self, prompt):
            raise RuntimeError("original core error")
    web.app.dependency_overrides[web.get_agent] = BrokenAgent
    try:
        with TestClient(web.app, raise_server_exceptions=False) as client:
            result = client.post("/run", json={"prompt": "hello"})
            assert result.status_code == 500 and "healing_status" in result.json()
            stream = client.post("/chat", json={"prompt": "hello", "stream": True})
            assert "event: error" in stream.text and "healing_status" in stream.text
        assert any(log["error"] == "original core error" for log in isolated.error_logs)
    finally:
        web.app.dependency_overrides.pop(web.get_agent, None)


def fixture_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    root.joinpath("example.py").write_text("VALUE = 1\n")
    return root


def proposal(old="VALUE = 1", new="VALUE = 2", path="example.py"):
    return {"summary": "fix", "edits": [{"path": path, "old": old, "new": new}]}


def test_patch_exact_edit_and_no_live_write(tmp_path):
    root = fixture_root(tmp_path)
    changed = validated_changes(root, proposal(), ("example.py",))
    assert changed["example.py"] == "VALUE = 2\n"
    assert root.joinpath("example.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize("patch", [proposal(path="../outside.py"), proposal(old="missing"),
                                   proposal(new="VALUE = ("), {"edits": []}])
def test_patch_rejects_path_stale_syntax_and_empty(tmp_path, patch):
    root = fixture_root(tmp_path)
    with pytest.raises((ValueError, SyntaxError)):
        validated_changes(root, patch, ("example.py",))


def test_patch_rejects_secret(monkeypatch, tmp_path):
    root = fixture_root(tmp_path)
    monkeypatch.setenv("KARRY_API_TOKEN", "secret-token")
    with pytest.raises(ValueError, match="secret"):
        validated_changes(root, proposal(new='VALUE = "secret-token"'), ("example.py",))


def test_async_patch_generated_but_not_repaired(tmp_path):
    root = fixture_root(tmp_path)
    engine = SelfHealingEngine()
    generator = Mock(return_value=proposal())
    pipeline = RepairPipeline(engine, root, generator=generator, allowed_paths=("example.py",), artifact_dir=tmp_path / "patches")
    engine.set_patch_hook(pipeline.submit)
    result = engine.analyze_and_heal(error_with_trace())
    pipeline.close()
    final = engine.error_logs[0]["healing_status"]
    assert final["status"] == "patch_ready" and final["verified"] is False
    assert list((tmp_path / "patches").glob("*.patch"))
    assert root.joinpath("example.py").read_text() == "VALUE = 1\n"
    assert generator.call_count == 1


def test_patch_generation_budget(tmp_path):
    root = fixture_root(tmp_path)
    engine = SelfHealingEngine(cooldown=0)
    generator = Mock(return_value=proposal())
    pipeline = RepairPipeline(engine, root, generator=generator, allowed_paths=("example.py",), max_jobs=1,
                              artifact_dir=tmp_path / "patches")
    engine.set_patch_hook(pipeline.submit)
    engine.analyze_and_heal(RuntimeError("one"))
    second = engine.analyze_and_heal(RuntimeError("two"))
    pipeline.close()
    assert second["action"] == "repair_generation_budget_exhausted"
    assert generator.call_count == 1


def test_invalid_generated_patch_is_not_published(tmp_path):
    root = fixture_root(tmp_path)
    engine = SelfHealingEngine()
    publisher = Mock()
    pipeline = RepairPipeline(engine, root, generator=Mock(return_value=proposal(new="bad (")), publisher=publisher,
                              allowed_paths=("example.py",), artifact_dir=tmp_path / "patches")
    engine.set_patch_hook(pipeline.submit)
    engine.analyze_and_heal(RuntimeError("bug"))
    pipeline.close()
    assert engine.error_logs[0]["healing_status"]["status"] == "patch_failed"
    publisher.publish.assert_not_called()


def test_patch_generation_default_disabled(tmp_path):
    assert configure_repair_pipeline(SelfHealingEngine(), tmp_path) is None


def test_github_publisher_only_changes_new_branch():
    session = Mock()
    original = "VALUE = 1\n"
    session.request.side_effect = [response(data={"object": {"sha": "base"}}),
        response(data={"content": base64.b64encode(original.encode()).decode(), "sha": "blob"}),
        response(201, {}), response(200, {}), response(201, {"html_url": "https://github.com/owner/repo/pull/1"})]
    publisher = GitHubRepairPublisher("owner/repo", "token", session=session)
    result = publisher.publish({"incident_id": "incident"}, {"example.py": "VALUE = 2\n"}, {"example.py": original})
    calls = session.request.call_args_list
    assert calls[2].kwargs["json"]["ref"] == "refs/heads/self-healing/incident"
    assert calls[3].kwargs["json"]["branch"] != "main"
    assert calls[4].kwargs["json"]["draft"] is True
    assert result["status"] == "pr_created" and result["deployed"] is False


def test_github_remote_drift_refuses_all_writes():
    session = Mock()
    session.request.side_effect = [response(data={"object": {"sha": "base"}}),
        response(data={"content": base64.b64encode(b"different").decode(), "sha": "blob"})]
    publisher = GitHubRepairPublisher("owner/repo", "token", session=session)
    with pytest.raises(ValueError, match="stale"):
        publisher.publish({"incident_id": "incident"}, {"example.py": "x"}, {"example.py": "old"})
    assert all(call.args[0] == "GET" for call in session.request.call_args_list)


def test_owned_workspace_directory_is_actually_restored(tmp_path, monkeypatch):
    from karry_agent.self_healing import workspace_directory_strategy
    target = tmp_path / "workspace"
    monkeypatch.setenv("KARRY_WORKSPACE", str(target))
    engine = SelfHealingEngine()
    engine.register(workspace_directory_strategy())
    result = engine.analyze_and_heal(FileNotFoundError(2, "missing", str(target)))
    assert result["status"] == "repaired" and result["verified"] is True
    assert target.is_dir()


def test_missing_arbitrary_file_is_not_created(tmp_path, monkeypatch):
    from karry_agent.self_healing import workspace_directory_strategy
    target = tmp_path / "workspace"
    monkeypatch.setenv("KARRY_WORKSPACE", str(target))
    engine = SelfHealingEngine()
    engine.register(workspace_directory_strategy())
    result = engine.analyze_and_heal(FileNotFoundError(2, "missing", str(tmp_path / "unknown.txt")))
    assert result["status"] != "repaired" and not target.exists()


def test_gemini_empty_retry_is_not_successful_repair():
    engine = SelfHealingEngine()
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=[TimeoutError("timeout"), SimpleNamespace(candidates=[])])))
    result = core.AgentCore(client=client, sleep_fn=lambda _: None, healer=engine).run("hello")
    assert result.status == "failed"
    assert engine.error_logs[0]["healing_status"]["verified"] is False


def test_compatibility_entry_exports_engine():
    import agent_core
    assert agent_core.SelfHealingEngine is SelfHealingEngine
    assert agent_core.HealingAPIClient is HealingAPIClient


@pytest.mark.parametrize("text", ['{"password":"leak"}', '{"client_secret": "leak"}',
                                  "Cookie: session=leak; second=other", "passphrase=leak",
                                  "Set-Cookie: session=leak; Secure", "authorization='leak'",
                                  '{"access_token":"leak"}'])
def test_redacts_secret_strings_in_json_and_headers(text):
    assert "leak" not in redact(text)


def test_repair_source_and_exact_old_match_beyond_6000(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    source = "# comment\n" * 800 + 'api_key = "environment-reference"\nVALUE = 1\n'
    root.joinpath("example.py").write_text(source)
    engine = SelfHealingEngine()
    def generate(incident, sources):
        assert sources["example.py"] == source
        return proposal(old='api_key = "environment-reference"\nVALUE = 1',
                        new='api_key = "environment-reference"\nVALUE = 2')
    pipeline = RepairPipeline(engine, root, generator=generate, allowed_paths=("example.py",), artifact_dir=tmp_path / "patches")
    engine.set_patch_hook(pipeline.submit)
    engine.analyze_and_heal(RuntimeError("code bug"))
    pipeline.close()
    assert engine.error_logs[0]["healing_status"]["status"] == "patch_ready"
    assert root.joinpath("example.py").read_text() == source


def test_source_with_real_secret_is_not_sent(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    monkeypatch.setenv("KARRY_API_TOKEN", "very-long-private-token")
    root.joinpath("example.py").write_text('TOKEN = "very-long-private-token"\n')
    generator = Mock(return_value=proposal())
    engine = SelfHealingEngine()
    pipeline = RepairPipeline(engine, root, generator=generator, allowed_paths=("example.py",), artifact_dir=tmp_path / "patches")
    engine.set_patch_hook(pipeline.submit)
    engine.analyze_and_heal(RuntimeError("bug"))
    pipeline.close()
    generator.assert_not_called()
    assert engine.error_logs[0]["healing_status"]["status"] == "patch_failed"


def test_short_secret_identifier_not_rewritten(monkeypatch):
    from karry_agent.repair_pipeline import source_contains_secret
    monkeypatch.setenv("KARRY_API_TOKEN", "short")
    assert not source_contains_secret('short_identifier = "other"\n')
    assert source_contains_secret('TOKEN = "short"\n')


@pytest.mark.parametrize("part", [SimpleNamespace(text="   ", thought=False),
                                  SimpleNamespace(text="private thought", thought=True),
                                  SimpleNamespace(text=None, thought=False)])
def test_unusable_retry_is_not_marked_repaired(part):
    engine = SelfHealingEngine()
    answer = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=[TimeoutError("timeout"), answer])))
    result = core.AgentCore(client=client, sleep_fn=lambda _: None, healer=engine).run("hello")
    assert result.status == "failed"
    assert not engine.error_logs[0]["healing_status"]["verified"]


def test_sse_trace_recursion_has_error_event_and_incident(isolated):
    from karry_agent.core import AgentResult, ExecutionEvent
    cyclic = {}
    cyclic["self"] = cyclic
    class CyclicAgent:
        def run(self, prompt):
            return AgentResult("ok", 1, 1, trace=[ExecutionEvent(1, "cyclic", {}, cyclic, 1)])
    events = list(web._stream_chat(web.RunRequest(prompt="hello"), CyclicAgent()))
    assert "event: error" in events[-1]
    assert "agent stream failed" in events[-1]
    assert isolated.error_logs[-1]["context"]["source"] == "api_stream"
