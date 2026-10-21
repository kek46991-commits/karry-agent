"""FFmpeg と eSpeak に限定したローカル・プレビュー・レンダラー。

外部 API やシェル文字列は使用しない。AI 動画ファイルを受け取る場合にも、その
元音声は入力から一切 map せず、プロジェクト指定のローカル TTS（または無音）で
置き換える。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .errors import VideoError
from .storage import Storage


_VOICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+\-]{0,31}$")
_JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]")


class PreviewRenderer:
    """Generate a bounded H.264/AAC MP4 using only files on this host.

    The class returns only deliberately safe metadata.  Diagnostic stderr remains
    local to the subprocess and is never placed in a :class:`VideoError`.
    """

    def __init__(self, settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self._japanese_available: bool | None = None

    @staticmethod
    def _safe_error(code: str, message: str, status_code: int = 422) -> VideoError:
        return VideoError(code, message, status_code)

    def _binary(self, configured: str, label: str) -> str:
        if not isinstance(configured, str) or not configured:
            raise self._safe_error("media_tool_unavailable", f"{label} を利用できません。", 503)
        found = shutil.which(configured)
        if not found:
            raise self._safe_error("media_tool_unavailable", f"{label} を利用できません。", 503)
        return found

    def _run_check(self, binary: str, label: str, version_flag: str = "-version") -> None:
        try:
            completed = subprocess.run(
                [binary, version_flag],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self._safe_error("media_tool_unavailable", f"{label} を利用できません。", 503) from exc
        if completed.returncode != 0:
            raise self._safe_error("media_tool_unavailable", f"{label} を利用できません。", 503)

    def _check_tools(self, *, include_tts: bool = True) -> tuple[str, str, str | None]:
        ffmpeg = self._binary(self.settings.ffmpeg_bin, "FFmpeg")
        ffprobe = self._binary(self.settings.ffprobe_bin, "ffprobe")
        self._run_check(ffmpeg, "FFmpeg")
        self._run_check(ffprobe, "ffprobe")
        if not include_tts:
            return ffmpeg, ffprobe, None
        espeak = self._binary(self.settings.espeak_bin, "eSpeak")
        self._run_check(espeak, "eSpeak", "--version")
        return ffmpeg, ffprobe, espeak

    @staticmethod
    def _project_values(project: dict[str, Any]) -> tuple[str, str, float, dict[str, Any]]:
        if not isinstance(project, dict):
            raise VideoError("invalid_project", "プロジェクト設定が不正です。", 422)
        image_key = project.get("image_key")
        aspect_ratio = project.get("aspect_ratio")
        duration = project.get("duration_seconds")
        tts = project.get("tts", {})
        if not isinstance(image_key, str) or not image_key.startswith("images/") or not image_key.endswith(".png"):
            raise VideoError("invalid_project", "プロジェクト画像が不正です。", 422)
        if aspect_ratio not in {"16:9", "9:16"}:
            raise VideoError("invalid_project", "アスペクト比が不正です。", 422)
        if isinstance(duration, bool):
            raise VideoError("invalid_project", "動画秒数が不正です。", 422)
        try:
            seconds = float(duration)
        except (TypeError, ValueError) as exc:
            raise VideoError("invalid_project", "動画秒数が不正です。", 422) from exc
        if not math.isfinite(seconds) or not 1 <= seconds <= 120:
            raise VideoError("invalid_project", "動画秒数が不正です。", 422)
        if not isinstance(tts, dict):
            raise VideoError("invalid_project", "音声設定が不正です。", 422)
        text = tts.get("text", "")
        voice = tts.get("voice", "en")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(voice, str) or not _VOICE_RE.fullmatch(voice):
            raise VideoError("invalid_project", "音声設定が不正です。", 422)
        normalized = {"text": text, "voice": voice}
        for name, default, minimum, maximum in (
            ("rate", 170, 80, 300),
            ("pitch", 50, 0, 99),
            ("volume", 100, 0, 200),
        ):
            value = tts.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise VideoError("invalid_project", "音声設定が不正です。", 422)
            normalized[name] = value
        return image_key, aspect_ratio, seconds, normalized

    def _japanese_voice_available(self, espeak: str) -> bool:
        if self._japanese_available is not None:
            return self._japanese_available
        try:
            completed = subprocess.run(
                [espeak, "--voices=ja"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._japanese_available = False
            return False
        # eSpeak prints one data row per local voice.  A header, if present, is
        # explicitly ignored so a build with no Japanese voice is not accepted.
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self._japanese_available = completed.returncode == 0 and any(
            not line.lower().startswith(("pty", "language")) for line in lines
        )
        return self._japanese_available

    def _validate_tts_language(self, tts: dict[str, Any], espeak: str) -> None:
        requested_japanese = tts["voice"].lower().startswith("ja") or bool(_JAPANESE_RE.search(tts["text"]))
        if requested_japanese and not self._japanese_voice_available(espeak):
            # Never substitute English: the request must be changed or the host
            # must be provisioned with a Japanese local eSpeak voice.
            raise self._safe_error(
                "tts_japanese_unavailable",
                "この環境では日本語のローカル音声を利用できません。",
                422,
            )

    def _probe(self, source: Path, ffprobe: str) -> dict[str, Any]:
        if not source.is_file() or source.is_symlink():
            raise self._safe_error("media_not_found", "メディアを確認できません。", 422)
        try:
            completed = subprocess.run(
                [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self._safe_error("media_probe_failed", "メディアを確認できません。", 422) from exc
        if completed.returncode != 0:
            raise self._safe_error("media_probe_failed", "メディアを確認できません。", 422)
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise self._safe_error("media_probe_failed", "メディアを確認できません。", 422) from exc
        if not isinstance(result, dict):
            raise self._safe_error("media_probe_failed", "メディアを確認できません。", 422)
        return result

    @staticmethod
    def _duration(probe: dict[str, Any]) -> float:
        values: list[float] = []
        format_info = probe.get("format")
        if isinstance(format_info, dict):
            values.append(format_info.get("duration", 0))
        for stream in probe.get("streams", []):
            if isinstance(stream, dict):
                values.append(stream.get("duration", 0))
        parsed: list[float] = []
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                parsed.append(number)
        if not parsed:
            raise VideoError("media_probe_failed", "メディア時間を確認できません。", 422)
        return max(parsed)

    @staticmethod
    def _video_stream(probe: dict[str, Any]) -> dict[str, Any] | None:
        for stream in probe.get("streams", []):
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                return stream
        return None

    @staticmethod
    def _audio_stream(probe: dict[str, Any]) -> dict[str, Any] | None:
        for stream in probe.get("streams", []):
            if isinstance(stream, dict) and stream.get("codec_type") == "audio":
                return stream
        return None

    def _synthesize(self, tts: dict[str, Any], destination: Path, espeak: str, ffprobe: str) -> float | None:
        text = tts["text"]
        if not text.strip():
            return None
        try:
            completed = subprocess.run(
                [
                    espeak,
                    "--stdin",
                    "-v",
                    tts["voice"],
                    "-s",
                    str(tts["rate"]),
                    "-p",
                    str(tts["pitch"]),
                    "-a",
                    str(tts["volume"]),
                    "-w",
                    str(destination),
                ],
                # Input is delivered only via --stdin, so a spoken text that starts
                # with "--" can never be parsed as an eSpeak command-line option.
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self._safe_error("tts_failed", "音声を生成できませんでした。", 422) from exc
        if completed.returncode != 0 or not destination.is_file():
            raise self._safe_error("tts_failed", "音声を生成できませんでした。", 422)
        os.chmod(destination, 0o600)
        info = self._probe(destination, ffprobe)
        if self._audio_stream(info) is None:
            raise self._safe_error("tts_failed", "音声を生成できませんでした。", 422)
        return self._duration(info)

    @staticmethod
    def _assert_speech_fits(speech_duration: float | None, video_duration: float) -> None:
        if speech_duration is not None and speech_duration > video_duration + 0.001:
            raise VideoError(
                "speech_too_long",
                "音声が指定した動画時間を超えています。テキストを短くするか動画時間を延ばしてください。",
                422,
            )

    def validate(self, project: dict) -> None:
        """Perform local dependency/TTS preflight and refuse speech truncation.

        TTS is actually generated into a temporary private workspace and measured
        with ffprobe; character-count estimates are deliberately not used.
        """
        image_key, _ratio, duration, tts = self._project_values(project)
        self.storage.path(image_key)
        _ffmpeg, ffprobe, espeak = self._check_tools(include_tts=bool(tts["text"].strip()))
        if espeak is not None:
            self._validate_tts_language(tts, espeak)
        job_id = str(uuid.uuid4())
        workspace = self.storage.workspace(job_id)
        try:
            speech_duration = self._synthesize(tts, workspace / "validate.wav", espeak, ffprobe) if espeak is not None else None
            self._assert_speech_fits(speech_duration, duration)
        finally:
            self._cleanup_workspace(workspace)

    @staticmethod
    def _size_for(aspect_ratio: str, long_side: int) -> tuple[int, int]:
        if aspect_ratio == "16:9":
            return long_side, long_side * 9 // 16
        return long_side * 9 // 16, long_side

    def _validate_video_source(self, source: Path, ffprobe: str) -> Path:
        candidate = Path(source)
        if not candidate.is_file() or candidate.is_symlink() or candidate.stat().st_size <= 0:
            raise self._safe_error("invalid_video_source", "動画ソースを確認できません。", 422)
        if candidate.stat().st_size > self.settings.max_video_bytes:
            raise self._safe_error("invalid_video_source", "動画ソースが大きすぎます。", 422)
        info = self._probe(candidate, ffprobe)
        video = self._video_stream(info)
        if video is None:
            raise self._safe_error("invalid_video_source", "動画ソースを確認できません。", 422)
        try:
            width, height = int(video.get("width", 0)), int(video.get("height", 0))
        except (TypeError, ValueError) as exc:
            raise self._safe_error("invalid_video_source", "動画ソースを確認できません。", 422) from exc
        if width < 1 or height < 1 or width * height > 64_000_000:
            raise self._safe_error("invalid_video_source", "動画ソースを確認できません。", 422)
        self._duration(info)
        return candidate.resolve(strict=True)

    def _run_ffmpeg(self, command: list[str]) -> None:
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self._safe_error("render_failed", "動画を生成できませんでした。", 422) from exc
        if completed.returncode != 0:
            raise self._safe_error("render_failed", "動画を生成できませんでした。", 422)

    @staticmethod
    def _filter_for_video(width: int, height: int, fps: int, target: float) -> str:
        return (
            f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={target:.6f},"
            f"trim=duration={target:.6f},setpts=PTS-STARTPTS,format=yuv420p"
        )

    @staticmethod
    def _filter_for_still(width: int, height: int, fps: int, target: float) -> str:
        return (
            f"fps={fps},scale={width}:{height}:flags=lanczos,setsar=1,"
            f"trim=duration={target:.6f},setpts=PTS-STARTPTS,format=yuv420p"
        )

    def _verify_output(self, path: Path, ffprobe: str, expected_duration: float, width: int, height: int) -> dict[str, Any]:
        info = self._probe(path, ffprobe)
        video = self._video_stream(info)
        audio = self._audio_stream(info)
        if video is None or audio is None:
            raise self._safe_error("render_failed", "動画を検証できませんでした。", 422)
        try:
            valid_video = (
                video.get("codec_name") == "h264"
                and video.get("pix_fmt") == "yuv420p"
                and int(video.get("width", 0)) == width
                and int(video.get("height", 0)) == height
            )
            valid_audio = (
                audio.get("codec_name") == "aac"
                and int(audio.get("channels", 0)) == 2
                and int(audio.get("sample_rate", 0)) == 48000
            )
        except (TypeError, ValueError) as exc:
            raise self._safe_error("render_failed", "動画を検証できませんでした。", 422) from exc
        if not valid_video or not valid_audio:
            raise self._safe_error("render_failed", "動画を検証できませんでした。", 422)
        actual_duration = self._duration(info)
        if abs(actual_duration - expected_duration) > 1.0 / self.settings.fps + 0.02:
            raise self._safe_error("render_failed", "動画時間を検証できませんでした。", 422)
        return {
            "duration_seconds": actual_duration,
            "width": width,
            "height": height,
            "has_audio": True,
        }

    def _output_job_id(self, output_key: str) -> str:
        if not isinstance(output_key, str) or not output_key.startswith("outputs/") or not output_key.endswith(".mp4"):
            raise self._safe_error("invalid_output_key", "出力先の指定が不正です。", 422)
        stem = Path(output_key).stem
        try:
            job_id = str(uuid.UUID(stem))
        except (ValueError, TypeError, AttributeError) as exc:
            raise self._safe_error("invalid_output_key", "出力先の指定が不正です。", 422) from exc
        if output_key != self.storage.output_key(job_id):
            raise self._safe_error("invalid_output_key", "出力先の指定が不正です。", 422)
        return job_id

    @staticmethod
    def _cleanup_workspace(workspace: Path) -> None:
        # rmtree unlinks descendants rather than following their symlinks; refuse a
        # swapped workspace root outright as a further guard. Cleanup errors should
        # not hide an already generated client-safe VideoError.
        try:
            if workspace.exists() and not workspace.is_symlink():
                shutil.rmtree(workspace)
        except OSError:
            pass

    def render(self, project: dict, output_key: str, video_source: Path | None = None) -> dict:
        """Render one static image or local AI-video source to a final MP4.

        A short ``video_source`` is completed by freezing its final frame, never by
        looping it. Its original audio is deliberately ignored and replaced with the
        configured local TTS/silence audio stream.
        """
        image_key, aspect_ratio, requested_duration, tts = self._project_values(project)
        job_id = self._output_job_id(output_key)
        # Repeat preflight in this process. The actual WAV is generated once below
        # and measured before FFmpeg runs, avoiding a render that would truncate it.
        ffmpeg, ffprobe, espeak = self._check_tools(include_tts=bool(tts["text"].strip()))
        if espeak is not None:
            self._validate_tts_language(tts, espeak)
        width, height = self._size_for(aspect_ratio, self.settings.preview_long_side)
        frame_count = max(1, math.ceil(requested_duration * self.settings.fps - 1e-9))
        target_duration = frame_count / self.settings.fps
        workspace = self.storage.workspace(job_id)
        try:
            prepared = self.storage.prepare_image(image_key, aspect_ratio, workspace / "prepared.png")
            speech_path = workspace / "speech.wav"
            speech_duration = self._synthesize(tts, speech_path, espeak, ffprobe) if espeak is not None else None
            self._assert_speech_fits(speech_duration, requested_duration)

            source_is_video = video_source is not None
            if source_is_video:
                visual = self._validate_video_source(Path(video_source), ffprobe)
            else:
                visual = prepared

            temporary = workspace / "output.tmp.mp4"
            command: list[str] = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-threads", "2", "-filter_threads", "2", "-filter_complex_threads", "2",
            ]
            if source_is_video:
                command += ["-i", str(visual)]
                visual_filter = self._filter_for_video(width, height, self.settings.fps, target_duration)
            else:
                command += ["-loop", "1", "-framerate", str(self.settings.fps), "-i", str(visual)]
                visual_filter = self._filter_for_still(width, height, self.settings.fps, target_duration)
            if speech_duration is None:
                command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
                audio_filter = f"[1:a]atrim=duration={target_duration:.6f},asetpts=N/SR/TB[a]"
            else:
                command += ["-i", str(speech_path)]
                audio_filter = (
                    f"[1:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=N/SR/TB,"
                    f"apad=whole_dur={target_duration:.6f},atrim=duration={target_duration:.6f}[a]"
                )
            command += [
                "-filter_complex", f"[0:v]{visual_filter}[v];{audio_filter}",
                "-map", "[v]", "-map", "[a]",
                "-t", f"{target_duration:.6f}",
                "-frames:v", str(frame_count),
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-r", str(self.settings.fps),
                "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
                "-movflags", "+faststart", str(temporary),
            ]
            self._run_ffmpeg(command)
            if not temporary.is_file():
                raise self._safe_error("render_failed", "動画を生成できませんでした。", 422)
            os.chmod(temporary, 0o600)
            metadata = self._verify_output(temporary, ffprobe, target_duration, width, height)

            output = self.storage.output_path(output_key)
            if output.is_symlink():
                raise self._safe_error("unsafe_storage_path", "安全でない保存先です。", 422)
            # Same filesystem (storage work/output) gives an atomic publish. The
            # target had no client-controlled filename and is mode 0600 before/after.
            os.replace(temporary, output)
            os.chmod(output, 0o600)
            if source_is_video:
                metadata["source_audio_replaced"] = True
            return metadata
        finally:
            self._cleanup_workspace(workspace)


__all__ = ["PreviewRenderer"]
