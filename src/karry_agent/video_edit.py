"""Long interview video editing: R2 multipart upload, Gemini transcript/cut plan, GitHub Actions rendering.

The 512 MB Render instance only proxies 8 MiB upload parts and keeps job state in R2.
ffmpeg work (audio extraction, cut + caption burn-in) runs in ``.github/workflows/video-edit.yml``
through ``scripts/video_edit_worker.py``; Gemini calls stay on the server so the key never leaves it.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from functools import lru_cache
from typing import Any

from .website_storage import StorageError, load_storage_config

logger = logging.getLogger(__name__)

PART_SIZE = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 5 * 1024 * 1024 * 1024
MAX_AUDIO_BYTES = PART_SIZE
MAX_SEGMENTS = 3000
MIN_TARGET_MINUTES, MAX_TARGET_MINUTES = 1, 180
JOB_ID_RE = re.compile(r"[a-f0-9]{32}")
EXTENSIONS = {"mp4", "mov", "m4v", "webm", "mkv", "avi", "3gp"}
ACTIVE = {"queued_analyze", "analyzing", "queued_render", "rendering"}
LEASE_SECONDS = {"analyze": 90 * 60, "render": 170 * 60}
MAX_ATTEMPTS = 3
STATUS_LABELS = {
    "uploading": "アップロード中", "queued_analyze": "解析待ち", "analyzing": "文字起こし・編集プラン作成中",
    "review": "編集プラン確認待ち", "queued_render": "書き出し待ち", "rendering": "書き出し中",
    "done": "完成", "failed": "失敗",
}


class EditError(ValueError):
    def __init__(self, message: str, status_code: int = 422, code: str = "invalid_request"):
        super().__init__(message)
        self.message, self.status_code, self.code = message, status_code, code


def _now() -> float:
    return time.time()


def max_total_bytes() -> int:
    try:
        return max(PART_SIZE, int(os.getenv("KARRY_VIDEO_EDIT_MAX_TOTAL_BYTES", str(9 * 10**9))))
    except ValueError:
        return 9 * 10**9


@lru_cache(maxsize=4)
def _s3(config):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=config.endpoint, region_name=config.region,
                        aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
                        aws_session_token=config.session_token,
                        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=60,
                                      retries={"mode": "standard", "total_max_attempts": 3},
                                      s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
                                      response_checksum_validation="when_required"))


def _storage_failure() -> EditError:
    return EditError("動画の保存先（R2）に接続できませんでした。時間をおいて再試行してください。", 503, "storage_unavailable")


class EditStore:
    """Job JSON and media objects under ``KARRY_VIDEO_EDIT_PREFIX`` (default ``karry-video-edits/``)."""

    _lock = threading.RLock()

    def __init__(self, client: Any = None, bucket: str | None = None, prefix: str | None = None):
        if client is None:
            try:
                config = load_storage_config()
            except StorageError as exc:
                raise EditError("動画の保存先（R2）が設定されていません。", 503, "storage_configuration") from exc
            prefix = prefix or os.getenv("KARRY_VIDEO_EDIT_PREFIX", "karry-video-edits/").strip().rstrip("/") + "/"
            config = dataclasses.replace(config, prefix=prefix)
            client, bucket = _s3(config), config.bucket
        self.client, self.bucket = client, bucket
        self.prefix = prefix or "karry-video-edits/"

    def key(self, job_id: str, name: str) -> str:
        if not JOB_ID_RE.fullmatch(job_id):
            raise EditError("動画編集ジョブが見つかりません。", 404, "not_found")
        return f"{self.prefix}{job_id}/{name}"

    def _call(self, method: str, **kwargs):
        try:
            return getattr(self.client, method)(Bucket=self.bucket, **kwargs)
        except EditError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider text can contain request details
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) if hasattr(exc, "response") else ""
            if code in {"NoSuchKey", "404", "NotFound", "NoSuchUpload"}:
                raise EditError("動画編集ジョブが見つかりません。", 404, "not_found") from None
            logger.warning("video edit storage %s failed: %s %s", method, type(exc).__name__, code)
            raise _storage_failure() from None

    def load(self, job_id: str) -> dict[str, Any]:
        response = self._call("get_object", Key=self.key(job_id, "job.json"))
        try:
            return json.loads(response["Body"].read())
        finally:
            response["Body"].close()

    def save(self, job: dict[str, Any]) -> dict[str, Any]:
        job["updated_at"] = _now()
        body = json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode()
        self._call("put_object", Key=self.key(job["id"], "job.json"), Body=body,
                   ContentType="application/json", CacheControl="no-store")
        return job

    def _objects(self) -> list[dict[str, Any]]:
        objects, token = [], None
        while True:
            kwargs = {"Prefix": self.prefix, "MaxKeys": 1000} | ({"ContinuationToken": token} if token else {})
            page = self._call("list_objects_v2", **kwargs)
            objects += page.get("Contents", [])
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return objects

    def jobs(self) -> list[dict[str, Any]]:
        ids = [o["Key"][len(self.prefix):-len("/job.json")] for o in self._objects() if o["Key"].endswith("/job.json")]
        jobs = []
        for job_id in ids:
            try:
                jobs.append(self.load(job_id))
            except EditError as exc:
                if exc.code != "not_found":
                    raise
        return sorted(jobs, key=lambda j: j.get("created_at", 0), reverse=True)

    def usage_bytes(self) -> int:
        stored = sum(int(o.get("Size", 0)) for o in self._objects())
        pending = sum(int(j.get("size", 0)) for j in self.jobs() if j.get("status") == "uploading")
        return stored + pending

    # Upload -----------------------------------------------------------------
    def start_upload(self, filename: str, size: int, content_type: str, target_minutes: int) -> dict[str, Any]:
        if not 0 < size <= MAX_SOURCE_BYTES:
            raise EditError("動画は5 GBまでです。", 413, "video_too_large")
        if not MIN_TARGET_MINUTES <= target_minutes <= MAX_TARGET_MINUTES:
            raise EditError("仕上がりの長さは1〜180分で指定してください。", 422, "invalid_target")
        if content_type and not content_type.startswith("video/") and content_type != "application/octet-stream":
            raise EditError("動画ファイルを選んでください。", 415, "unsupported_type")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp4"
        ext = ext if ext in EXTENSIONS else "mp4"
        with self._lock:
            if self.usage_bytes() + size > max_total_bytes():
                raise EditError("保存容量の上限に近いので、古い動画編集を削除してからアップロードしてください。", 507, "storage_capacity")
            job_id = uuid.uuid4().hex
            source = f"source.{ext}"
            upload = self._call("create_multipart_upload", Key=self.key(job_id, source),
                                ContentType=content_type if content_type.startswith("video/") else "video/mp4")
            job = {"id": job_id, "filename": re.sub(r"[\x00-\x1f]", "", filename)[:200] or "video", "size": size,
                   "source": source, "upload_id": upload["UploadId"], "parts_total": -(-size // PART_SIZE),
                   "target_seconds": target_minutes * 60, "status": "uploading", "created_at": _now(),
                   "burn_subtitles": True, "segments": [], "chunks": {}, "attempts": 0}
            return self.save(job)

    def upload_part(self, job: dict[str, Any], number: int, body: bytes) -> None:
        if job["status"] != "uploading":
            raise EditError("このジョブはアップロード済みです。", 409, "not_uploading")
        total = job["parts_total"]
        expected = PART_SIZE if number < total else job["size"] - PART_SIZE * (total - 1)
        if not 1 <= number <= total or len(body) != expected:
            raise EditError("アップロードの分割サイズが一致しません。", 422, "invalid_part")
        self._call("upload_part", Key=self.key(job["id"], job["source"]), UploadId=job["upload_id"],
                   PartNumber=number, Body=body)

    def uploaded_parts(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        parts, marker = [], 0
        while True:
            page = self._call("list_parts", Key=self.key(job["id"], job["source"]), UploadId=job["upload_id"],
                              PartNumberMarker=marker, MaxParts=1000)
            parts += [{"PartNumber": p["PartNumber"], "ETag": p["ETag"], "Size": p.get("Size")} for p in page.get("Parts", [])]
            if not page.get("IsTruncated"):
                return parts
            marker = page.get("NextPartNumberMarker", parts[-1]["PartNumber"] if parts else 0)

    def complete_upload(self, job: dict[str, Any]) -> dict[str, Any]:
        if job["status"] != "uploading":
            return job
        parts = self.uploaded_parts(job)
        if sorted(p["PartNumber"] for p in parts) != list(range(1, job["parts_total"] + 1)):
            raise EditError("まだ全部アップロードされていません。", 409, "upload_incomplete")
        key = self.key(job["id"], job["source"])
        self._call("complete_multipart_upload", Key=key, UploadId=job["upload_id"],
                   MultipartUpload={"Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in sorted(parts, key=lambda p: p["PartNumber"])]})
        head = self._call("head_object", Key=key)
        if int(head.get("ContentLength", -1)) != job["size"]:
            raise EditError("アップロードした動画のサイズ検証に失敗しました。", 502, "upload_integrity")
        job.pop("upload_id", None)
        job["status"] = "queued_analyze"
        return self.save(job)

    def presign(self, job: dict[str, Any], name: str, method: str = "get_object", expires: int = 3 * 3600, **params) -> str:
        try:
            return self.client.generate_presigned_url(method, Params={"Bucket": self.bucket, "Key": self.key(job["id"], name), **params},
                                                      ExpiresIn=expires)
        except Exception:  # noqa: BLE001
            raise _storage_failure() from None

    def exists(self, job: dict[str, Any], name: str) -> int | None:
        try:
            return int(self._call("head_object", Key=self.key(job["id"], name)).get("ContentLength", 0))
        except EditError as exc:
            if exc.code == "not_found":
                return None
            raise

    def delete(self, job: dict[str, Any]) -> None:
        if job.get("upload_id"):
            try:
                self._call("abort_multipart_upload", Key=self.key(job["id"], job["source"]), UploadId=job["upload_id"])
            except EditError:
                pass
        for obj in self._objects():
            if obj["Key"].startswith(f"{self.prefix}{job['id']}/"):
                self._call("delete_object", Key=obj["Key"])

    # Worker lease -------------------------------------------------------------
    def claim(self, job_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            candidates = [self.load(job_id)] if job_id else list(reversed(self.jobs()))
            now = _now()
            for job in candidates:
                status = job.get("status")
                stage = {"queued_analyze": "analyze", "queued_render": "render"}.get(status)
                if stage is None and status in {"analyzing", "rendering"} and job.get("lease_until", 0) < now:
                    stage = "analyze" if status == "analyzing" else "render"
                if stage is None:
                    continue
                if job.get("attempts", 0) >= MAX_ATTEMPTS:
                    job |= {"status": "failed", "error": "処理が何度も中断されました。もう一度お試しください。", "failed_stage": stage}
                    self.save(job)
                    continue
                job |= {"status": "analyzing" if stage == "analyze" else "rendering", "lease_until": now + LEASE_SECONDS[stage],
                        "attempts": job.get("attempts", 0) + 1, "error": None}
                return self.save(job) | {"_stage": stage}
            return None


def pending_count(store: EditStore) -> int:
    now = _now()
    return sum(1 for j in store.jobs() if j.get("status") in {"queued_analyze", "queued_render"}
               or (j.get("status") in {"analyzing", "rendering"} and j.get("lease_until", 0) < now))


def require_lease(job: dict[str, Any], status: str) -> None:
    if job.get("status") != status:
        raise EditError("このジョブは処理中ではありません。", 409, "not_leased")


# Public views -----------------------------------------------------------------
def kept_seconds(segments: list[dict[str, Any]]) -> float:
    return round(sum(s["end"] - s["start"] for s in segments if s.get("keep")), 1)


def public_job(job: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    keys = ("id", "filename", "size", "parts_total", "target_seconds", "status", "created_at", "updated_at", "title",
            "error", "failed_stage", "duration", "output_size", "output_duration", "burn_subtitles", "dispatch", "note")
    view = {k: job.get(k) for k in keys if job.get(k) is not None}
    view["status_label"] = STATUS_LABELS.get(job.get("status"), job.get("status"))
    view["part_size"] = PART_SIZE
    segments = job.get("segments") or []
    view["segment_count"] = len(segments)
    view["kept_seconds"] = kept_seconds(segments)
    view["telop_count"] = sum(1 for s in segments if s.get("telop"))
    if detail:
        view["segments"] = [{k: s.get(k) for k in ("id", "start", "end", "speaker", "text", "keep", "telop")} for s in segments]
    return view


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).replace("{", "（").replace("}", "）").strip()[:limit]


def apply_edits(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if job.get("status") not in {"review", "done", "failed"} or not job.get("segments"):
        raise EditError("編集プランができてから編集してください。", 409, "not_editable")
    by_id = {s["id"]: s for s in job["segments"]}
    for change in payload.get("segments") or []:
        segment = by_id.get(change.get("id")) if isinstance(change, dict) else None
        if segment is None:
            continue
        if isinstance(change.get("keep"), bool):
            segment["keep"] = change["keep"]
        if "text" in change:
            segment["text"] = _clean_text(change["text"], 200)
        if "telop" in change:
            segment["telop"] = _clean_text(change["telop"], 40)
    if isinstance(payload.get("burn_subtitles"), bool):
        job["burn_subtitles"] = payload["burn_subtitles"]
    if "title" in payload:
        job["title"] = _clean_text(payload["title"], 80)
    if not any(s.get("keep") for s in job["segments"]):
        raise EditError("残す場面を1つ以上選んでください。", 422, "nothing_kept")
    return job


# Render plan --------------------------------------------------------------------
def render_plan(job: dict[str, Any], gap: float = 1.5, pad: float = 0.2) -> dict[str, Any]:
    """Cut ranges in source time and caption cues in output time."""
    duration = float(job.get("duration") or 0) or None
    kept = [s for s in sorted(job.get("segments") or [], key=lambda s: s["start"]) if s.get("keep")]
    if not kept:
        raise EditError("残す場面がありません。", 422, "nothing_kept")
    ranges: list[dict[str, Any]] = []
    for seg in kept:
        start, end = max(0.0, seg["start"] - pad), seg["end"] + pad
        if duration:
            end = min(end, duration)
        last = ranges[-1] if ranges else None
        if last and (start <= last["end"] or (seg["id"] == last["last_id"] + 1 and start - last["end"] <= gap)):
            last["end"], last["last_id"] = max(last["end"], end), seg["id"]
            last["segments"].append(seg)
        else:
            ranges.append({"start": start, "end": end, "last_id": seg["id"], "segments": [seg]})
    cues, out, telops = [], 0.0, []
    for r in ranges:
        r["out_start"] = out
        for seg in r["segments"]:
            a = round(out + max(0.0, seg["start"] - r["start"]), 3)
            b = round(min(out + (seg["end"] - r["start"]), out + (r["end"] - r["start"])), 3)
            if job.get("burn_subtitles", True) and seg.get("text") and b > a:
                cues.append({"start": a, "end": b, "text": seg["text"], "kind": "sub"})
            if seg.get("telop"):
                telops.append({"start": a, "text": seg["telop"]})
        out += r["end"] - r["start"]
    for i, telop in enumerate(telops):
        limit = telops[i + 1]["start"] if i + 1 < len(telops) else out
        end = min(telop["start"] + 6.0, limit, out)
        if end > telop["start"]:
            cues.append({"start": telop["start"], "end": round(end, 3), "text": telop["text"], "kind": "telop"})
    return {"ranges": [{"start": round(r["start"], 3), "end": round(r["end"], 3), "out_start": round(r["out_start"], 3)} for r in ranges],
            "cues": sorted(cues, key=lambda c: (c["start"], c["kind"])), "duration": round(out, 3)}


# Gemini --------------------------------------------------------------------------
def _gemini():
    from google import genai
    from google.genai import types
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key in {"your-gemini-api-key", "YOUR_GEMINI_API_KEY"}:
        raise EditError("GEMINI_API_KEY が設定されていません。", 503, "gemini_unavailable")
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=300_000)), types


def _model() -> str:
    return os.getenv("KARRY_VIDEO_EDIT_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-3.5-flash"


def _generate_json(contents: list[Any], max_tokens: int = 32768) -> Any:
    client, types = _gemini()  # keep a reference: a collected Client closes its HTTP session mid-request
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=_model(), contents=contents,
                config=types.GenerateContentConfig(temperature=0.2, response_mime_type="application/json", max_output_tokens=max_tokens,
                                                   automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
            return json.loads(response.text)
        except Exception as exc:  # noqa: BLE001 - retried, then reported without provider text
            last = exc
            logger.warning("video edit Gemini call failed: %s %s", type(exc).__name__, getattr(exc, "code", ""))
            time.sleep(2 * (attempt + 1))
    raise EditError("Gemini での処理に失敗しました。時間をおいて再試行してください。", 502, "gemini_failed") from last


TRANSCRIBE_PROMPT = """これはインタビュー動画の音声の一部（約{seconds}秒）です。日本語で文字起こしして、字幕に使える形に区切ってください。
- 1〜2文ごと、長くても15秒程度で区切る
- 「えー」「あの」などのフィラーや言い直しは除き、読みやすい字幕文に整える（意味は変えない、1つ40文字程度まで）
- 話者が変わったら区切り、話者を "A" "B" のように付ける
- 時刻はこの音声の先頭を0秒とした秒数（小数可）
JSON配列だけを返す: [{{"start": 0.0, "end": 4.2, "speaker": "A", "text": "..."}}]"""


def transcribe_chunk(audio: bytes, mime_type: str, offset: float, seconds: float) -> list[dict[str, Any]]:
    _, types = _gemini()
    raw = _generate_json([types.Part.from_bytes(data=audio, mime_type=mime_type),
                          TRANSCRIBE_PROMPT.format(seconds=int(seconds))], 65536)
    return normalize_transcript(raw, offset, seconds)


def normalize_transcript(raw: Any, offset: float, seconds: float) -> list[dict[str, Any]]:
    items = raw.get("segments") if isinstance(raw, dict) else raw
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            start, end = float(item.get("start")), float(item.get("end"))
        except (TypeError, ValueError):
            continue
        text = _clean_text(item.get("text"), 200)
        if not text or start < 0 or start > seconds + 5:
            continue
        end = min(max(end, start + 0.5), seconds + 5, start + 30)
        result.append({"start": round(offset + start, 2), "end": round(offset + end, 2),
                       "speaker": _clean_text(item.get("speaker"), 10) or None, "text": text})
    return sorted(result, key=lambda s: s["start"])


def merge_chunks(chunks: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for index in sorted(chunks, key=int):
        for seg in chunks[index]:
            if segments and seg["start"] < segments[-1]["end"]:
                if seg["end"] <= segments[-1]["end"]:
                    continue
                seg = seg | {"start": segments[-1]["end"]}
            segments.append(seg)
    return [s | {"id": i, "keep": True, "telop": ""} for i, s in enumerate(segments[:MAX_SEGMENTS])]


PLAN_PROMPT = """あなたはプロの動画編集者です。下はインタビュー動画（全{total}分）の発話リストです（id|開始|秒数|話者|内容）。
これを約{target}分（合計 {target_s} 秒 ±5%）の見やすい動画に編集します。
- 話の流れが自然につながるように残す発話の id を選ぶ（重要な発言・具体的なエピソード・面白い部分を優先し、重複・脱線・前置き・言い直しはカット）
- 質問と答えはセットで残す
- 話題の変わり目に見出しテロップを付ける（20文字以内、全体で{telops}個程度、残す発話の id に付ける）
- 動画のタイトルも付ける（30文字以内）
{feedback}JSONだけを返す: {{"title": "...", "keep": [0, 1, 5], "telops": [{{"id": 0, "text": "..."}}]}}

{lines}"""


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def plan_cut(segments: list[dict[str, Any]], target_seconds: int) -> dict[str, Any]:
    """Ask Gemini for kept ids + telops; retry once when the total misses the target; trim as a last resort."""
    total = segments[-1]["end"] if segments else 0
    if not segments:
        raise EditError("音声から発話を検出できませんでした。", 422, "no_speech")
    lines = "\n".join(f"{s['id']}|{_clock(s['start'])}|{s['end'] - s['start']:.0f}|{s.get('speaker') or '-'}|{s['text']}" for s in segments)
    lengths = {s["id"]: s["end"] - s["start"] for s in segments}
    feedback, plan = "", None
    for _ in range(2):
        raw = _generate_json([PLAN_PROMPT.format(total=round(total / 60), target=round(target_seconds / 60), target_s=target_seconds,
                                                 telops=max(3, round(target_seconds / 180)), feedback=feedback, lines=lines)], 16384)
        if not isinstance(raw, dict):
            continue
        keep = sorted({i for i in raw.get("keep") or [] if isinstance(i, int) and i in lengths})
        telops = {t["id"]: _clean_text(t.get("text"), 40) for t in raw.get("telops") or []
                  if isinstance(t, dict) and isinstance(t.get("id"), int) and t["id"] in lengths}
        plan = {"title": _clean_text(raw.get("title"), 80), "keep": keep, "telops": telops}
        kept = sum(lengths[i] for i in keep)
        if target_seconds * 0.85 <= kept <= target_seconds * 1.08 or total <= target_seconds:
            break
        feedback = (f"前回の案は合計 {kept / 60:.1f} 分でした。必ず約{round(target_seconds / 60)}分になるよう"
                    f"{'減らして' if kept > target_seconds else '増やして'}ください。\n")
    if plan is None or not plan["keep"]:
        raise EditError("編集プランを作れませんでした。再試行してください。", 502, "plan_failed")
    keep = plan["keep"]
    while sum(lengths[i] for i in keep) > target_seconds * 1.08 and len(keep) > 1:
        # Drop untitled segments evenly across the video until the length fits.
        droppable = [i for i in keep if i not in plan["telops"]] or keep[1:]
        keep = [i for i in keep if i != droppable[len(droppable) // 2]]
    plan["keep"] = keep
    return plan


def apply_plan(job: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    keep = set(plan["keep"])
    for seg in job["segments"]:
        seg["keep"] = seg["id"] in keep
        seg["telop"] = plan["telops"].get(seg["id"], "") if seg["keep"] else ""
    job["title"] = plan.get("title") or job.get("title") or job["filename"]
    return job


# GitHub Actions dispatch -----------------------------------------------------------
def dispatch_worker(job_id: str) -> str:
    token = (os.getenv("KARRY_GITHUB_TOKEN") or os.getenv("KARRY_GIT_GITHUB_TOKEN") or "").strip()
    repo = os.getenv("KARRY_GITHUB_REPO", "kek46991-commits/karry-agent").strip()
    workflow = os.getenv("KARRY_VIDEO_EDIT_WORKFLOW", "video-edit.yml").strip()
    ref = os.getenv("KARRY_VIDEO_EDIT_WORKFLOW_REF", "main").strip()
    if not token:
        return "skipped: no GitHub token"
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[\w.-]+\.ya?ml", workflow) or not re.fullmatch(r"[\w./-]+", ref):
        return "skipped: invalid workflow settings"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
        data=json.dumps({"ref": ref, "inputs": {"job_id": job_id}}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return f"dispatched: HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"failed: HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {type(exc).__name__}"
