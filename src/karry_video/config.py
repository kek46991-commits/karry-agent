import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path = field(default_factory=lambda: Path(os.getenv("VIDEO_STORAGE_ROOT", "./data/video")))
    database_url: str = field(default_factory=lambda: os.getenv("VIDEO_DATABASE_URL") or os.getenv("DATABASE_URL") or "sqlite:///./data/video_projects.db", repr=False)
    api_token: str = field(default_factory=lambda: "".join((os.getenv("VIDEO_API_TOKEN") or "").split())
                           or "".join((os.getenv("KARRY_API_TOKEN") or "").split()), repr=False)
    allow_unauthenticated: bool = False
    max_upload_bytes: int = 8 * 1024 * 1024
    max_pixels: int = 16_000_000
    max_video_bytes: int = 256 * 1024 * 1024
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    espeak_bin: str = "espeak-ng"
    preview_long_side: int = 640
    fps: int = 24
    max_workers: int = 2
    queue_limit: int = 16
    poll_timeout: float = 180.0
    poll_interval: float = 2.0
    lease_seconds: float = 60.0

    def __post_init__(self):
        object.__setattr__(self, "root", self.root.absolute())
        if not 320 <= self.preview_long_side <= 1920 or self.preview_long_side % 32:
            raise ValueError("preview_long_side must be a multiple of 32 between 320 and 1920")
        if not 1 <= self.max_workers <= 8 or self.queue_limit < self.max_workers:
            raise ValueError("invalid worker limits")
        if not 1 <= self.poll_timeout <= 600 or not 0.01 <= self.poll_interval <= 30:
            raise ValueError("invalid polling limits")
        if self.lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
