# Lip-sync endpoint (SadTalker) for karry-agent

karry-agent runs on a CPU-only Render instance, so the talking-head model runs elsewhere and
karry-agent calls it. Both options below are free; neither uses a paid video API.

## Option A — this reference server on a free GPU (Colab T4)

```bash
git clone https://github.com/OpenTalker/SadTalker && cd SadTalker
pip install -r requirements.txt && bash scripts/download_models.sh && cd ..
pip install fastapi uvicorn python-multipart
export SADTALKER_DIR=$PWD/SadTalker LIPSYNC_TOKEN=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
uvicorn server:app --host 0.0.0.0 --port 8000   # expose with an HTTPS tunnel, e.g. cloudflared
```

Render (karry-agent-1):

```
KARRY_LIPSYNC_PROVIDER=http
KARRY_LIPSYNC_URL=https://<tunnel-host>/lipsync
KARRY_LIPSYNC_TOKEN=<LIPSYNC_TOKEN>
```

## Option B — your own SadTalker Hugging Face Space

Duplicate a SadTalker Space to your account (a GPU or ZeroGPU Space is fast; free CPU works but is slow),
then set:

```
KARRY_LIPSYNC_PROVIDER=gradio
KARRY_LIPSYNC_URL=https://<you>-sadtalker.hf.space
KARRY_LIPSYNC_TOKEN=<hf_... read token, only for a private Space>
# optional: KARRY_LIPSYNC_GRADIO_API=/test
# optional: KARRY_LIPSYNC_GRADIO_ARGS=["crop", false, false, 2, 256, 0]
```

`KARRY_LIPSYNC_GRADIO_ARGS` are the inputs after (image, audio) for the Space's API; check them on
the Space's "Use via API" page. Public community Spaces checked on 2026-10-19 were broken or refused
to serve results, so use a Space you control.
