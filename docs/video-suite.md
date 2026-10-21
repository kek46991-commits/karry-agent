# Creative Video Suite

Owner-authenticated image/text → video generation with character narration.

## Flow

```
UI (static/index.html)  ── multipart POST /api/video/jobs ──▶  video_suite_api.build_router
  photo · 16:9|9:16 · 1–120 s · narration                       │ validate (aspect, duration, voice, 4 MiB image)
                                                                │ plan_segments → N clips (≤10 s each)
                                                                │ VideoAdapter.submit × N  (Replicate Kling | Runway)
                                                                │ GeminiNarrator.synthesize → narration.wav
  poll GET /api/video/jobs/{id} every 6 s  ◀────────────────────┤ refresh: poll provider, download clip → R2/S3
  ▶ play clips in order, trim last clip, play narration         └ GET /clips/{n}, /narration (private, no-store)
```

## Endpoints (all require `Authorization: Bearer $KARRY_API_TOKEN`)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/video/config` | enabled flag, provider, clip lengths, voices (no secrets) |
| POST | `/api/video/jobs` | form: `session_id`, `prompt`, `aspect_ratio`, `duration_seconds`, `narration_text`, `voice`, `request_id`, `image` |
| GET | `/api/video/jobs?session_id=` | latest 20 jobs of the task |
| GET | `/api/video/jobs/{id}?session_id=` | refreshes provider status |
| GET | `/api/video/jobs/{id}/clips/{n}?session_id=` | `video/mp4` |
| GET | `/api/video/jobs/{id}/narration?session_id=` | `audio/wav` |

## Duration (1–120 s)

Providers only render short clips (Kling 5/10 s, Runway gen4 2–10 s). `plan_segments` splits the
request into ≤10 s clips; the last clip uses the smallest supported length and the player stops it at
`play_seconds`. Example: 23 s on Kling → 10 + 10 + 5 (shown 3 s). Clips are generated independently
from the same prompt/photo, so continuity between cuts is not guaranteed. Clips are not muxed on the
server (Render has no ffmpeg by default); the UI plays clips and narration together.

## Providers

| `KARRY_VIDEO_PROVIDER` | Key | Model / endpoint |
| --- | --- | --- |
| `replicate` (default) | `REPLICATE_API_TOKEN` | `KARRY_VIDEO_REPLICATE_MODEL` (default `kwaivgi/kling-v2.5-turbo-pro`), `start_image` + `aspect_ratio` |
| `runway` | `RUNWAYML_API_SECRET` | `image_to_video` `gen4_turbo` with a photo, `text_to_video` `gen4.5` without; ratio `1280:720` / `720:1280` |

Set `KARRY_VIDEO_GENERATION_ENABLED=1` to enable. Without it (or without a key) every create request
fails closed with 503. Narration uses `GEMINI_API_KEY` and `KARRY_TTS_MODEL` (Interactions API).

## Security

- Owner bearer token on every route; the upload guard bounds the multipart body before parsing.
- Photos are re-encoded to JPEG data URIs under 900 KB (provider data-URI guidance is < 1 MB).
- Provider job IDs and output URLs never leave the server; clips are downloaded only from allowlisted
  HTTPS hosts (`replicate.delivery`, `KARRY_VIDEO_OUTPUT_HOSTS` for Runway) without credentials and stored in the private bucket.
- Provider error bodies are never echoed; `request_id` makes retries idempotent (no double billing).
