"""ローカル専用の画像・レンダリング用ストレージ。

クライアントから受け取る値を OS パスとして信用せず、全ての永続ファイルを
``Settings.root`` 以下の非公開ディレクトリに限定する。
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import uuid
import warnings
from pathlib import Path, PurePosixPath
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import VideoError


_ALLOWED_FORMATS: Final = {"JPEG", "PNG", "WEBP"}
_UUID_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


class Storage:
    """Private storage rooted at :class:`Settings.root`.

    Images are decoded and re-encoded as PNG on ingress.  This both establishes a
    content type independent of an upload filename and removes EXIF/XMP/ICC and
    PNG text metadata before it can reach FFmpeg.
    """

    def __init__(self, settings):
        self.settings = settings
        requested_root = Path(settings.root).expanduser()
        requested_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if requested_root.is_symlink() or not requested_root.is_dir():
            raise VideoError("storage_unavailable", "安全な保存領域を利用できません。", 500)
        self.root = requested_root.resolve(strict=True)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _uuid(value: str, *, field: str) -> str:
        if not isinstance(value, str):
            raise VideoError("invalid_identifier", f"{field} が不正です。", 422)
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise VideoError("invalid_identifier", f"{field} が不正です。", 422) from exc
        # uuid.UUID accepts a few non-canonical input spellings.  Returning the
        # canonical spelling keeps keys deterministic without trusting the input.
        return str(parsed)

    def _validate_relative(self, relative: str) -> PurePosixPath:
        if not isinstance(relative, str) or not relative or '\x00' in relative:
            raise VideoError("invalid_storage_path", "保存先の指定が不正です。", 422)
        if '\\' in relative:
            raise VideoError("invalid_storage_path", "保存先の指定が不正です。", 422)
        value = PurePosixPath(relative)
        if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
            raise VideoError("invalid_storage_path", "保存先の指定が不正です。", 422)
        return value

    def path(self, relative: str) -> Path:
        """Resolve a storage key without permitting traversal or symlinks.

        Existing path components are inspected with ``lstat``/``is_symlink``; a
        prospective path is additionally resolved and required to remain below the
        private root.  This prevents an uploaded key from escaping via a symlink.
        """
        rel = self._validate_relative(relative)
        candidate = self.root.joinpath(*rel.parts)
        current = self.root
        for part in rel.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if current.is_symlink():
                    raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422)
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422) from exc
        return candidate

    def _private_dir(self, relative: str) -> Path:
        """Create a root-relative directory tree, each level private.

        ``relative`` is internal, but it still uses :meth:`path` so a hostile
        replacement of a directory with a symlink is rejected.
        """
        rel = self._validate_relative(relative)
        current = self.root
        for part in rel.parts:
            current = current / part
            # ``exist_ok`` resolves a normal concurrent mkdir race.  Immediately
            # re-check the resulting object, so a conflicting file or symlink is
            # never accepted merely because another worker won the race.
            try:
                current.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422) from exc
            if current.is_symlink() or not current.is_dir():
                raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422)
            os.chmod(current, 0o700)
        return current

    @staticmethod
    def _image_error() -> VideoError:
        return VideoError("invalid_image", "JPEG、PNG、WebP の安全な静止画像を指定してください。", 422)

    def _decode_image(self, raw: bytes) -> Image.Image:
        if not isinstance(raw, bytes) or not raw or len(raw) > self.settings.max_upload_bytes:
            raise self._image_error()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as check:
                    image_format = check.format
                    if image_format not in _ALLOWED_FORMATS:
                        raise self._image_error()
                    if getattr(check, "n_frames", 1) != 1 or getattr(check, "is_animated", False):
                        raise self._image_error()
                    if check.width < 1 or check.height < 1:
                        raise self._image_error()
                    if (
                        check.width > 8192
                        or check.height > 8192
                        or check.width * check.height > self.settings.max_pixels
                    ):
                        raise self._image_error()
                    check.verify()

                # ``verify`` deliberately invalidates the decoder, so reopen and
                # load every pixel. This is the actual decode admission check.
                with Image.open(io.BytesIO(raw)) as source:
                    if source.format not in _ALLOWED_FORMATS or getattr(source, "n_frames", 1) != 1:
                        raise self._image_error()
                    source.load()
                    oriented = ImageOps.exif_transpose(source)
                    if oriented.width < 1 or oriented.height < 1:
                        raise self._image_error()
                    if (
                        oriented.width > 8192
                        or oriented.height > 8192
                        or oriented.width * oriented.height > self.settings.max_pixels
                    ):
                        raise self._image_error()
                    # Copy pixels only; no source metadata is retained. Preserve
                    # alpha in storage so prepare_image can composite it correctly.
                    mode = "RGBA" if "A" in oriented.getbands() or oriented.mode == "P" else "RGB"
                    return oriented.convert(mode).copy()
        except VideoError:
            raise
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            ValueError,
        ) as exc:
            raise self._image_error() from exc

    @staticmethod
    def _atomic_png(image: Image.Image, target: Path) -> None:
        if target.exists() or target.is_symlink():
            if target.is_symlink():
                raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422)
        fd, name = tempfile.mkstemp(prefix=".image-", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                # No exif/icc_profile/pnginfo arguments: metadata is intentionally
                # stripped on every write.
                image.save(handle, format="PNG", optimize=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def save_image(self, project_id: str, raw: bytes) -> str:
        """Decode a user image and save it as ``images/<uuid>.png``.

        ``project_id`` is validated even though filenames are intentionally random;
        the association belongs in the database rather than in a client-controlled
        filesystem path.
        """
        self._uuid(project_id, field="project_id")
        image = self._decode_image(raw)
        self._private_dir("images")
        key = f"images/{uuid.uuid4()}.png"
        target = self.path(key)
        self._atomic_png(image, target)
        if target.stat().st_size > self.settings.max_upload_bytes:
            target.unlink(missing_ok=True)
            raise self._image_error()
        return key

    def workspace(self, job_id: str) -> Path:
        """Return the job's isolated private ``work/<uuid>`` directory.

        A job identifier is a UUID, therefore this directory is unique per job and
        contains no client-supplied path segment.
        """
        safe_id = self._uuid(job_id, field="job_id")
        self._private_dir("work")
        workspace = self.path(f"work/{safe_id}")
        try:
            workspace.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422) from exc
        if workspace.is_symlink() or not workspace.is_dir():
            raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422)
        os.chmod(workspace, 0o700)
        return workspace

    def output_key(self, job_id: str) -> str:
        safe_id = self._uuid(job_id, field="job_id")
        return f"outputs/{safe_id}.mp4"

    def output_path(self, key: str) -> Path:
        """Return a checked output path and create its private parent.

        This is an internal helper used by the renderer; public keys still pass
        through :meth:`path` first.
        """
        self._private_dir("outputs")
        return self.path(key)

    def prepare_image(self, relative: str, aspect_ratio: str, dest: Path) -> Path:
        """Write a metadata-free, black-letterboxed PNG for FFmpeg input.

        The source is always contained (never cropped). Transparent input is first
        composited onto black, avoiding renderer-dependent alpha handling.
        """
        if aspect_ratio == "16:9":
            size = (self.settings.preview_long_side, self.settings.preview_long_side * 9 // 16)
        elif aspect_ratio == "9:16":
            size = (self.settings.preview_long_side * 9 // 16, self.settings.preview_long_side)
        else:
            raise VideoError("invalid_aspect_ratio", "アスペクト比は 16:9 または 9:16 を指定してください。", 422)

        source = self.path(relative)
        if not source.is_file() or source.is_symlink():
            raise VideoError("image_not_found", "画像を確認できません。", 422)
        # Re-apply the admission controls to stored bytes.  This protects against
        # manual tampering between upload and asynchronous render.
        image = self._decode_image(source.read_bytes())

        destination = Path(os.path.abspath(os.fspath(dest)))
        try:
            rel_dest = destination.relative_to(self.root)
        except ValueError as exc:
            raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422) from exc
        target = self.path(rel_dest.as_posix())
        if not target.parent.is_dir() or target.parent.is_symlink() or target.is_symlink():
            raise VideoError("unsafe_storage_path", "安全でない保存先です。", 422)

        rgba = image.convert("RGBA")
        black = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        black.alpha_composite(rgba)
        flattened = black.convert("RGB")
        contained = ImageOps.pad(flattened, size, method=Image.Resampling.LANCZOS, color=(0, 0, 0))
        self._atomic_png(contained, target)
        return target


__all__ = ["Storage"]
