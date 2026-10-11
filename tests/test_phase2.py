from types import SimpleNamespace

from karry_agent.cli import run_interactive
from karry_agent.logger import log_progress


def test_logging(tmp_path, monkeypatch):
    progress = tmp_path / "progress.md"
    monkeypatch.setenv("KARRY_PROGRESS_FILE", str(progress))
    log_progress("UnitTest", "Testing logger functionality.")
    assert progress.exists()
    assert "UnitTest" in progress.read_text(encoding="utf-8")


def test_cli_processes_input_and_exits(tmp_path, monkeypatch, capsys):
    progress = tmp_path / "progress.md"
    monkeypatch.setenv("KARRY_PROGRESS_FILE", str(progress))

    class FakeAgent:
        def run(self, prompt):
            return SimpleNamespace(text=f"echo: {prompt}")

    inputs = iter(["こんにちは", "exit"])
    assert run_interactive(FakeAgent(), lambda _: next(inputs)) == 0
    output = capsys.readouterr().out
    assert "echo: こんにちは" in output
    log = progress.read_text(encoding="utf-8")
    assert "CLI Started" in log
    assert "Task Processed" in log
    assert "CLI Ended" in log


def test_core_compatibility_import():
    from karry_agent.core import run_agent, run_agent_core
    assert callable(run_agent)
    assert callable(run_agent_core)
