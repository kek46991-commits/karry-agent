"""SadTalker Gradio 呼出し専用の短命な子プロセス。

stdin の限定 JSON を読み、stdout には固定形の safe JSON だけを一行出力する。
このモジュールは親 ``sadtalker`` からだけ起動する想定であり、ローカル推論を含まない。
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Final
from urllib.parse import unquote, urlsplit


MAX_OUTPUT_BYTES: Final = 60 * 1024 * 1024
MAX_REQUEST_BYTES: Final = 4096
MAX_NETWORK_SECONDS: Final = 30.0
_SPACE_ID_RE: Final = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$"
)
_API_NAME_RE: Final = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$")

# app.py の submit.click inputs と同じ順序。profile を短縮して推測することはない。
_PROFILE_16: Final = (
    "source_image",
    "driven_audio",
    "full",
    True,
    False,
    1,
    256,
    0,
    "facevid2vid",
    1,
    False,
    None,
    "pose",
    False,
    5,
    True,
)
# 公式16入力の既知の引数名だけを許可する。部分文字列で別APIを受理しない。
_PARAMETER_NAMES: Final = (
    frozenset({"source image"}),
    frozenset({"driven audio"}),
    frozenset({"preprocess", "preprocess type"}),
    frozenset({"still mode", "is still mode"}),
    frozenset({"enhancer", "use enhancer"}),
    frozenset({"batch size"}),
    frozenset({"size", "size of image"}),
    frozenset({"pose style"}),
    frozenset({"facerender"}),
    frozenset({"exp scale", "exp weight", "expression scale"}),
    frozenset({"use ref video"}),
    frozenset({"ref video"}),
    frozenset({"ref info"}),
    frozenset({"use idle mode"}),
    frozenset({"length of audio"}),
    frozenset({"use blink", "blink every", "blink"}),
)


class WorkerError(Exception):
    """親に返せる fixed code だけを持つ内部例外。"""

    _CODES: Final = frozenset({"configuration", "unavailable", "protocol", "output_invalid"})

    def __init__(self, code: str) -> None:
        self.code = code if code in self._CODES else "unavailable"
        super().__init__(self.code)


def main() -> int:
    """stdin を一度だけ処理し、safe JSON だけを stdout に書く。"""

    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        request = _parse_request(raw)
        # SDK や依存ライブラリの stdout/stderr はプロトコルを壊さないよう破棄する。
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            _run(request)
    except WorkerError as error:
        _emit({"ok": False, "code": error.code})
        return 0
    except (OSError, ValueError, TypeError, UnicodeError):
        _emit({"ok": False, "code": "unavailable"})
        return 0
    except Exception:
        # SDK のエラー・URL・token などを stdout に絶対に転写しない。
        _emit({"ok": False, "code": "unavailable"})
        return 0
    _emit({"ok": True, "output_name": "result.mp4"})
    return 0


def _parse_request(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise WorkerError("protocol")
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=lambda _item: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise WorkerError("protocol") from None
    if not isinstance(value, dict):
        raise WorkerError("protocol")
    required = {"image_path", "audio_path", "output_dir", "space_id", "api_name", "timeout_seconds"}
    if set(value) != required:
        raise WorkerError("protocol")
    if not all(isinstance(value[name], str) for name in ("image_path", "audio_path", "output_dir", "space_id", "api_name")):
        raise WorkerError("protocol")
    if not isinstance(value["timeout_seconds"], int) or not 5 <= value["timeout_seconds"] <= 180:
        raise WorkerError("protocol")
    space_id = value["space_id"]
    api_name = value["api_name"]
    if not _SPACE_ID_RE.fullmatch(space_id) or not _API_NAME_RE.fullmatch(api_name) or "//" in api_name or ".." in api_name:
        raise WorkerError("configuration")
    _validate_parent_paths(value)
    return value


def _validate_parent_paths(request: dict[str, object]) -> None:
    """親が作った input/output sibling dirs 以外を子が読書きしないよう確認する。"""

    image = Path(str(request["image_path"]))
    audio = Path(str(request["audio_path"]))
    output_dir = Path(str(request["output_dir"]))
    try:
        input_dir = image.parent
        root = input_dir.parent
        if audio.parent != input_dir or output_dir.parent != root:
            raise ValueError
        if input_dir.name != "input" or output_dir.name != "output":
            raise ValueError
        # resolve() は symlink をまたぐので、現在の parents が private root と一致することも確認する。
        if image.name != "source-image" or audio.name != "driven-audio.wav":
            raise ValueError
        root_lstat = os.lstat(root)
        input_lstat = os.lstat(input_dir)
        output_lstat = os.lstat(output_dir)
        if any(stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode) for item in (root_lstat, input_lstat, output_lstat)):
            raise ValueError
        for item in (image, audio):
            info = os.lstat(item)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ValueError
    except (OSError, ValueError):
        raise WorkerError("protocol") from None


def _run(request: dict[str, object]) -> None:
    """Gradio API 契約を読んでから一回だけ submit し、同じ Space origin から MP4 を得る。"""

    client = _make_client(
        str(request["space_id"]),
        int(request["timeout_seconds"]),
        os.environ.get("HF_TOKEN"),
    )
    try:
        # 画像や音声のアップロード前に、認証を送るSpace originを検証する。
        origin = _space_origin(getattr(client, "src", None))
        profile = _checked_profile(
            client,
            str(request["api_name"]),
            str(request["image_path"]),
            str(request["audio_path"]),
        )
        from gradio_client import handle_file
        upload_profile = (handle_file(profile[0]), handle_file(profile[1]), *profile[2:])
        job = client.submit(*upload_profile, api_name=str(request["api_name"]))
        result = job.result()
        output_url = _one_output_url(result)
        _download_same_space(
            output_url,
            origin,
            Path(str(request["output_dir"])) / "result.mp4",
            int(request["timeout_seconds"]),
            os.environ.get("HF_TOKEN"),
        )
    except WorkerError:
        raise
    except Exception:
        raise WorkerError("unavailable") from None
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _make_client(space_id: str, timeout_seconds: int, token: str | None) -> Any:
    """gradio_client 1.11 と 2.x の token keyword 差だけを明示的に吸収する。"""

    try:
        from gradio_client import Client
    except Exception:
        raise WorkerError("unavailable") from None

    try:
        parameters = inspect.signature(Client).parameters
        network_timeout = min(float(timeout_seconds), MAX_NETWORK_SECONDS)
        kwargs: dict[str, Any] = {
            "max_workers": 1,
            "verbose": False,
            "analytics_enabled": False,
            "download_files": False,
            # httpx に必ず有限 timeout を設定する。redirect は SDK 側にも追わせない。
            "httpx_kwargs": {"timeout": network_timeout, "follow_redirects": False},
        }
        if "token" in parameters:
            kwargs["token"] = token or False
        elif "hf_token" in parameters:
            kwargs["hf_token"] = token or False
        return Client(space_id, **kwargs)
    except WorkerError:
        raise
    except Exception:
        raise WorkerError("unavailable") from None


def _checked_profile(client: Any, api_name: str, image_path: str, audio_path: str) -> tuple[object, ...]:
    """view_api の辞書を確認して、既知の16入力だけをsubmitする。"""

    try:
        info = client.view_api(return_format="dict", print_info=False)
    except TypeError:
        # 1.11 以降はこの形を持つ。未知 SDK の fallback API は推測しない。
        raise WorkerError("protocol") from None
    except Exception:
        raise WorkerError("unavailable") from None
    if not isinstance(info, dict):
        raise WorkerError("protocol")
    endpoints = info.get("named_endpoints")
    endpoint = endpoints.get(api_name) if isinstance(endpoints, dict) else None
    parameters = endpoint.get("parameters") if isinstance(endpoint, dict) else None
    if not isinstance(parameters, list):
        raise WorkerError("protocol")
    count = len(parameters)
    if count == 16:
        _validate_parameter_schema(parameters, 16)
        return (image_path, audio_path, *_PROFILE_16[2:])
    raise WorkerError("protocol")


def _normalise_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _validate_parameter_schema(parameters: list[object], count: int) -> None:
    """公式プロファイルの引数名または定義済み別名と完全一致させる。"""

    if count != 16:
        raise WorkerError("protocol")
    for index, parameter in enumerate(parameters):
        if not isinstance(parameter, dict):
            raise WorkerError("protocol")
        name = _normalise_name(parameter.get("parameter_name"))
        if name not in _PARAMETER_NAMES[index]:
            raise WorkerError("protocol")


def _one_output_url(value: object) -> str:
    """download_files=False の FileData URL を一出力だけ明示的に受け取る。"""

    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return _one_output_url(value[0])
    if isinstance(value, (list, tuple)) and len(value) == 2 and value[1] is None:
        # Gradio Videoの動画＋字幕なしという既知の出力だけを受け入れる。
        return _one_output_url(value[0])
    if isinstance(value, dict) and "video" in value and set(value) <= {"video", "subtitles"}:
        return _one_output_url(value["video"])
    if isinstance(value, dict) and set(value).issuperset({"url"}) and isinstance(value.get("url"), str):
        # path/name などを信頼せず URL だけを使う。余分な provider 値は無視する。
        return str(value["url"])
    raise WorkerError("protocol")


def _space_origin(value: object) -> str:
    """Client.src の正確な https HF Space origin だけを download 許可 origin とする。"""

    if not isinstance(value, str):
        raise WorkerError("protocol")
    split = urlsplit(value)
    hostname = split.hostname
    if (
        split.scheme != "https"
        or not hostname
        or not hostname.endswith(".hf.space")
        or split.username is not None
        or split.password is not None
        or split.port not in (None, 443)
        or split.query
        or split.fragment
        or split.path not in ("", "/")
    ):
        raise WorkerError("protocol")
    return "https://" + hostname.lower()


def _download_same_space(url: str, origin: str, destination: Path, timeout_seconds: int, token: str | None) -> None:
    """redirect を追わず、60 MiB 以下の HTTPS 同一origin MP4だけを書き出す。"""

    split = urlsplit(url)
    decoded_path = unquote(split.path)
    if (
        split.scheme != "https"
        or not split.hostname
        or split.username is not None
        or split.password is not None
        or split.port not in (None, 443)
        or ("https://" + split.hostname.lower()) != origin
        or not split.path.startswith("/")
        or split.path.startswith("//")
        or ".." in decoded_path
        or "\\" in decoded_path
    ):
        raise WorkerError("output_invalid")
    try:
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        network_timeout = min(float(timeout_seconds), MAX_NETWORK_SECONDS)
        with httpx.Client(timeout=network_timeout, follow_redirects=False) as http:
            with http.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    raise WorkerError("output_invalid")
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) > MAX_OUTPUT_BYTES):
                    raise WorkerError("output_invalid")
                _write_bounded_mp4(destination, response.iter_bytes())
    except WorkerError:
        raise
    except Exception:
        raise WorkerError("unavailable") from None


def _write_bounded_mp4(destination: Path, chunks: Any) -> None:
    """symlink を拒否して新規 regular file にだけダウンロードする。"""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o600)
        total = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise WorkerError("output_invalid")
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        raise WorkerError("output_invalid")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        info = os.lstat(destination)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            raise WorkerError("output_invalid")
        with open(destination, "rb") as stream:
            header = stream.read(12)
        if total < 12 or header[4:8] != b"ftyp":
            raise WorkerError("output_invalid")
    except WorkerError:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkerError("output_invalid") from None


def _emit(value: dict[str, object]) -> None:
    # stdout は parent の parse-safe-response が受ける一行だけにする。
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
