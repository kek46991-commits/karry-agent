"""SadTalker の外部通信境界を、実ネットワークなしの子プロセスで検証する。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from karry_agent import sadtalker as st


MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20


@pytest.fixture
def fake_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """子 worker が import する fake gradio_client/httpx を PYTHONPATH に置く。"""

    root = tmp_path / "fake_sdk"
    (root / "gradio_client").mkdir(parents=True)
    (root / "gradio_client" / "__init__.py").write_text(
        '''
import json, os, signal, time
ROOT = os.path.dirname(os.path.dirname(__file__))

def handle_file(path):
    return {"path": str(path), "meta": {"_type": "gradio.FileData"}}

def log(value):
    with open(os.path.join(ROOT, "child-log.jsonl"), "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\\n")

def mode():
    try:
        with open(os.path.join(ROOT, "mode.txt"), encoding="utf-8") as stream:
            return stream.read().strip()
    except OSError:
        return "success"

NAMES = [
 "source_image", "driven_audio", "preprocess_type", "is_still_mode", "enhancer",
 "batch_size", "size_of_image", "pose_style", "facerender", "exp_weight",
 "use_ref_video", "ref_video", "ref_info", "use_idle_mode", "length_of_audio", "blink_every",
]

class Job:
    def result(self):
        selected = mode()
        if selected == "ignore_term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(60)
        if selected == "sleep":
            time.sleep(60)
        if selected == "failure":
            raise RuntimeError("private-provider-error: secret-token")
        if selected == "outside_url":
            return "https://outside.example.invalid/result.mp4"
        if selected == "path_traversal":
            return "https://owner-name.hf.space/file=../../etc/passwd"
        if selected == "redirect":
            return "https://owner-name.hf.space/redirect.mp4"
        return {"video": {"url": "https://owner-name.hf.space/file=video.mp4"}, "subtitles": None}

class Client:
    def __init__(self, src, **kwargs):
        self.src = "https://owner-name.hf.space/"
        log({"event": "init", "src": src, "kwargs": kwargs,
             "hf_token": os.environ.get("HF_TOKEN"), "karry_auth": os.environ.get("KARRY_AUTH")})
    def view_api(self, all_endpoints=None, print_info=True, return_format=None):
        log({"event": "view_api", "return_format": return_format, "print_info": print_info})
        return {"named_endpoints": {"/test": {"parameters": [
            {"parameter_name": name, "label": name} for name in NAMES
        ]}}}
    def submit(self, *args, api_name=None, **kwargs):
        assert all(isinstance(value, dict) and value.get("meta", {}).get("_type") == "gradio.FileData" for value in args[:2])
        log({"event": "submit", "args": list(args), "api_name": api_name})
        return Job()
    def close(self):
        log({"event": "close"})
''',
        encoding="utf-8",
    )
    (root / "httpx.py").write_text(
        '''
import json, os
MP4 = b"\\x00\\x00\\x00\\x18ftypisom" + b"\\x00" * 20
ROOT = os.path.dirname(__file__)

def log(value):
    with open(os.path.join(ROOT, "child-log.jsonl"), "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\\n")

def mode():
    try:
        with open(os.path.join(ROOT, "mode.txt"), encoding="utf-8") as stream:
            return stream.read().strip()
    except OSError:
        return "success"

class Response:
    def __init__(self):
        self.status_code = 302 if mode() == "redirect" else 200
        self.headers = {"content-length": str(len(MP4))}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def iter_bytes(self): yield MP4

class Client:
    def __init__(self, **kwargs):
        log({"event": "http_init", "kwargs": kwargs})
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def stream(self, method, url, headers=None):
        log({"event": "download", "method": method, "url": url, "headers": headers or {}})
        return Response()
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(root))
    return root


def _log(root: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (root / "child-log.jsonl").read_text(encoding="utf-8").splitlines()]


def _set_fake_mode(root: Path, value: str) -> None:
    (root / "mode.txt").write_text(value, encoding="utf-8")


def test_configuration_env_is_public_bounded_and_url_space_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KARRY_SADTALKER_SPACE", raising=False)
    monkeypatch.delenv("KARRY_SADTALKER_API_NAME", raising=False)
    monkeypatch.delenv("KARRY_SADTALKER_TIMEOUT", raising=False)
    assert st.configuration_from_env() == {
        "configured": False,
        "timeout_seconds": 90,
        "provider": "huggingface",
    }

    monkeypatch.setenv("KARRY_SADTALKER_SPACE", "https://attacker.invalid/not-allowed")
    monkeypatch.setenv("KARRY_SADTALKER_TIMEOUT", "9999")
    assert st.configuration_from_env() == {
        "configured": False,
        "timeout_seconds": 180,
        "provider": "huggingface",
    }
    assert st.adapter_from_env().configured is False

    monkeypatch.setenv("KARRY_SADTALKER_SPACE", "owner/space_name")
    monkeypatch.setenv("KARRY_SADTALKER_TIMEOUT", "1")
    assert st.configuration_from_env() == {
        "configured": True,
        "timeout_seconds": 5,
        "provider": "huggingface",
    }


async def test_fake_child_uses_16_profile_and_no_karry_credentials(
    fake_sdk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_private_test_value")
    monkeypatch.setenv("KARRY_AUTH", "must-not-reach-child")
    _set_fake_mode(fake_sdk, "success")
    adapter = st.SadTalkerAdapter("owner/name", timeout_seconds=5)

    output = await adapter.generate(b"image", b"wav")

    assert output == MP4
    events = _log(fake_sdk)
    init = next(event for event in events if event["event"] == "init")
    assert init["karry_auth"] is None
    assert init["hf_token"] == "hf_private_test_value"
    kwargs = init["kwargs"]
    assert kwargs["max_workers"] == 1
    assert kwargs["verbose"] is False
    assert kwargs["analytics_enabled"] is False
    assert kwargs["download_files"] is False
    assert next(event for event in events if event["event"] == "view_api") == {
        "event": "view_api", "return_format": "dict", "print_info": False
    }
    submit = next(event for event in events if event["event"] == "submit")
    assert submit["api_name"] == "/test"
    assert submit["args"][2:] == [
        "full", True, False, 1, 256, 0, "facevid2vid", 1, False, None, "pose", False, 5, True,
    ]
    from gradio_client.utils import is_file_obj_with_meta
    assert is_file_obj_with_meta(submit["args"][0]) and is_file_obj_with_meta(submit["args"][1])
    assert Path(submit["args"][0]["path"]).name == "source-image"
    assert Path(submit["args"][1]["path"]).name == "driven-audio.wav"
    download = next(event for event in events if event["event"] == "download")
    assert download["url"].startswith("https://owner-name.hf.space/")
    assert download["headers"] == {"Authorization": "Bearer hf_private_test_value"}


@pytest.mark.parametrize("mode,expected", [
    ("outside_url", st.SadTalkerErrorCode.UNAVAILABLE),
    ("path_traversal", st.SadTalkerErrorCode.UNAVAILABLE),
    ("redirect", st.SadTalkerErrorCode.UNAVAILABLE),
    ("failure", st.SadTalkerErrorCode.UNAVAILABLE),
])
async def test_fake_child_provider_failures_are_safe(
    fake_sdk: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: st.SadTalkerErrorCode
) -> None:
    _set_fake_mode(fake_sdk, mode)
    with pytest.raises(st.SadTalkerError) as raised:
        await st.SadTalkerAdapter("owner/name", timeout_seconds=5).generate(b"image", b"wav")
    assert raised.value.code == expected
    assert "secret-token" not in str(raised.value)
    events = _log(fake_sdk)
    if mode in {"outside_url", "path_traversal"}:
        assert not any(event["event"] == "download" for event in events)


async def test_timeout_terminates_fake_child_and_releases_global_slot(
    fake_sdk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_fake_mode(fake_sdk, "sleep")
    adapter = st.SadTalkerAdapter("owner/name", timeout_seconds=5)
    with pytest.raises(st.SadTalkerError) as raised:
        await adapter.generate(b"image", b"wav")
    assert raised.value.code == st.SadTalkerErrorCode.TIMEOUT

    # timeout 後に semaphore が残っていれば、次の呼出しは busy になってしまう。
    _set_fake_mode(fake_sdk, "success")
    assert await adapter.generate(b"image", b"wav") == MP4


async def test_timeout_kills_sigterm_ignoring_child_and_cancel_releases_slot(
    fake_sdk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kill escalation should not leave a process or the process-wide busy slot behind.
    monkeypatch.setattr(st, "_PROCESS_STOP_SECONDS", 0.1)
    _set_fake_mode(fake_sdk, "ignore_term")
    adapter = st.SadTalkerAdapter("owner/name", timeout_seconds=5)
    with pytest.raises(st.SadTalkerError) as raised:
        await adapter.generate(b"image", b"wav")
    assert raised.value.code == st.SadTalkerErrorCode.TIMEOUT

    _set_fake_mode(fake_sdk, "sleep")
    pending = asyncio.create_task(adapter.generate(b"image", b"wav"))
    await asyncio.sleep(0.2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    _set_fake_mode(fake_sdk, "success")
    assert await adapter.generate(b"image", b"wav") == MP4


async def test_busy_is_nonblocking_and_local_symlink_output_is_rejected(tmp_path: Path) -> None:
    assert st._EXTERNAL_JOB_GUARD.acquire(blocking=False)
    try:
        with pytest.raises(st.SadTalkerError) as raised:
            await st.SadTalkerAdapter("owner/name").generate(b"image", b"wav")
        assert raised.value.code == st.SadTalkerErrorCode.BUSY
    finally:
        st._EXTERNAL_JOB_GUARD.release()

    target = tmp_path / "real.mp4"
    target.write_bytes(MP4)
    link = tmp_path / "result.mp4"
    link.symlink_to(target)
    with pytest.raises(st.SadTalkerError) as raised:
        st._read_verified_mp4(link)
    assert raised.value.code == st.SadTalkerErrorCode.UNAVAILABLE


def test_error_code_is_a_fixed_safe_enum() -> None:
    assert st.SadTalkerError("sadtalker_busy").code == "sadtalker_busy"
    with pytest.raises(ValueError):
        st.SadTalkerError("private-provider-message")
