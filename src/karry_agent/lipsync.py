"""Free CPU lip sync for the local video renderer.

Finds the face in the base frame (OpenCV Haar cascade), estimates the mouth position,
pre-renders a few mouth-open poses by warping the lower lip/jaw downward and darkening
the opening, then picks one pose per video frame from the narration loudness. This is
a 2D approximation (no GPU model like SadTalker), sized for Render's CPU instance.
"""
from __future__ import annotations

import io
import os
import wave
from dataclasses import dataclass

LEVELS = 5
MIN_FACE_RATIO = 0.08


class LipSyncError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Mouth:
    cx: int
    cy: int
    width: int
    face_height: int


def available() -> bool:
    if os.getenv("KARRY_LIPSYNC", "1") == "0":
        return False
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def _decode(frame: bytes):
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise LipSyncError("invalid_image")
    return image


def find_mouth(frame: bytes) -> Mouth:
    """Largest detected face → mouth centre at ~78% of the face box height."""
    import cv2
    image = _decode(frame)
    gray = cv2.equalizeHist(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    side = int(min(gray.shape[:2]) * MIN_FACE_RATIO)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(side, side))
    if len(faces) == 0:
        raise LipSyncError("no_face")
    x, y, w, h = (int(v) for v in max(faces, key=lambda f: f[2] * f[3]))
    cx, half = x + w // 2, max(2, int(w * 0.18))
    # The lip seam is the darkest row in the lower face band; fall back to the typical 86% height.
    top, bottom = y + int(h * 0.72), min(gray.shape[0], y + int(h * 0.98))
    band = gray[top:bottom, max(0, cx - half):cx + half].mean(axis=1) if bottom - top > 4 else None
    if band is not None:
        band = cv2.GaussianBlur(band.reshape(-1, 1).astype("float32"), (1, 5), 0).ravel()
        cy = top + int(band.argmin())
    else:
        cy = y + int(h * 0.86)
    return Mouth(cx=cx, cy=cy, width=int(w * 0.42), face_height=h)


def mouth_poses(frame: bytes, mouth: Mouth, levels: int = LEVELS) -> list[bytes]:
    """JPEG frames from closed (index 0) to fully open (index levels-1)."""
    import cv2
    import numpy as np
    image = _decode(frame)
    height, width = image.shape[:2]
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    half_w = max(mouth.width / 2, 4.0)
    reach = mouth.face_height * 0.45
    max_open = mouth.face_height * 0.09
    # Smooth weight: 1 at the lower lip, fading out sideways and towards the chin.
    side = np.clip(1 - ((xs - mouth.cx) / (half_w * 1.6)) ** 2, 0, 1)
    below = ys - mouth.cy
    ramp = max(2.0, mouth.face_height * 0.015)
    down = np.where(below >= 0, np.clip(1 - below / reach, 0, 1), np.clip(1 + below / ramp, 0, 1))
    weight = (side ** 1.5) * (down ** 1.2)
    poses = []
    for level in range(levels):
        amount = max_open * level / max(levels - 1, 1)
        warped = cv2.remap(image, xs, ys - weight * amount, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        if amount >= 1:
            mask = np.zeros((height, width), np.float32)
            cv2.ellipse(mask, (mouth.cx, int(mouth.cy + amount * 0.5)), (int(half_w * 0.75), max(1, int(amount * 0.5))),
                        0, 0, 360, 1.0, -1)
            mask = cv2.GaussianBlur(mask, (0, 0), max(1.0, amount * 0.25))[..., None] * 0.8
            cavity = np.array([28, 18, 52], np.float32)
            warped = (warped.astype(np.float32) * (1 - mask) + cavity * mask).astype(np.uint8)
        ok, encoded = cv2.imencode(".jpg", warped, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise LipSyncError("render_failed")
        poses.append(encoded.tobytes())
    return poses


def loudness_levels(wav: bytes, fps: int, frames: int, levels: int = LEVELS) -> list[int]:
    """Per-frame mouth pose index from the narration RMS (silence → closed)."""
    import numpy as np
    try:
        with wave.open(io.BytesIO(wav)) as handle:
            rate, channels, width = handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError):
        raise LipSyncError("invalid_audio") from None
    if width != 2 or rate <= 0:
        raise LipSyncError("invalid_audio")
    samples = np.frombuffer(raw, "<i2").astype(np.float32)
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    step = rate / fps
    rms = np.zeros(frames, np.float32)
    for i in range(frames):
        chunk = samples[int(i * step): int((i + 1) * step)]
        if chunk.size:
            rms[i] = float(np.sqrt(np.mean(chunk ** 2)))
    peak = float(np.percentile(rms[rms > 0], 95)) if np.any(rms > 0) else 0.0
    if peak <= 0:
        return [0] * frames
    norm = np.clip(rms / peak, 0, 1)
    norm[norm < 0.12] = 0
    smooth = np.convolve(norm, [0.25, 0.5, 0.25], mode="same")
    return [int(round(v * (levels - 1))) for v in smooth]


def concat_script(pose_paths: list[str], indices: list[int], fps: int) -> str:
    """ffmpeg concat-demuxer list: one entry per run of identical poses."""
    lines, run_start = ["ffconcat version 1.0"], 0
    for i in range(1, len(indices) + 1):
        if i == len(indices) or indices[i] != indices[run_start]:
            lines += [f"file '{pose_paths[indices[run_start]]}'", f"duration {(i - run_start) / fps:.4f}"]
            run_start = i
    lines.append(f"file '{pose_paths[indices[-1]]}'")
    return "\n".join(lines) + "\n"
