# Creative Video Suite

Owner-authenticated image/text → video generation with character narration.

## Flow (default: free local synthesis)

```
UI (static/index.html)  ── multipart POST /api/video/jobs ──▶  video_suite_api.build_router
  photo · 16:9|9:16 · 1–120 s · narration                       │ validate (aspect, duration, voice, 4 MiB image)
                                                                │ 202 {status: processing}, render in a background thread:
                                                                │   GeminiNarrator.synthesize → narration.wav (optional)
                                                                │   LocalVideoRenderer: Pillow frame + ffmpeg pan/fade
                                                                │   + narration → one H.264/AAC MP4 → R2/S3
  poll GET /api/video/jobs/{id} every 6 s  ◀────────────────────┤ status: succeeded
  ▶ play / ⬇ save the MP4                                       └ GET /clips/0, /narration (private, no-store)
```

`KARRY_VIDEO_PROVIDER=local` (the default) never calls a paid video API, so there is no billing and
no `402 Insufficient credit`. ffmpeg comes from the `imageio-ffmpeg` wheel (static binary, no apt
step on Render); `KARRY_FFMPEG` overrides the path. Output is 1280×720 or 720×1280 at 15 fps,
`libx264 -preset ultrafast`, `+faststart`; renders run one at a time to fit Render's free instance.
Without a photo the frame is a gradient title card with the prompt (CJK font if installed).
Narration is never silently dropped. Engines are tried in order: Gemini TTS (`GEMINI_API_KEY`) → key-free gTTS (`KARRY_FREE_TTS=1`, default), converted to WAV and muxed as an AAC track. `narration.engine` reports `gemini` or `free`. If both fail, a short audible placeholder (two beeps) is muxed, `narration.status` becomes `placeholder` with an `error` code, the job ends as `partial`, and the UI shows the reason and reads the text with the device voice.
A render killed by a restart is marked `failed` after `LOCAL_RENDER_TIMEOUT` + 5 min.

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

Local mode renders the full length as a single clip. Paid providers only render short clips
(Kling 5/10 s, Runway gen4 2–10 s): `plan_segments` splits the request into ≤10 s clips, the last
clip uses the smallest supported length and the player stops it at `play_seconds`
(23 s on Kling → 10 + 10 + 5, shown 3 s).

## Providers

| `KARRY_VIDEO_PROVIDER` | Cost | Key | Model / endpoint |
| --- | --- | --- | --- |
| `local` (default) | free | none (narration uses `GEMINI_API_KEY` if set) | in-server ffmpeg |
| `replicate` (opt-in) | paid | `REPLICATE_API_TOKEN` | `KARRY_VIDEO_REPLICATE_MODEL` (default `kwaivgi/kling-v2.5-turbo-pro`) |
| `runway` (opt-in) | paid | `RUNWAYML_API_SECRET` | `gen4_turbo` with a photo, `gen4.5` without |

Paid providers additionally require `KARRY_VIDEO_GENERATION_ENABLED=1`; otherwise create requests
fail closed with 503. Narration uses `KARRY_TTS_MODEL` (Interactions API).

## Security

- Owner bearer token on every route; the upload guard bounds the multipart body before parsing.
- Photos are re-encoded to JPEG data URIs under 900 KB (provider data-URI guidance is < 1 MB).
- Provider job IDs and output URLs never leave the server; clips are downloaded only from allowlisted
  HTTPS hosts (`replicate.delivery`, `KARRY_VIDEO_OUTPUT_HOSTS` for Runway) without credentials and stored in the private bucket.
- Provider error bodies are never echoed; `request_id` makes retries idempotent (no double billing).
