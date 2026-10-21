"""Reference lip-sync endpoint for ``KARRY_LIPSYNC_PROVIDER=http`` (run it on a GPU, e.g. free Colab T4).

Contract: ``POST /lipsync`` multipart ``image`` (PNG/JPEG) + ``audio`` (WAV) → ``200 video/mp4``.
It shells out to SadTalker's ``inference.py``; set ``SADTALKER_DIR`` to a checkout with checkpoints
downloaded and ``LIPSYNC_TOKEN`` to the same value as karry-agent's ``KARRY_LIPSYNC_TOKEN``.
"""
from __future__ import annotations

import glob
import hmac
import os
import subprocess
import sys
import tempfile
from typing import Annotated

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response

SADTALKER_DIR = os.environ.get("SADTALKER_DIR", "SadTalker")
TOKEN = os.environ.get("LIPSYNC_TOKEN", "")
MAX_BYTES = 20 * 1024 * 1024
app = FastAPI(title="karry lip-sync (SadTalker)")


@app.post("/lipsync")
def lipsync(image: Annotated[UploadFile, File()], audio: Annotated[UploadFile, File()],
            authorization: Annotated[str, Header()] = ""):
    if not TOKEN or not hmac.compare_digest(authorization, "Bearer " + TOKEN):
        raise HTTPException(401, "unauthorized")
    face, voice = image.file.read(MAX_BYTES + 1), audio.file.read(MAX_BYTES + 1)
    if not face or not voice or len(face) > MAX_BYTES or len(voice) > MAX_BYTES:
        raise HTTPException(413, "image and audio are required (max 20 MiB each)")
    with tempfile.TemporaryDirectory() as work:
        src, wav, out = os.path.join(work, "face.png"), os.path.join(work, "voice.wav"), os.path.join(work, "out")
        for path, body in ((src, face), (wav, voice)):
            with open(path, "wb") as handle:
                handle.write(body)
        cmd = [sys.executable, "inference.py", "--source_image", src, "--driven_audio", wav, "--result_dir", out,
               "--preprocess", os.environ.get("SADTALKER_PREPROCESS", "crop"), "--size", "256"]
        try:
            subprocess.run(cmd, cwd=SADTALKER_DIR, check=True, capture_output=True, timeout=1200)
        except (subprocess.SubprocessError, OSError):
            raise HTTPException(500, "SadTalker failed (is there a clear face in the photo?)") from None
        videos = sorted(glob.glob(os.path.join(out, "**", "*.mp4"), recursive=True), key=os.path.getmtime)
        if not videos:
            raise HTTPException(500, "SadTalker produced no video")
        with open(videos[-1], "rb") as handle:
            return Response(handle.read(), media_type="video/mp4")
