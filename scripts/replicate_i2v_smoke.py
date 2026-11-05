"""Turn one photo into a short MP4 with the Replicate image-to-video adapter (paid, ~$0.05).

Usage: REPLICATE_API_TOKEN=... python scripts/replicate_i2v_smoke.py photo.jpg out.mp4 ["prompt"] [16:9|9:16]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from karry_agent import video_suite as vs


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip())
        return 2
    image, out = Path(sys.argv[1]), Path(sys.argv[2])
    prompt = sys.argv[3] if len(sys.argv) > 3 else ""
    ratio = sys.argv[4] if len(sys.argv) > 4 else "16:9"
    started = time.monotonic()
    try:
        adapter = vs.ReplicateVideoAdapter()
        clip = vs.image_to_video(image.read_bytes(), prompt, ratio, adapter=adapter)
    except vs.VideoError as exc:
        print(f"failed: {exc.code} ({exc.status_code}) {exc.message}")
        return 1
    out.write_bytes(clip)
    print(f"ok: {adapter.model} -> {out} ({len(clip)} bytes, {time.monotonic() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
