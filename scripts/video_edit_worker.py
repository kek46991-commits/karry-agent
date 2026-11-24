#!/usr/bin/env python3
"""GitHub Actions worker for long interview video edits.

Claims queued jobs from ``/api/video/edits/worker/claim`` and runs the heavy
ffmpeg work here instead of on the small Render instance:

* analyze: probe the source, extract mono audio in 10-minute chunks and post
  each chunk for transcription (Gemini runs server-side), then request a cut plan.
* render: cut the kept ranges with burned-in subtitles/telops (ASS), concatenate
  and upload the MP4 to the presigned R2 URL.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK_SECONDS = 600
FONT = os.getenv("KARRY_VIDEO_EDIT_FONT", "Noto Sans CJK JP")
MAX_JOBS = 5


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, payload=None, data: bytes | None = None, content_type: str = "application/json",
             timeout: int = 120):
        body = json.dumps(payload).encode() if payload is not None else data
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        for attempt in range(4):
            request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read() or b"null")
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", "replace")
                if exc.code < 500 and exc.code != 429 or attempt == 3:
                    raise RuntimeError(f"{method} {path.split('?')[0]} -> HTTP {exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 3:
                    raise RuntimeError(f"{method} {path.split('?')[0]} failed: {type(exc).__name__}") from None
            time.sleep(10 * (attempt + 1))
        raise AssertionError("unreachable")


def run(*args: str) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode:
        tail = result.stderr.strip().splitlines()[-5:]
        raise RuntimeError(f"{args[0]} failed: " + " | ".join(tail))
    return result.stdout


def download(url: str, target: Path) -> None:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as out:
                shutil.copyfileobj(response, out, 8 * 1024 * 1024)
            return
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(15)


def upload(url: str, source: Path) -> None:
    for attempt in range(3):
        try:
            with source.open("rb") as body:
                request = urllib.request.Request(url, data=body, method="PUT",
                                                 headers={"Content-Type": "video/mp4", "Content-Length": str(source.stat().st_size)})
                with urllib.request.urlopen(request, timeout=1800) as response:
                    if response.status in {200, 201}:
                        return
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(15)
    raise RuntimeError("upload failed")


def probe(path: Path) -> dict:
    info = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)))
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("映像トラックがありません。")
    width, height = int(video["width"]), int(video["height"])
    rotation = abs(int(float(video.get("tags", {}).get("rotate", 0) or 0)))
    for side in video.get("side_data_list", []):
        rotation = abs(int(float(side.get("rotation", rotation) or 0)))
    if rotation % 180 == 90:
        width, height = height, width
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    num, _, den = rate.partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return {"duration": float(info["format"]["duration"]), "width": width, "height": height,
            "has_audio": any(s.get("codec_type") == "audio" for s in info["streams"]),
            "fps": fps if 10 <= fps <= 120 else 30.0}


def analyze(api: Api, job: dict, work: Path) -> None:
    source = work / "source"
    download(job["source_url"], source)
    meta = probe(source)
    api.call("POST", f"/api/video/edits/worker/{job['job_id']}/probe",
             {k: meta[k] for k in ("duration", "width", "height", "has_audio")})
    chunks = math.ceil(meta["duration"] / CHUNK_SECONDS)
    for index in range(chunks):
        offset = index * CHUNK_SECONDS
        seconds = min(CHUNK_SECONDS, meta["duration"] - offset)
        audio = work / f"audio{index}.mp3"
        run("ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(offset), "-t", str(seconds), "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio))
        print(f"transcribe {index + 1}/{chunks}", flush=True)
        api.call("POST", f"/api/video/edits/worker/{job['job_id']}/transcribe?index={index}&offset={offset}&seconds={seconds:.2f}",
                 data=audio.read_bytes(), content_type="audio/mpeg", timeout=900)
    print("plan", flush=True)
    api.call("POST", f"/api/video/edits/worker/{job['job_id']}/plan", {}, timeout=900)


def even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def output_size(width: int, height: int, limit: int = 1920) -> tuple[int, int]:
    scale = min(1.0, limit / max(width, height))
    return even(width * scale), even(height * scale)


def wrap(text: str, per_line: int) -> str:
    text = text.replace("\\", "＼").replace("{", "（").replace("}", "）").replace("\n", " ")
    if len(text) <= per_line:
        return text
    lines = math.ceil(len(text) / per_line)
    size = math.ceil(len(text) / lines)
    return r"\N".join(text[i:i + size] for i in range(0, len(text), size))


def stamp(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_document(cues: list[dict], width: int, height: int) -> str:
    short = min(width, height)
    sub_size, telop_size = round(short * 0.058), round(short * 0.05)
    margin = round(short * 0.04)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT},{sub_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{max(2, round(sub_size * 0.09))},1,2,{margin},{margin},{margin},1
Style: Telop,{FONT},{telop_size},&H0000E6FF,&H0000E6FF,&H00201810,&HC0201810,1,0,0,0,100,100,0,0,3,{max(4, round(telop_size * 0.25))},0,7,{margin},{margin},{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    sub_chars = max(8, int(width * 0.9 / sub_size))
    telop_chars = max(6, int(width * 0.6 / telop_size))
    events = []
    for cue in cues:
        telop = cue["kind"] == "telop"
        text = wrap(cue["text"], telop_chars if telop else sub_chars)
        fade = r"{\fad(250,250)}" if telop else ""
        events.append(f"Dialogue: {1 if telop else 0},{stamp(cue['start'])},{stamp(cue['end'])},{'Telop' if telop else 'Sub'},,0,0,0,,{fade}{text}")
    return header + "\n".join(events) + "\n"


def range_cues(cues: list[dict], start: float, end: float) -> list[dict]:
    """Cues overlapping [start, end) in output time, shifted so the range starts at 0."""
    return [c | {"start": max(0.0, c["start"] - start), "end": min(end, c["end"]) - start}
            for c in cues if c["end"] > start and c["start"] < end and min(end, c["end"]) - max(start, c["start"]) > 0.05]


def render(api: Api, job: dict, work: Path) -> float:
    source = work / "source"
    download(job["source_url"], source)
    meta = probe(source)
    width, height = output_size(meta["width"], meta["height"])
    fps = meta["fps"]
    parts, total = [], 0.0
    for i, r in enumerate(job["ranges"]):
        frames = max(1, round((r["end"] - r["start"]) * fps))
        length = frames / fps
        out_start = total
        cues = range_cues(job["cues"], out_start, out_start + length)
        subs = work / f"r{i}.ass"
        subs.write_text(ass_document(cues, width, height), encoding="utf-8")
        part = work / f"r{i}.mp4"
        vf = f"scale={width}:{height}:flags=lanczos,setsar=1,fps={fps:.6f},ass={subs.name}"
        audio = ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                 "-af", f"aresample=async=1,apad,atrim=0:{length:.6f}"] if meta["has_audio"] else ["-an"]
        run("ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{r['start']:.3f}", "-i", str(source), "-map", "0:v:0",
            "-vf", vf, "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", *audio, "-video_track_timescale", "90000", str(part))
        parts.append(part)
        total += length
        print(f"render {i + 1}/{len(job['ranges'])} ({total / 60:.1f} min)", flush=True)
    listing = work / "parts.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
    output = work / "output.mp4"
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart", str(output))
    for part in parts:
        part.unlink()
    upload(job["output_url"], output)
    return probe(output)["duration"]


def main() -> int:
    base = os.getenv("KARRY_URL", "https://karry-agent-1.onrender.com")
    token = os.getenv("KARRY_API_TOKEN", "").strip()
    if not token:
        print("KARRY_API_TOKEN is not set", file=sys.stderr)
        return 1
    api = Api(base, token)
    target = os.getenv("JOB_ID", "").strip() or None
    for _ in range(MAX_JOBS):
        claimed = api.call("POST", "/api/video/edits/worker/claim", {"job_id": target} if target else {})["job"]
        target = None
        if not claimed:
            print("no pending video edit job")
            return 0
        print(f"job {claimed['job_id'][:8]}: {claimed['stage']}", flush=True)
        with tempfile.TemporaryDirectory(prefix="video-edit-") as tmp:
            work = Path(tmp)
            os.chdir(work)
            try:
                if claimed["stage"] == "analyze":
                    analyze(api, claimed, work)
                else:
                    duration = render(api, claimed, work)
                    api.call("POST", f"/api/video/edits/worker/{claimed['job_id']}/rendered", {"duration": duration})
            except Exception as exc:  # noqa: BLE001 - report and move to the next job
                print(f"job failed: {exc}", file=sys.stderr)
                try:
                    api.call("POST", f"/api/video/edits/worker/{claimed['job_id']}/failed", {"message": str(exc)[:300]})
                except Exception as report:  # noqa: BLE001
                    print(f"could not report failure: {report}", file=sys.stderr)
            finally:
                os.chdir("/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
