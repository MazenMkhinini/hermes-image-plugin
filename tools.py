"""Tool handlers for the Hermes ``image-utils`` plugin.

Every handler returns a JSON string (Hermes tool contract). Failures come back as
``{"error": ..., "how_to_fix": ...}`` envelopes — never as raised exceptions, which would surface as
a tool crash in the agent loop. Keywords and JSON (not prints) carry the data.

Safety contract:

- Pillow is the only engine: no subprocess, no shell, no ImageMagick, no new dependencies. The
  Pillow import is guarded so a broken Pillow (or a Pillow-less venv) leaves the plugin *registering*
  its tools: ``image_info`` then explains the state instead of the plugin vanishing.
- Inputs: regular files only, <= 50 MB, <= 50 MP per frame, <= 100 MP summed over frames. Pillow's
  own ``MAX_IMAGE_PIXELS`` guard is never disabled and is escalated to an error for the duration of
  every ``open()``/``load()`` so a decompression bomb is a structured refusal, not a warning.
- Writes never overwrite by default: a new path, or ``overwrite=true`` *and* ``confirm=true``.
- Writes are atomic: temp file in the target's directory, fsync, output-cap check, a re-read check of
  the encoded bytes, then an exclusive publish (``os.link`` with an O_EXCL fallback, unless
  ``overwrite=true`` *and* ``confirm=true`` asked for a replace); the input's read handles are
  released before the publish, because Windows refuses to replace a file that still has one open;
  the temp file is removed on every failure path.
- Every write reports before -> after (dimensions, format, mode, bytes) and the output path.
- Metadata is carried by presence (never ``.get()``-with-``None``, which crashes the savers);
  ``image_rotate`` always clears the EXIF orientation tag because it changes pixel orientation.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import stat
import tempfile
import threading
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes_image_utils")

TOOLSET = "image_utils"

try:  # guarded import: a missing Pillow must not kill plugin registration
    from PIL import Image, ImageOps, ImageSequence

    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - exercised by monkeypatching in tests
    Image = ImageOps = ImageSequence = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

MAX_INPUT_BYTES = 50 * 1024 * 1024          # 50 MB file cap
MAX_PIXELS_PER_FRAME = 50_000_000           # 50 MP per frame (below Pillow's 89,478,485 guard)
MAX_PIXELS_TOTAL = 100_000_000              # 100 MP summed across frames
MAX_OUTPUT_BYTES = 200 * 1024 * 1024        # refuse to write more than 200 MB
DEFAULT_QUALITY = 85                        # image_convert / image_optimize default
GEOMETRY_QUALITY = 95                       # resize/crop/rotate re-encode quality for lossy targets

# Encoder-effort defaults, measured against Pillow 12.3.0 (see the README's encoding-effort table).
# Pillow's own fallbacks sit at the slow end of each range — WebP's saver defaults to method=4 and
# AVIF's to speed=6 — and this plugin only reached those numbers when the caller asked for them.
WEBP_DEFAULT_METHOD = 2                     # effort not given: several times faster than method=4
WEBP_MAX_METHOD = 5                         # effort=9 stops here; method=6 is the slowest, ~0.2% smaller
AVIF_DEFAULT_SPEED = 8                      # effort not given: Pillow's default is 6

SAVE_FORMATS: Tuple[str, ...] = ("PNG", "JPEG", "WEBP", "TIFF", "GIF", "AVIF", "BMP")
EXTENSIONS: Dict[str, str] = {
    "PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "TIFF": ".tiff",
    "GIF": ".gif", "AVIF": ".avif", "BMP": ".bmp",
}
EXTENSION_ALIASES: Dict[str, str] = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".png": "PNG", ".webp": "WEBP",
    ".tif": "TIFF", ".tiff": "TIFF", ".gif": "GIF", ".avif": "AVIF", ".bmp": "BMP",
}

# Which metadata each target format can carry (frozen for Pillow 12.3.0).
# Pillow has no runtime introspection API for this, so it is a frozen table with a test per row.
FORMAT_METADATA_SUPPORT: Dict[str, set] = {
    "exif": {"PNG", "JPEG", "WEBP", "TIFF", "AVIF"},
    "icc": {"PNG", "JPEG", "WEBP", "TIFF", "AVIF"},
    "dpi": {"PNG", "JPEG", "TIFF", "BMP"},
}

RESAMPLE_CHOICES: Dict[str, str] = {
    "lanczos": "LANCZOS", "bicubic": "BICUBIC", "bilinear": "BILINEAR", "nearest": "NEAREST",
}

_EXIF_ORIENTATION = 0x0112
_ROTATING_ORIENTATIONS = (5, 6, 7, 8)   # the tags that swap the axes when a reader applies them
_EXIF_MAKE = 0x010F
_EXIF_MODEL = 0x0110
_EXIF_DATETIME = 0x0132
_EXIF_GPS_IFD = 0x8825


class ToolError(Exception):
    """A structured refusal: the envelope the model sees instead of a traceback."""

    def __init__(self, message: str, how_to_fix: str, **context: Any) -> None:
        super().__init__(message)
        self.payload: Dict[str, Any] = {"error": message, "how_to_fix": how_to_fix}
        for key, value in context.items():
            self.payload[key] = value


# --------------------------------------------------------------------------- envelopes

def _ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(exc: Exception) -> str:
    if isinstance(exc, ToolError):
        return json.dumps(exc.payload, ensure_ascii=False, default=str)
    return json.dumps(
        {"error": f"{type(exc).__name__}: {exc}",
         "how_to_fix": "unexpected failure; re-run with the same arguments and check the gateway log for 'hermes_image_utils'"},
        ensure_ascii=False,
    )


def _require_pillow() -> None:
    if not _PIL_AVAILABLE:
        raise ToolError(
            "Pillow is not importable in this process, so no image can be read or written",
            "the image-utils plugin expects Pillow (12.3.0) in the Hermes venv; "
            "check `hermes plugins doctor` and the gateway log for the plugin line",
        )


_GUARD_LOCK = threading.RLock()


@contextmanager
def _guarded():
    """Turn Pillow's decompression-bomb *warning* into an error for this open/decode.

    ``MAX_IMAGE_PIXELS`` itself is never touched. ``warnings.catch_warnings`` mutates the
    process-global filter list and Hermes runs tool calls in worker threads, so two overlapping
    calls could otherwise leave the 'error' filter installed after both exited; the toggle
    is serialised with ``_GUARD_LOCK`` (warnings filters cannot be made thread-local).
    """
    if Image is None:
        yield
        return
    with _GUARD_LOCK, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        yield


# --------------------------------------------------------------------------- input handling

def _bomb_error(exc: Exception) -> ToolError:
    return ToolError(
        f"refused: {exc}",
        f"this plugin caps inputs at {MAX_PIXELS_PER_FRAME // 1_000_000} MP per frame / "
        f"{MAX_PIXELS_TOTAL // 1_000_000} MP total and keeps Pillow's own decompression-bomb guard active; "
        "downscale a copy of the file elsewhere first",
    )


def _heic_hint(name: str) -> str:
    state = "HEIC/HEIF is not a Pillow-core format"
    try:
        import importlib.util

        if importlib.util.find_spec("pillow_heif") is not None:
            state = ("pillow_heif 1.5.0 is installed in the venv but is not registered with Pillow in "
                     "this process, and image-utils v1 deliberately does not register it")
    except Exception:
        pass
    return f"{state}; convert {name} to JPEG/PNG elsewhere first — no pip install is needed or suggested"


def _unidentified(path: Path) -> ToolError:
    suffix = path.suffix.lower()
    if suffix in (".heic", ".heif"):
        return ToolError(
            f"cannot read HEIC/HEIF input: {path.name}",
            _heic_hint(path.name),
        )
    return ToolError(
        f"not a readable image: {path}",
        "supported inputs are PNG/JPEG/WebP/TIFF/GIF/AVIF/BMP; HEIC/HEIF is out of scope for v1",
    )


def _open_source(raw_path: Any) -> Tuple[Any, Path, int]:
    """Validate + open a source image (header stage only; callers must decode and close)."""
    _require_pillow()
    if raw_path is None or not str(raw_path).strip():
        raise ToolError("no input path given", "pass path=<absolute or ~/path to an image file>")
    path = Path(str(raw_path)).expanduser()
    try:
        st = os.stat(path)  # follows symlinks
    except FileNotFoundError:
        raise ToolError(f"input does not exist: {path}",
                        "pass the path of an existing image file (absolute, or ~-expanded)")
    except OSError as exc:
        raise ToolError(f"cannot read input: {type(exc).__name__}: {exc}",
                        "check the path and the permissions on the file and its directory")
    if stat.S_ISDIR(st.st_mode):
        raise ToolError(f"input is a directory, not a file: {path}", "pass an image file, not a directory")
    if not stat.S_ISREG(st.st_mode):
        raise ToolError(f"input is not a regular file: {path}",
                        "only regular image files are supported (no devices, FIFOs or sockets)")
    if st.st_size == 0:
        raise ToolError(f"input is empty (0 bytes): {path}",
                        "the file has no content — re-export or re-download the image")
    if st.st_size > MAX_INPUT_BYTES:
        raise ToolError(
            f"input is {st.st_size:,} bytes, over the {MAX_INPUT_BYTES // (1024 * 1024)} MB cap: {path}",
            f"downscale or re-compress a copy elsewhere first; image-utils refuses inputs over "
            f"{MAX_INPUT_BYTES // (1024 * 1024)} MB",
        )

    with _guarded():
        try:
            im = Image.open(path)
        except Image.DecompressionBombError as exc:  # not an OSError — must be caught explicitly
            raise _bomb_error(exc)
        except Image.DecompressionBombWarning as exc:  # _guarded() escalates the 89.5-179 MP band
            raise _bomb_error(exc)
        except Image.UnidentifiedImageError:
            raise _unidentified(path)
        except OSError as exc:
            raise ToolError(f"cannot open image: {type(exc).__name__}: {exc}",
                            "the file may be damaged; re-export it from the source application")

    try:
        width, height = im.size
        pixels = int(width) * int(height)
        frames = max(1, int(getattr(im, "n_frames", 1) or 1))
        if width <= 0 or height <= 0:
            raise ToolError(f"image reports an unusable size {width}x{height}",
                            "the file is corrupt; re-export it from the source application")
        if pixels > MAX_PIXELS_PER_FRAME:
            raise ToolError(
                f"image is {width}x{height} = {pixels:,} pixels, over the "
                f"{MAX_PIXELS_PER_FRAME // 1_000_000} MP per-frame cap",
                "downscale a copy of the file elsewhere first",
                width=int(width), height=int(height), pixels=pixels, frames=frames, decode_ok=False,
            )
        if frames * pixels > MAX_PIXELS_TOTAL:
            raise ToolError(
                f"animation is {frames} frames x {pixels:,} px = {frames * pixels:,} pixels, over the "
                f"{MAX_PIXELS_TOTAL // 1_000_000} MP total cap",
                "trim or downscale the animation elsewhere first",
                width=int(width), height=int(height), pixels=pixels, frames=frames, decode_ok=False,
            )
    except ToolError:
        im.close()
        raise
    if (im.format or "").upper() == "TIFF":
        try:
            # Pillow's TIFF reader applies the orientation to the pixels and consumes the tag when
            # the image is loaded; snapshot it at header time so the notes can tell the truth.
            orientation = im.getexif().get(_EXIF_ORIENTATION)
        except Exception:
            orientation = None
        setattr(im, "_hermes_tiff_orientation", orientation)
    return im, path, int(st.st_size)


def _decode(im: Any) -> None:
    """Decode the pixels. ``open()`` only reads headers; a truncated file only fails here."""
    with _guarded():
        try:
            im.load()
        except Image.DecompressionBombError as exc:
            raise _bomb_error(exc)
        except Image.DecompressionBombWarning as exc:  # escalated warning, same refusal
            raise _bomb_error(exc)
        except (OSError, ValueError) as exc:
            message = str(exc)
            if "truncated" in message.lower():
                raise ToolError(
                    f"image is truncated and cannot be decoded: {message}",
                    "re-export or re-download the file — image-utils never enables Pillow's "
                    "LOAD_TRUNCATED_IMAGES",
                )
            raise ToolError(f"cannot decode image: {type(exc).__name__}: {message}",
                            "the file may be corrupt; re-export it from the source application")


def _open_decoded(raw_path: Any):
    """Open + decode a source image. Callers own ``im`` and must close it."""
    im, src, src_bytes = _open_source(raw_path)
    try:
        _decode(im)
    except ToolError:
        im.close()
        raise
    return im, src, src_bytes


def _target_for(im: Any, src: Path, output_path: Any, operation: str, fmt: Optional[str],
                overwrite: bool, confirm: bool):
    """Resolve + gate the output path once the tool's own parameters are known to be valid.

    ``fmt`` is the *explicit* target format (image_convert); pass ``None`` for the tools that keep
    the input's format. Returns ``(target, replaced, fmt)``.
    """
    resolved = str(fmt).upper() if fmt else (im.format or "").upper()
    if resolved not in SAVE_FORMATS:
        resolved = "PNG"  # unknown input format: default to a lossless container
    try:
        target, replaced = _resolve_target(src, output_path, operation, resolved, overwrite, confirm, im)
    except ToolError:
        im.close()
        raise
    return target, replaced, resolved


def _format_notes(source_im: Any, target: Path, fmt: str) -> List[str]:
    """Notes about a forced format fallback and about the name not matching the bytes."""
    notes: List[str] = []
    source_fmt = (source_im.format or "").upper()
    if source_fmt and source_fmt not in SAVE_FORMATS:
        notes.append(f"input format {source_fmt} cannot be written by Pillow 12.3.0; the bytes are "
                     f"{fmt} (a lossless container was chosen)")
    suffix = target.suffix.lower()
    if suffix and EXTENSION_ALIASES.get(suffix) != fmt:
        notes.append(f"output_path suffix {target.suffix} does not match the written format {fmt} "
                     f"(the bytes are {fmt}, the name is yours to choose)")
    return notes


def _resolve_target(src: Path, output_path: Any, operation: str, fmt: str,
                    overwrite: bool, confirm: bool, im: Any) -> Tuple[Path, bool]:
    if output_path:
        target = Path(str(output_path)).expanduser()
    else:
        suffix = EXTENSIONS[fmt]
        if fmt == (im.format or "").upper() and src.suffix.lower() in EXTENSION_ALIASES:
            suffix = src.suffix.lower()  # keep the input's own extension (e.g. .jpeg)
        target = src.with_name(f"{src.stem}_{operation}{suffix}")
    target = target.resolve()          # follows symlinks: the write lands on the resolved target
    src_resolved = src.resolve()

    overwrite = bool(overwrite)
    confirm = bool(confirm)
    if overwrite and not confirm:
        raise ToolError(
            f"overwrite=true needs confirm=true (refusing to replace {target})",
            "re-issue with overwrite=true and confirm=true to replace the file, or drop overwrite "
            "and pass a fresh output_path",
        )

    exists = target.exists() or target.is_symlink()
    if target == src_resolved:
        if not (overwrite and confirm):
            im.close()
            raise ToolError(
                f"the output path is the input file ({src_resolved}); refusing to overwrite in place",
                "pass output_path for a new file, or overwrite=true AND confirm=true to replace the "
                "input — in place is never the default",
            )
        replaced = True
    elif exists:
        if not (overwrite and confirm):
            im.close()
            raise ToolError(
                f"output already exists: {target}",
                "the default output name is a function of the input and the operation, so re-runs "
                "collide; pass a different output_path, or overwrite=true and confirm=true to replace "
                "it",
            )
        replaced = True
    else:
        replaced = False

    if not target.parent.is_dir():
        im.close()
        raise ToolError(
            f"output directory does not exist: {target.parent}",
            "create the directory first, or pass output_path inside an existing directory — the "
            "plugin never creates directories",
        )
    if target.exists() and target.is_dir():
        im.close()
        raise ToolError(f"output path is a directory: {target}", "pass a file path, not a directory")
    return target, replaced


# --------------------------------------------------------------------------- output handling

def _publish_exclusive(tmp: Path, target: Path) -> None:
    """Publish ``tmp`` as ``target`` only if nothing is at ``target`` (no TOCTOU window)."""
    try:
        os.link(tmp, target)          # atomic create-exclusive where hard links exist
        return
    except FileExistsError:
        raise ToolError(f"output appeared while writing (concurrent write?): {target}",
                        "retry, or pass overwrite=true and confirm=true to replace it")
    except OSError:
        pass                          # no hard links here (FAT/exFAT): fall back to O_EXCL
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise ToolError(f"output appeared while writing (concurrent write?): {target}",
                        "retry, or pass overwrite=true and confirm=true to replace it")
    except OSError as exc:
        raise ToolError(f"cannot write {target}: {type(exc).__name__}: {exc}",
                        "the output directory is not writable for this process; choose another "
                        "directory or fix its permissions")
    try:
        with os.fdopen(fd, "wb") as out, open(tmp, "rb") as source_fh:
            shutil.copyfileobj(source_fh, out)
            out.flush()
            os.fsync(out.fileno())
    except OSError as exc:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise ToolError(f"cannot write {target}: {type(exc).__name__}: {exc}",
                        "the output directory is not writable for this process; choose another "
                        "directory or fix its permissions")


def _verify_encoded(tmp: Path, expect: Optional[Dict[str, Any]]) -> None:
    """Re-read the encoded temp file before publishing it: size, frame count, decodability."""
    if not expect:
        return
    read_orientation = None
    try:
        with Image.open(tmp) as check:
            size = (int(check.size[0]), int(check.size[1]))
            frames = max(1, int(getattr(check, "n_frames", 1) or 1))
            read_orientation = check.getexif().get(_EXIF_ORIENTATION)
            check.load()
            icc_bytes = len(check.info.get("icc_profile") or b"")
            exif_present = bool(len(check.getexif()))
    except Exception as exc:
        raise ToolError(
            f"the encoder produced an unreadable file ({type(exc).__name__}: {exc}); nothing was written",
            "this is a plugin bug, not your input — try another target format and report it",
        )
    want_size = expect.get("size")
    if want_size is not None:
        want = (int(want_size[0]), int(want_size[1]))
        # Pillow's TIFF reader applies a rotating EXIF orientation (5-8) as it opens the file and
        # consumes the tag at load, so a TIFF written with such a tag reads back transposed — which
        # is the file being right, not wrong. Only that declared case is allowed through: an encoder
        # that merely swapped the two dimensions still fails here.
        transposed_by_tag = size == (want[1], want[0]) and read_orientation in _ROTATING_ORIENTATIONS
        if size != want and not transposed_by_tag:
            raise ToolError(
                f"the encoder wrote {size[0]}x{size[1]} but the tool reported "
                f"{want[0]}x{want[1]}; nothing was written",
                "this is a plugin bug, not your input — try another target format and report it",
            )
    want_frames = expect.get("frames")
    if want_frames is not None and frames != max(1, int(want_frames)):
        raise ToolError(
            f"the encoder wrote {frames} frame(s) but the tool reported {max(1, int(want_frames))}; "
            f"nothing was written",
            "this is a plugin bug, not your input — try another target format and report it",
        )
    if expect.get("strip") and (icc_bytes or exif_present):
        raise ToolError(
            f"strip_metadata=true but the output still carries metadata (icc {icc_bytes} bytes, "
            f"exif {exif_present}); nothing was written",
            "this is a plugin bug, not your input — report it",
        )


_WINDOWS_RETRY_WINERRORS = (5, 32)   # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
_PUBLISH_ATTEMPTS = 4


def _publish_with_retry(publish: Callable[[], None]) -> None:
    """Publish, retrying briefly on the Windows errors an external holder causes.

    An antivirus scanner, the search indexer or a sync client can hold the target at the instant of
    the rename, which Windows reports as WinError 5 or 32 even when nothing in this process still
    has the file open. Nothing in-process can prevent that, so the rename is retried a few times
    with a short backoff before the failure is reported. POSIX raises no ``winerror`` and is never
    retried — its permission failures are real.
    """
    for attempt in range(_PUBLISH_ATTEMPTS):
        try:
            publish()
            return
        except OSError as exc:
            transient = getattr(exc, "winerror", None) in _WINDOWS_RETRY_WINERRORS
            if not transient or attempt == _PUBLISH_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _release_input(image: Any) -> None:
    """Release the input's read handle, so its file can be replaced.

    Windows refuses to replace a file that still has an open handle (``os.replace`` fails with
    WinError 5); POSIX allows it, so an in-place write only ever broke there. Two things have to go:

    - the file handle: Pillow keeps it in ``ImageFile._fp`` (the public ``fp`` is already ``None``
      once ``load()`` has run), and ``Image.close()`` is what releases it;
    - the mapping: for single-strip BMP/TIFF/PNG/P inputs Pillow maps the file instead of reading it
      (``ImageFile`` only mmaps when the mode is in ``_MAPMODES``), and ``close()`` merely drops
      ``self.map`` — it closes the mapping itself only under win32 + PyPy — so a live mapping would
      otherwise survive until the garbage collector happens to run, which is exactly the kind of
      timing a rename cannot rely on.
    """
    mapping = getattr(image, "map", None)   # read before close(): close() clears the attribute
    try:
        image.close()
    except Exception:
        pass
    if mapping is not None and hasattr(mapping, "close"):
        try:
            mapping.close()
        except Exception:
            pass


def _write_atomic(target: Path, writer: Callable[[Any], None], overwrite: bool, confirm: bool,
                  expect: Optional[Dict[str, Any]] = None,
                  release: Optional[Callable[[], None]] = None) -> int:
    """Encode into a same-directory temp file, verify it, then publish it."""
    parent = target.parent
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{target.stem}.", suffix=".tmp")
    except OSError as exc:
        raise ToolError(
            f"cannot create a temporary file in {parent} ({type(exc).__name__})",
            "the output directory is not writable for this process; choose another directory or "
            "fix its permissions",
        )
    tmp: Optional[Path] = Path(tmp_name)
    try:
        with os.fdopen(fd, "w+b") as fh:   # w+b: the multi-page TIFF saver re-reads what it writes
            writer(fh)
            fh.flush()
            os.fsync(fh.fileno())
        size = tmp.stat().st_size
        if size == 0:
            raise ToolError(f"the encoder produced an empty file for {target.name}",
                            "the target format may not support this image; try another format")
        if size > MAX_OUTPUT_BYTES:
            raise ToolError(
                f"encoded output would be {size:,} bytes, over the {MAX_OUTPUT_BYTES:,}-byte cap",
                "lower quality, pass a smaller max_dimension, or choose a more compact format",
            )
        _verify_encoded(tmp, expect)
        try:
            mode = stat.S_IMODE(os.stat(target).st_mode)   # read before the replace: afterwards the
        except OSError:                                    # target carries the temp file's mode
            mode = 0o644

        def _publish() -> None:
            if overwrite and confirm:
                os.replace(tmp, target)      # the caller explicitly asked to replace the target
                return
            if target.exists():
                raise ToolError(f"output appeared while writing (concurrent write?): {target}",
                                "retry, or pass overwrite=true and confirm=true to replace it")
            _publish_exclusive(tmp, target)

        # Releasing, renaming and restoring the mode are one critical section: between them the
        # target must not be opened again. Hermes runs tool calls in worker threads, and on Windows
        # an open handle anywhere turns the rename into WinError 5. _GUARD_LOCK is the lock that
        # already serialises _guarded(), and it is re-entrant.
        with _GUARD_LOCK:
            if release is not None:
                # The input's handle has to be gone before its own file can be replaced: Windows
                # refuses a rename over a file that still has one open, POSIX allows it. The encoded
                # bytes are verified by now, so nothing needs the input any more.
                release()
            _publish_with_retry(_publish)
            try:
                # After the rename, not on the temp file before it: Windows denies replacing a
                # read-only file (the same WinError 5 signature), and the temp file's own mode is
                # narrower (0600) than the 0644 a fresh output would get.
                os.chmod(target, mode)
            except OSError:
                pass
        return size                          # the finally unlinks the temp name (a no-op after replace)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _before(im: Any, src_bytes: int) -> Dict[str, Any]:
    return {"dimensions": [int(im.size[0]), int(im.size[1])], "format": im.format or "?",
            "mode": im.mode, "bytes": int(src_bytes)}


def _after_from_disk(target: Path, size: int, fmt: str) -> Dict[str, Any]:
    """After-facts read back from the file that was written: the bytes are the truth."""
    facts: Dict[str, Any] = {"format": fmt, "bytes": int(size), "path": str(target),
                             "dimensions": None, "mode": None}
    try:
        with Image.open(target) as check:
            facts["dimensions"] = [int(check.size[0]), int(check.size[1])]
            facts["mode"] = check.mode
    except Exception as exc:   # a successful write is never failed over the read-back
        facts["verify_error"] = f"{type(exc).__name__}: {exc}"
    return facts


# --------------------------------------------------------------------------- metadata helpers

def _parse_color(value: Any, default: Tuple[int, int, int] = (255, 255, 255)) -> Tuple[int, int, int]:
    if value is None:
        return default
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return (int(value[0]), int(value[1]), int(value[2]))
        except (TypeError, ValueError):
            raise ToolError(f"background must be an RGB triple or a colour name, got {value!r}",
                            "pass background='white', '#rrggbb', or [r, g, b]")
    text = str(value).strip().lower()
    names = {"white": (255, 255, 255), "black": (0, 0, 0), "grey": (128, 128, 128),
             "gray": (128, 128, 128), "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255)}
    if text in names:
        return names[text]
    if text.startswith("#") and len(text) == 7:
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            pass
    raise ToolError(f"cannot parse background colour: {value!r}",
                    "pass background='white', '#rrggbb', or [r, g, b]")


def _alpha_present(im: Any) -> bool:
    return im.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in im.info


def _pick_resample(name: Any, default: str = "lanczos") -> Any:
    key = str(name or default).strip().lower()
    if key not in RESAMPLE_CHOICES:
        raise ToolError(f"unknown resample {name!r}",
                        f"choose one of {', '.join(sorted(RESAMPLE_CHOICES))}")
    return getattr(Image.Resampling, RESAMPLE_CHOICES[key])


def _save_all_formats() -> set:
    try:
        Image.init()
        return set(Image.SAVE_ALL.keys())
    except Exception:  # pragma: no cover
        return {"PNG", "GIF", "TIFF", "WEBP", "AVIF"}


def _effort_kwargs(fmt: str, effort: Any) -> Tuple[Dict[str, Any], List[str]]:
    if effort is None:
        # Not asking for an effort is not a reason to inherit the encoder's slowest sane setting.
        if fmt == "WEBP":
            return {"method": WEBP_DEFAULT_METHOD}, [
                f"effort not given -> WebP method {WEBP_DEFAULT_METHOD} (Pillow's own default is 4, "
                f"which measures 1.6-2.0x slower at the same quality setting)"]
        if fmt == "AVIF":
            return {"speed": AVIF_DEFAULT_SPEED}, [
                f"effort not given -> AVIF speed {AVIF_DEFAULT_SPEED} (Pillow's own default is 6; "
                f"higher is faster and slightly larger)"]
        return {}, []
    if isinstance(effort, bool):
        raise ToolError(f"effort must be an integer 0-9, got {effort!r}", "pass effort between 0 and 9")
    try:
        e = max(0, min(9, int(effort)))
    except (TypeError, ValueError):
        raise ToolError(f"effort must be an integer 0-9, got {effort!r}", "pass effort between 0 and 9")
    if fmt == "WEBP":
        method = max(0, min(WEBP_MAX_METHOD, round(e * 6 / 9)))
        return {"method": method}, [f"effort {e} -> WebP method {method}"]
    if fmt == "AVIF":
        speed = max(0, min(10, 10 - round(e * 10 / 9)))
        return {"speed": speed}, [f"effort {e} -> AVIF speed {speed} (lower is slower and better)"]
    if fmt == "PNG":
        return {"compress_level": e}, [f"effort {e} -> PNG compress_level {e}"]
    if fmt == "JPEG":
        kw: Dict[str, Any] = {}
        notes = []
        if e >= 6:
            kw["optimize"] = True
            notes.append("effort >= 6 -> JPEG optimize=True")
        if e >= 8:
            kw["progressive"] = True
            notes.append("effort >= 8 -> JPEG progressive=True")
        return kw, notes or [f"effort {e} -> JPEG has no compression-effort knob below 6"]
    if fmt == "TIFF":
        if e >= 5:
            return {"compression": "tiff_deflate"}, ["effort >= 5 -> TIFF deflate compression"]
        return {}, ["effort < 5 -> TIFF left uncompressed"]
    return {}, [f"effort ignored: {fmt} has no effort/compression knob in Pillow 12.3.0"]


_TIFF_SAFE_EXIF_TAGS = (0x010F, 0x0110, 0x0112, 0x0132, 0x8825, 0x9003, 0x9004, 0x829A, 0x829D)


def _safe_exif(source_fmt: str, exif: Any) -> Any:
    """Keep only user-level tags when the source's IFD is its structural directory.

    A TIFF's ``getexif()`` *is* the image's IFD (StripOffsets, TileOffsets, BitsPerSample, …);
    writing it onto a re-encoded image makes the file declare the source's geometry.
    """
    if (source_fmt or "").upper() != "TIFF":
        return exif
    keep = Image.Exif()
    for tag in _TIFF_SAFE_EXIF_TAGS:
        try:
            value = exif.get(tag)
        except Exception:
            value = None
        if value is not None:
            keep[tag] = value
    return keep


def _save_kwargs(im: Any, fmt: str, *, quality: Any = None, effort: Any = None,
                 strip_metadata: bool = False, exif_override: Any = None,
                 geometry_quality: bool = False) -> Tuple[Dict[str, Any], List[str], List[str], bool]:
    """Build save kwargs by presence (never ``.get()``-with-None) and report what was carried.

    Returns ``(kwargs, notes, dropped, orientation_present_in_output)``.
    """
    kwargs: Dict[str, Any] = {}
    notes: List[str] = []
    dropped: List[str] = []

    if strip_metadata:
        # An explicit empty value beats Pillow's own carry-into from im.info (PNG/TIFF)
        kwargs["icc_profile"] = b""
    if not strip_metadata:
        want_exif = exif_override is not None or ("exif" in im.info) or len(im.getexif()) > 0
        if want_exif:
            if fmt in FORMAT_METADATA_SUPPORT["exif"]:
                try:
                    ex = exif_override if exif_override is not None else im.getexif()
                    ex = _safe_exif(im.format, ex)   # never round-trip a TIFF's structural IFD
                    if len(ex):  # an empty Exif would write a useless APP1 block
                        kwargs["exif"] = ex.tobytes()
                except Exception as exc:  # metadata is never worth failing a write over
                    notes.append(f"exif not carried ({type(exc).__name__})")
                    dropped.append("exif")
            else:
                dropped.append("exif")
        icc = im.info.get("icc_profile")
        if icc:
            if fmt in FORMAT_METADATA_SUPPORT["icc"]:
                kwargs["icc_profile"] = icc
            else:
                dropped.append("icc_profile")
        dpi = im.info.get("dpi")
        if dpi:
            if fmt in FORMAT_METADATA_SUPPORT["dpi"]:
                kwargs["dpi"] = tuple(dpi)
            else:
                dropped.append("dpi")
    if fmt in ("JPEG", "WEBP", "AVIF"):
        q = quality
        if q is None:
            q = GEOMETRY_QUALITY if geometry_quality else DEFAULT_QUALITY
        try:
            kwargs["quality"] = max(1, min(100, int(q)))
            notes.append(f"quality={kwargs['quality']}")
        except (TypeError, ValueError):
            raise ToolError(f"quality must be an integer 1-100, got {quality!r}",
                            "pass quality between 1 and 100")
    elif quality is not None:
        notes.append(f"quality ignored: {fmt} has no quality setting in Pillow 12.3.0")

    effort_kwargs, effort_notes = _effort_kwargs(fmt, effort)
    kwargs.update(effort_kwargs)
    notes.extend(effort_notes)
    return kwargs, notes, dropped, bool(exif_override is not None)


# --------------------------------------------------------------------------- tools: read

def image_info(**params: Any) -> str:
    """READ-ONLY facts about one image file, including honest failure states."""
    raw = params.get("path")
    path = Path(str(raw)).expanduser() if raw else None
    base: Dict[str, Any] = {"pillow_available": _PIL_AVAILABLE}
    if path is not None:
        base["path"] = str(path)
        try:
            base["bytes"] = os.stat(path).st_size
        except OSError:
            base["bytes"] = None

    if not _PIL_AVAILABLE:
        return _err(ToolError(
            "Pillow is not importable in this process, so no image facts can be read",
            "image-utils expects Pillow 12.3.0 in the Hermes venv; check `hermes plugins doctor` and "
            "the gateway log for the image-utils plugin line",
            **base,
        ))

    try:
        im, path, src_bytes = _open_source(raw)
    except ToolError as exc:
        payload = dict(exc.payload)
        payload.setdefault("pillow_available", True)
        if path is not None:
            payload.setdefault("path", str(path))
            try:
                payload.setdefault("bytes", os.stat(path).st_size)
            except OSError:
                pass
        return _ok(payload)

    try:
        facts: Dict[str, Any] = dict(base)
        facts["path"] = str(path)
        facts["bytes"] = src_bytes
        facts["format"] = im.format or "?"
        facts["mode"] = im.mode
        facts["width"], facts["height"] = int(im.size[0]), int(im.size[1])
        facts["pixels"] = facts["width"] * facts["height"]
        facts["megapixels"] = round(facts["pixels"] / 1_000_000, 2)
        frames = max(1, int(getattr(im, "n_frames", 1) or 1))
        facts["frames"] = frames
        facts["animated"] = bool(getattr(im, "is_animated", False))
        if facts["animated"] and frames <= 200:
            durations = []
            for index in range(frames):
                try:
                    im.seek(index)
                except EOFError:
                    break
                durations.append(im.info.get("duration"))
            facts["frame_durations"] = durations
            facts["loop"] = im.info.get("loop")
            try:
                im.seek(0)
            except EOFError:
                pass
        dpi = im.info.get("dpi")
        if dpi:
            try:
                facts["dpi"] = [round(float(v), 2) if abs(float(v) - round(float(v))) > 0.01 else int(round(float(v))) for v in tuple(dpi)[:2]]
            except Exception:
                facts["dpi"] = None
        else:
            facts["dpi"] = None
        icc = im.info.get("icc_profile")
        facts["icc_profile_present"] = bool(icc)
        facts["icc_profile_bytes"] = len(icc) if icc else 0

        try:  # decode first: PNG/TIFF EXIF reads load the file, which can fail on a damaged input
            _decode(im)
            facts["decode_ok"] = True
        except ToolError as exc:
            facts["decode_ok"] = False
            facts["decode_error"] = exc.payload["error"]

        try:
            exif = im.getexif()
            try:
                gps_present = bool(exif.get_ifd(_EXIF_GPS_IFD))
            except Exception:
                gps_present = False
            facts["exif"] = {
                "has_exif": bool(len(exif)) or ("exif" in im.info),
                "camera_make": exif.get(_EXIF_MAKE),
                "camera_model": exif.get(_EXIF_MODEL),
                "datetime": exif.get(_EXIF_DATETIME),
                "orientation": exif.get(_EXIF_ORIENTATION),
                "gps_present": gps_present,   # presence only — coordinates are deliberately not returned
            }
        except Exception as exc:   # damaged files: report the header facts, name the failure
            facts["exif"] = {"error": f"{type(exc).__name__}: {exc}"}
        return _ok(facts)
    finally:
        im.close()


# --------------------------------------------------------------------------- tools: writes

def image_resize(**params: Any) -> str:
    try:
        width = params.get("width")
        height = params.get("height")
        percent = params.get("percent")
        allow_distort = bool(params.get("allow_distort", False))
        allow_upscale = bool(params.get("allow_upscale", False))
        resample_name = params.get("resample", "lanczos")

        im, src, src_bytes = _open_source(params.get("path"))   # header-only: draft() needs the dimensions first
        try:
            src_w, src_h = int(im.size[0]), int(im.size[1])
            mode = im.mode
            before = {"dimensions": [src_w, src_h], "format": im.format or "?", "mode": mode,
                      "bytes": int(src_bytes)}

            given = [name for name, value in (("width", width), ("height", height), ("percent", percent))
                     if value is not None]
            if not given:
                raise ToolError("nothing to do: pass width, height or percent",
                                "pass exactly one of width / height / percent (or width AND height "
                                "with allow_distort=true)")
            if len(given) > 1 and not (set(given) == {"width", "height"} and allow_distort):
                raise ToolError(f"ambiguous resize: got {', '.join(given)}",
                                "pass exactly one of width / height / percent; passing both width and "
                                "height needs allow_distort=true (aspect is preserved otherwise)")

            if percent is not None:
                try:
                    factor = float(percent) / 100.0
                except (TypeError, ValueError):
                    raise ToolError(f"percent must be a number, got {percent!r}", "pass percent=50 for half size")
                if not math.isfinite(factor) or factor <= 0:
                    raise ToolError(f"percent must be a finite number > 0, got {percent!r}",
                                    "pass percent=50 for half size")
                tgt_w = max(1, int(round(src_w * factor)))
                tgt_h = max(1, int(round(src_h * factor)))
            elif width is not None and height is not None:
                tgt_w, tgt_h = _whole_int(width, "width"), _whole_int(height, "height")
            elif width is not None:
                tgt_w = _whole_int(width, "width")
                tgt_h = max(1, int(round(src_h * (tgt_w / float(src_w)))))
            else:
                tgt_h = _whole_int(height, "height")
                tgt_w = max(1, int(round(src_w * (tgt_h / float(src_h)))))

            if tgt_w == src_w and tgt_h == src_h:
                im.close()
                return _ok({"input": str(src), "output": None, "written": False,
                            "notes": [f"target dimensions equal the source ({src_w}x{src_h}); nothing to write"],
                            "before": before})
            if (tgt_w > src_w or tgt_h > src_h) and not allow_upscale:
                raise ToolError(
                    f"refusing to upscale {src_w}x{src_h} to {tgt_w}x{tgt_h}",
                    "pass allow_upscale=true to allow it, or pick dimensions no larger than the source",
                )
            if tgt_w * tgt_h > MAX_PIXELS_PER_FRAME:
                raise ToolError(
                    f"target {tgt_w}x{tgt_h} = {tgt_w * tgt_h:,} pixels, over the "
                    f"{MAX_PIXELS_PER_FRAME // 1_000_000} MP per-frame cap",
                    "choose a smaller target; the cap protects this process's memory",
                )

            target, replaced, fmt = _target_for(im, src, params.get("output_path"), "resize", None,
                                                params.get("overwrite", False),
                                                params.get("confirm", False))
            resample_key = str(resample_name or "").strip().lower() or "lanczos"
            resample = _pick_resample(resample_key)
            drafted = False
            if (fmt == "JPEG" and im.mode in ("RGB", "L")
                    and tgt_w * tgt_h < src_w * src_h
                    and RESAMPLE_CHOICES[resample_key] != "NEAREST"):
                try:
                    full_size = im.size
                    im.draft(im.mode, (tgt_w, tgt_h))  # decode only as much as the target needs
                    drafted = im.size != full_size   # draft() is a no-op below a 2x reduction
                except Exception:
                    pass
            _decode(im)   # decoded AFTER draft(), so draft() really does cut the decode
            frames, durations = _frames_of(im)
            result_frames = [frame.resize((tgt_w, tgt_h), resample=resample) for frame in frames]
            result_frames, extra, frames_written, frames_dropped, frame_notes = _animation_extra(
                fmt, result_frames, durations, im.info.get("loop", 0))
            notes = [f"resample={resample_key}"]
            if drafted:
                notes.append("JPEG draft decode used: decoded at a reduced scale before resizing")
            notes.extend(frame_notes)
            notes.extend(_format_notes(im, target, fmt))
            kwargs, meta_notes, dropped, _ = _save_kwargs(
                im, fmt, geometry_quality=True, strip_metadata=False)
            kwargs.update(extra)
            notes.extend(meta_notes)
            if dropped:
                notes.append("metadata dropped by target format: " + ", ".join(sorted(set(dropped))))
            size = _write_atomic(target, lambda fh: result_frames[0].save(fh, format=fmt, **kwargs),
                                 bool(params.get("overwrite")), bool(params.get("confirm")),
                                 expect={"size": result_frames[0].size, "frames": frames_written},
                                 release=lambda: _release_input(im))
            after = _after_from_disk(target, size, fmt)
            notes.append(_orientation_note(im, "resize"))
            im.close()
            return _ok({"input": str(src), "output": str(target), "before": before,
                        "after": {k: v for k, v in after.items() if k != "path"},
                        "replaced": replaced, "frames_written": frames_written,
                        "frames_dropped": frames_dropped, "notes": notes})
        except ToolError:
            im.close()
            raise
        except Exception as exc:
            im.close()
            raise ToolError(f"{type(exc).__name__}: {exc}",
                            "unexpected failure while resizing; check the input format and the arguments")
    except Exception as exc:
        return _err(exc)


def image_crop(**params: Any) -> str:
    try:
        box = params.get("box")
        aspect = params.get("aspect")
        width = params.get("width")
        height = params.get("height")
        modes = [name for name, value in (("box", box), ("aspect", aspect),
                                          ("width", width), ("height", height)) if value is not None]
        if not modes:
            raise ToolError("nothing to crop: pass box, aspect, or width/height",
                            "pass box=[left, top, right, bottom], or aspect='1:1', or centred "
                            "width/height")
        if box is not None and (aspect is not None or width is not None or height is not None):
            raise ToolError("box cannot be combined with aspect/width/height",
                            "pass box alone, or aspect/width/height alone (centred)")

        im, src, src_bytes = _open_decoded(params.get("path"))
        try:
            src_w, src_h = int(im.size[0]), int(im.size[1])
            before = _before(im, src_bytes)   # before any frame iteration mutates im.mode

            if box is not None:
                if not isinstance(box, (list, tuple)) or len(box) != 4:
                    raise ToolError(f"box must be [left, top, right, bottom], got {box!r}",
                                    "pass four integers, e.g. box=[0, 0, 800, 600]")
                try:
                    left = _whole_int(box[0], "box[0]", 0)
                    top = _whole_int(box[1], "box[1]", 0)
                    right = _whole_int(box[2], "box[2]", 0)
                    bottom = _whole_int(box[3], "box[3]", 0)
                except ToolError:
                    raise
                except (TypeError, ValueError):
                    raise ToolError(f"box entries must be integers, got {box!r}",
                                    "pass four integers, e.g. box=[0, 0, 800, 600]")
                if not (0 <= left < right and 0 <= top < bottom):
                    raise ToolError(
                        f"box {[left, top, right, bottom]} is degenerate: it needs left < right and top < bottom",
                        "pass four integers with left < right and top < bottom, "
                        f"e.g. box=[0, 0, {src_w}, {src_h}]",
                    )
                if right > src_w or bottom > src_h:
                    raise ToolError(
                        f"box {[left, top, right, bottom]} is outside the image bounds {src_w}x{src_h}",
                        f"Pillow would silently pad the missing area with black, so this is refused: "
                        f"keep 0 <= left < right <= {src_w} and 0 <= top < bottom <= {src_h}",
                    )
            elif aspect is not None:
                text = str(aspect).strip()
                parts = text.split(":")
                if len(parts) != 2:
                    raise ToolError(f"aspect must look like '16:9', got {aspect!r}",
                                    "pass aspect='1:1', '16:9', '4:3', …")
                try:
                    ar_w, ar_h = int(parts[0]), int(parts[1])
                except ValueError:
                    raise ToolError(f"aspect must look like '16:9', got {aspect!r}",
                                    "pass integer ratios, e.g. aspect='16:9'")
                if ar_w <= 0 or ar_h <= 0:
                    raise ToolError(f"aspect components must be positive, got {aspect!r}",
                                    "pass aspect='1:1', '16:9', '4:3', …")
                if src_w * ar_h > src_h * ar_w:
                    crop_h = src_h
                    crop_w = max(1, int(src_h * ar_w / ar_h))
                else:
                    crop_w = src_w
                    crop_h = max(1, int(src_w * ar_h / ar_w))
                left = (src_w - crop_w) // 2
                top = (src_h - crop_h) // 2
                right, bottom = left + crop_w, top + crop_h
            else:
                if width is not None and height is not None:
                    crop_w, crop_h = _whole_int(width, "width"), _whole_int(height, "height")
                    if crop_w > src_w or crop_h > src_h:
                        raise ToolError(f"cannot crop {crop_w}x{crop_h} out of {src_w}x{src_h}",
                                        "a crop cannot create pixels; pass smaller dimensions")
                    left, top = (src_w - crop_w) // 2, (src_h - crop_h) // 2
                    right, bottom = left + crop_w, top + crop_h
                elif width is not None:
                    crop_w = _whole_int(width, "width")
                    if crop_w > src_w:
                        raise ToolError(f"cannot crop width {crop_w} out of {src_w}",
                                        "pass a width no larger than the source")
                    left, right = (src_w - crop_w) // 2, (src_w - crop_w) // 2 + crop_w
                    top, bottom = 0, src_h
                else:
                    crop_h = _whole_int(height, "height")
                    if crop_h > src_h:
                        raise ToolError(f"cannot crop height {crop_h} out of {src_h}",
                                        "pass a height no larger than the source")
                    top, bottom = (src_h - crop_h) // 2, (src_h - crop_h) // 2 + crop_h
                    left, right = 0, src_w

            target, replaced, fmt = _target_for(im, src, params.get("output_path"), "crop", None,
                                                params.get("overwrite", False),
                                                params.get("confirm", False))
            frames, durations = _frames_of(im)
            result_frames = [frame.crop((left, top, right, bottom)) for frame in frames]
            result_frames, extra, frames_written, frames_dropped, frame_notes = _animation_extra(
                fmt, result_frames, durations, im.info.get("loop", 0))
            crop_notes: List[str] = []

            def _write_crop(fh: Any) -> None:
                meta = _save_with_meta(result_frames[0], im, fh, fmt, True, extra_kwargs=extra)
                crop_notes.extend(meta)

            size = _write_atomic(target, _write_crop,
                                 bool(params.get("overwrite")), bool(params.get("confirm")),
                                 expect={"size": result_frames[0].size, "frames": frames_written},
                                 release=lambda: _release_input(im))
            after = _after_from_disk(target, size, fmt)
            notes = [f"box=({left}, {top}, {right}, {bottom})",
                     _orientation_note(im, "crop")]
            notes.extend(frame_notes)
            notes.extend(_format_notes(im, target, fmt))
            notes.extend(crop_notes)
            im.close()
            return _ok({"input": str(src), "output": str(target), "before": before,
                        "after": {k: v for k, v in after.items() if k != "path"},
                        "replaced": replaced, "frames_written": frames_written,
                        "frames_dropped": frames_dropped, "notes": notes})
        except ToolError:
            im.close()
            raise
        except Exception as exc:
            im.close()
            raise ToolError(f"{type(exc).__name__}: {exc}",
                            "unexpected failure while cropping; check the box and the arguments")
    except Exception as exc:
        return _err(exc)


def _save_with_meta(result: Any, source: Any, fh: Any, fmt: str, geometry_quality: bool,
                    exif_override: Any = None, extra_kwargs: Optional[Dict[str, Any]] = None) -> List[str]:
    """Save ``result`` to ``fh`` carrying ``source``'s metadata; returns notes."""
    kwargs, notes, dropped, _ = _save_kwargs(source, fmt, geometry_quality=geometry_quality,
                                             exif_override=exif_override)
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    if dropped:
        notes.append("metadata dropped by target format: " + ", ".join(sorted(set(dropped))))
    result.save(fh, format=fmt, **kwargs)
    return notes


def image_rotate(**params: Any) -> str:
    try:
        angle = params.get("angle")
        auto_orient = bool(params.get("auto_orient", True))
        expand = bool(params.get("expand", True))
        resample_name = params.get("resample", "bicubic")
        background = params.get("background")

        im, src, src_bytes = _open_decoded(params.get("path"))
        try:
            notes: List[str] = []
            orientation_applied = False
            original_orientation = im.getexif().get(_EXIF_ORIENTATION)
            source_before = _before(im, src_bytes)   # captured before any transpose

            target, replaced, fmt = _target_for(im, src, params.get("output_path"), "rotate", None,
                                                params.get("overwrite", False),
                                                params.get("confirm", False))
            notes.extend(_format_notes(im, target, fmt))
            if auto_orient and original_orientation not in (None, 1):
                im = ImageOps.exif_transpose(im)  # returns a copy; never in_place=True
                orientation_applied = True
                notes.append(f"EXIF orientation {original_orientation} applied")

            if angle is None:
                if not orientation_applied:
                    im.close()
                    if original_orientation not in (None, 1):
                        raise ToolError(
                            f"nothing to do: the file's EXIF orientation is {original_orientation} "
                            f"but auto_orient=false",
                            "pass auto_orient=true to normalise it, or an explicit angle",
                        )
                    raise ToolError("nothing to do: no angle and no EXIF orientation to apply",
                                    "pass angle=90/180/270 (or any degree value), or set auto_orient "
                                    "for a file whose EXIF orientation is 1")
                numeric_angle = 0.0
            else:
                try:
                    numeric_angle = float(angle)
                except (TypeError, ValueError):
                    raise ToolError(f"angle must be a number, got {angle!r}",
                                    "pass angle=90, 180, 270 or any degree value")
                numeric_angle = numeric_angle % 360.0

            if numeric_angle == 0.0 and not orientation_applied:
                im.close()
                return _ok({"input": str(src), "output": None, "written": False,
                            "notes": ["angle is 0 and no EXIF orientation to apply; nothing to write"],
                            "before": source_before})

            frames, durations = _frames_of(im)
            arbitrary = numeric_angle not in (0.0, 90.0, 180.0, 270.0)
            resample = _pick_resample(resample_name) if arbitrary else None
            result_frames: List[Any] = []
            fill_text: Any = None
            fill_note: Optional[str] = None
            for index, frame in enumerate(frames):
                if numeric_angle == 0.0:
                    result_frames.append(frame.copy())
                elif not arbitrary:
                    transpose = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
                                 270: Image.Transpose.ROTATE_270}[int(numeric_angle)]
                    result_frames.append(frame.transpose(transpose))
                else:
                    work, fill, note = _rotate_fill(frame, background)
                    if index == 0:
                        fill_text = "transparent" if fill in ((0, 0, 0, 0), (0, 0)) else fill
                        fill_note = note
                    result_frames.append(work.rotate(numeric_angle, resample=resample, expand=expand,
                                                     fillcolor=fill))
            result_frames, extra, frames_written, frames_dropped, frame_notes = _animation_extra(
                fmt, result_frames, durations, im.info.get("loop", 0))
            if numeric_angle == 0.0:
                notes.append("no rotation applied: only the EXIF orientation was normalised")
            elif not arbitrary:
                notes.append(f"angle {int(numeric_angle)} applied by exact transpose (no resampling)")
            else:
                notes.append(f"angle {numeric_angle:g} applied with resample="
                             f"{str(resample_name).lower()}, expand={expand}, fill={fill_text}")
                if fill_note:
                    notes.append(fill_note)
            notes.extend(frame_notes)

            # image_rotate ALWAYS clears the orientation tag where the target can carry EXIF.
            exif = im.getexif()
            exif[_EXIF_ORIENTATION] = 1
            kwargs, meta_notes, dropped, _ = _save_kwargs(im, fmt, geometry_quality=True, exif_override=exif)
            kwargs.update(extra)
            notes.extend(meta_notes)
            if dropped:
                notes.append("metadata dropped by target format: " + ", ".join(sorted(set(dropped))))
            size = _write_atomic(target, lambda fh: result_frames[0].save(fh, format=fmt, **kwargs),
                                 bool(params.get("overwrite")), bool(params.get("confirm")),
                                 expect={"size": result_frames[0].size, "frames": frames_written},
                                 release=lambda: _release_input(im))
            after = _after_from_disk(target, size, fmt)
            tag_cleared = fmt in FORMAT_METADATA_SUPPORT["exif"]
            notes.append("orientation tag cleared (written as 1)" if tag_cleared
                         else f"orientation tag not written: {fmt} cannot carry EXIF")
            im.close()
            return _ok({"input": str(src), "output": str(target), "before": source_before,
                        "after": {k: v for k, v in after.items() if k != "path"},
                        "replaced": replaced, "orientation_applied": orientation_applied,
                        "orientation_tag_cleared": tag_cleared, "frames_written": frames_written,
                        "frames_dropped": frames_dropped, "notes": notes})
        except ToolError:
            im.close()
            raise
        except Exception as exc:
            im.close()
            raise ToolError(f"{type(exc).__name__}: {exc}",
                            "unexpected failure while rotating; check angle/resample/background")
    except Exception as exc:
        return _err(exc)


def image_convert(**params: Any) -> str:
    try:
        wanted = params.get("format")
        if wanted is None:
            raise ToolError("no target format given", f"pass format=<{'|'.join(SAVE_FORMATS)}>")
        fmt = str(wanted).strip().upper()
        aliases = {"JPG": "JPEG", "TIF": "TIFF"}
        fmt = aliases.get(fmt, fmt)
        if fmt not in SAVE_FORMATS:
            raise ToolError(f"unsupported target format: {wanted!r}",
                            f"Pillow 12.3.0 can save {'/'.join(SAVE_FORMATS)}; HEIC/HEIF is out of "
                            f"scope for v1 (no pip install is suggested)")
        flatten = bool(params.get("flatten", False))
        background = _parse_color(params.get("background"))

        im, src, src_bytes = _open_decoded(params.get("path"))
        try:
            before = _before(im, src_bytes)   # before frame iteration mutates im.mode
            target, replaced, fmt = _target_for(im, src, params.get("output_path"), "convert", fmt,
                                                params.get("overwrite", False),
                                                params.get("confirm", False))
            alpha = _alpha_present(im)
            # Settle the frame count before materialising anything: a still target writes the first
            # frame only, so decoding the other 59 of a 60-frame GIF would be work thrown away.
            source_frames = _frame_count(im)
            animate = fmt in _save_all_formats() and source_frames > 1
            frames, durations = _frames_of(im, first_only=not animate)
            loop = im.info.get("loop", 0)
            notes: List[str] = []
            notes.extend(_format_notes(im, target, fmt))

            if fmt in ("JPEG", "BMP", "GIF") and alpha:
                if not flatten:
                    im.close()
                    raise ToolError(
                        f"target {fmt} cannot carry alpha and flatten=true was not given",
                        f"pass flatten=true to composite onto background (default white), or convert "
                        f"to PNG/WebP instead",
                    )
                frames = [_flatten_frame(f, background) for f in frames]
                notes.append(f"alpha flattened onto rgb{background}")
            elif fmt == "JPEG":
                frames = [_to_jpeg_mode(f) for f in frames]
            elif fmt == "BMP":
                frames = [f if f.mode in ("1", "L", "P", "RGB") else f.convert("RGB") for f in frames]

            animate = fmt in _save_all_formats() and len(frames) > 1
            if animate and fmt == "PNG":
                frames = [f.convert("RGBA") for f in frames]  # P-mode frames break APNG saving
            if animate:
                frames_written, frames_dropped = len(frames), 0
            else:
                frames_written, frames_dropped = 1, max(0, source_frames - 1)

            extra: Dict[str, Any] = {}
            if animate:
                extra = {"save_all": True, "append_images": frames[1:],
                         "duration": [int(d) if d else 100 for d in durations] or 100,
                         "loop": loop}
                notes.append(f"animated target: {frames_written} frames written, "
                             f"duration={extra['duration'] if isinstance(extra['duration'], int) else 'per-frame'}, "
                             f"loop={loop}")
            elif frames_dropped:
                notes.append(f"frames_dropped={frames_dropped} ({fmt} does not support multi-frame "
                             f"images via Pillow's save_all in 12.3.0; the first frame was written and "
                             f"the dropped frames were neither decoded nor validated)")
            orientation = im.getexif().get(_EXIF_ORIENTATION)
            dropped_meta: List[str] = []

            def _write(fh: Any) -> None:
                kwargs, meta_notes, dropped, _ = _save_kwargs(
                    im, fmt, quality=params.get("quality"), effort=params.get("effort"),
                    strip_metadata=bool(params.get("strip_metadata", False)))
                kwargs.update(extra)
                notes.extend(meta_notes)
                dropped_meta.extend(dropped)
                frames[0].save(fh, format=fmt, **kwargs)

            size = _write_atomic(target, _write, bool(params.get("overwrite")),
                                 bool(params.get("confirm")),
                                 expect={"size": frames[0].size, "frames": frames_written,
                                         "strip": bool(params.get("strip_metadata", False))},
                                 release=lambda: _release_input(im))
            if dropped_meta:
                notes.append("metadata dropped by target format: " + ", ".join(sorted(set(dropped_meta))))
            if orientation not in (None, 1) and "exif" in dropped_meta:
                notes.append(f"source had EXIF orientation {orientation} and {fmt} cannot carry it: "
                             f"viewers may show the result rotated differently")
            if bool(params.get("strip_metadata", False)):
                notes.append("strip_metadata=true: EXIF/ICC/DPI were not written")
            after = _after_from_disk(target, size, fmt)
            im.close()
            return _ok({"input": str(src), "output": str(target), "before": before,
                        "after": {k: v for k, v in after.items() if k != "path"},
                        "replaced": replaced, "frames_written": frames_written,
                        "frames_dropped": frames_dropped,
                        "frame_durations": [int(d) if d else None for d in durations],
                        "loop": loop, "notes": notes})
        except ToolError:
            im.close()
            raise
        except Exception as exc:
            im.close()
            message = str(exc)
            if "cannot write mode" in message:
                hint = (f"the {fmt} encoder cannot write mode {before.get('mode')} — convert to a "
                        f"format that carries this mode first (JPEG for CMYK/RGB, TIFF for I;16/F/LAB)")
            else:
                hint = "unexpected failure while converting; check the target format and arguments"
            raise ToolError(f"{type(exc).__name__}: {message}", hint)
    except Exception as exc:
        return _err(exc)


def image_optimize(**params: Any) -> str:
    try:
        im, src, src_bytes = _open_source(params.get("path"))   # header-only: draft() needs the dimensions first
        try:
            before = _before(im, src_bytes)   # before frame iteration mutates im.mode
            if (im.format or "").upper() not in SAVE_FORMATS:
                im.close()
                raise ToolError(
                    f"cannot re-encode {(im.format or '?')} input: image_optimize keeps the input format",
                    "convert it first with the image_convert tool (PNG/JPEG/WEBP/TIFF/GIF/AVIF/BMP)",
                )
            target, replaced, fmt = _target_for(im, src, params.get("output_path"), "optimize", None,
                                                params.get("overwrite", False),
                                                params.get("confirm", False))
            notes: List[str] = []
            notes.extend(_format_notes(im, target, fmt))
            strip = bool(params.get("strip_metadata", False))
            max_dimension = params.get("max_dimension")
            limit: Optional[int] = None
            tgt: Optional[Tuple[int, int]] = None
            if max_dimension is not None:
                limit = _whole_int(max_dimension, "max_dimension")
                longest = max(im.size)
                if longest > limit:
                    scale = limit / float(longest)
                    tgt = (max(1, int(round(im.size[0] * scale))),
                           max(1, int(round(im.size[1] * scale))))
            drafted = False
            if tgt is not None and fmt == "JPEG" and im.mode in ("RGB", "L"):
                try:
                    full_size = im.size
                    im.draft(im.mode, tgt)   # decode only as much as the target needs
                    drafted = im.size != full_size   # draft() is a no-op below a 2x reduction
                except Exception:
                    pass
            _decode(im)   # decoded AFTER draft(), so draft() really does cut the decode
            frames, durations = _frames_of(im)
            result_frames = frames
            resized = False
            if tgt is not None:
                result_frames = [f.resize(tgt, resample=Image.Resampling.LANCZOS) for f in frames]
                resized = True
                before_w, before_h = before["dimensions"]
                notes.append(f"max_dimension={limit}: resized {before_w}x{before_h} -> "
                             f"{result_frames[0].size[0]}x{result_frames[0].size[1]} (Lanczos)")
            elif limit is not None:
                notes.append(f"max_dimension={limit}: source is already within the limit; not resized")
            if drafted:
                notes.append("JPEG draft decode used: decoded at a reduced scale before resizing")

            result_frames, extra, frames_written, frames_dropped, frame_notes = _animation_extra(
                fmt, result_frames, durations, im.info.get("loop", 0))
            notes.extend(frame_notes)

            if strip:
                notes.append("strip_metadata=true: EXIF/ICC/DPI were not written")

            def _write(fh: Any) -> None:
                kwargs, meta_notes, dropped, _ = _save_kwargs(
                    im, fmt, quality=params.get("quality"), effort=params.get("effort"),
                    strip_metadata=strip)
                kwargs.update(extra)
                notes.extend(meta_notes)
                if dropped:
                    notes.append("metadata dropped by target format: " + ", ".join(sorted(set(dropped))))
                result_frames[0].save(fh, format=fmt, **kwargs)

            size = _write_atomic(target, _write, bool(params.get("overwrite")),
                                 bool(params.get("confirm")),
                                 expect={"size": result_frames[0].size, "frames": frames_written,
                                         "strip": strip},
                                 release=lambda: _release_input(im))
            after = _after_from_disk(target, size, fmt)
            if size > src_bytes:
                notes.append(f"output is {size - src_bytes:,} bytes LARGER than the input "
                             f"({src_bytes:,}); lower quality or try another format")
            elif size == src_bytes:
                notes.append(f"re-encoded to exactly {size:,} bytes — nothing was saved, so this "
                             f"format/effort combination only cost time; a lower effort would be "
                             f"faster for the same result")
            else:
                saved = src_bytes - size
                notes.append(f"re-encoded: {src_bytes:,} -> {size:,} bytes ({saved:,} saved, "
                             f"{100.0 * saved / src_bytes:.1f}% smaller)")
            im.close()
            return _ok({"input": str(src), "output": str(target), "before": before,
                        "after": {k: v for k, v in after.items() if k != "path"},
                        "replaced": replaced, "resized": resized, "stripped": strip,
                        "frames_written": frames_written, "frames_dropped": frames_dropped,
                        "notes": notes})
        except ToolError:
            im.close()
            raise
        except Exception as exc:
            im.close()
            raise ToolError(f"{type(exc).__name__}: {exc}",
                            "unexpected failure while optimizing; check quality/effort/max_dimension")
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------- frame helpers

def _whole_int(value: Any, label: str, minimum: int = 1) -> int:
    """A strict integer parameter: reject bools, NaN and fractional floats."""
    if isinstance(value, bool):
        raise ToolError(f"{label} must be a number, got {value!r}", f"pass {label}=<pixels>")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{label} must be an integer, got {value!r}", f"pass {label}=<pixels>")
    if not math.isfinite(numeric) or numeric != int(numeric):
        raise ToolError(f"{label} must be a whole number, got {value!r}", f"pass {label}=<pixels>")
    result = int(numeric)
    if result < minimum:
        wanted = f"> {minimum - 1}" if minimum == 1 else f">= {minimum}"
        raise ToolError(f"{label} must be {wanted}, got {value!r}",
                        f"pass {label}=<pixels> (positive)")
    return result


def _rotate_fill(im: Any, background: Any) -> Tuple[Any, Any, Optional[str]]:
    """Return (image to rotate, fillcolor, note) with the fill shaped for the image's mode.

    Shapes measured on Pillow 12.3.0: int for L/1/I/I;16, float for F, 2-tuple for LA, 3-tuple for
    RGB/CMYK/LAB/YCbCr/plain-P, 4-tuple for RGBA/RGBa/La. A P image with palette transparency is
    rotated as RGBA, because a transparent fill cannot be added to an RGB palette.
    """
    rgb = _parse_color(background)
    mode = im.mode
    if mode in ("RGBA", "RGBa", "La"):
        return im, ((0, 0, 0, 0) if background is None else rgb + (255,)), None
    if mode == "LA":
        return im, ((0, 0) if background is None else (rgb[0], 255)), None
    if mode in ("L", "1", "I", "I;16", "I;16B", "I;16L", "I;16N"):
        return im, rgb[0], None
    if mode == "F":
        return im, float(rgb[0]), None
    if mode == "P" and "transparency" in im.info:
        if background is None:
            return (im.convert("RGBA"), (0, 0, 0, 0),
                    "palette transparency preserved: the image was rotated as RGBA")
        return (im.convert("RGBA"), rgb + (255,),
                "palette transparency preserved: the image was rotated as RGBA")
    return im, rgb, None


def _orientation_note(im: Any, operation: str) -> str:
    """Honest orientation note for a geometry write (Pillow consumes a TIFF's tag at load)."""
    consumed = getattr(im, "_hermes_tiff_orientation", None)
    if consumed not in (None, 1):
        return (f"EXIF orientation {consumed} was applied by Pillow's TIFF reader at load; the "
                f"written file carries no orientation tag (its pixels are upright)")
    return f"orientation tag preserved (a {operation} does not change pixel orientation)"


def _animation_extra(fmt: str, frames: List[Any], durations: List[Optional[int]], loop: Any
                     ) -> Tuple[List[Any], Dict[str, Any], int, int, List[str]]:
    """save_all kwargs + frame accounting for a possibly animated write.

    Returns ``(frames_to_write, extra_kwargs, frames_written, frames_dropped, notes)``.
    """
    if len(frames) > 1 and fmt in _save_all_formats():
        if fmt == "PNG":   # P-mode frames break APNG saving
            frames = [f if f.mode in ("RGB", "RGBA") else f.convert("RGBA") for f in frames]
        duration = [int(d) if d else 100 for d in durations]
        if len(duration) != len(frames):
            duration = (duration + [100] * len(frames))[:len(frames)]
        extra: Dict[str, Any] = {"save_all": True, "append_images": frames[1:],
                                 "duration": duration or 100,
                                 "loop": loop if isinstance(loop, int) else 0}
        return frames, extra, len(frames), 0, [f"animated input: {len(frames)} frames written"]
    dropped = max(0, len(frames) - 1)
    notes: List[str] = []
    if dropped:
        notes.append(f"frames_dropped={dropped} ({fmt} cannot take multi-frame images via Pillow's "
                     f"save_all in 12.3.0; the first frame was written)")
    return frames, {}, 1, dropped, notes


def _frame_count(im: Any) -> int:
    """How many frames the source file holds (1 for a still image)."""
    return max(1, int(getattr(im, "n_frames", 1) or 1))


def _frames_of(im: Any, first_only: bool = False) -> Tuple[List[Any], List[Optional[int]]]:
    """Copy the frames to write, plus one duration per frame returned.

    ``first_only`` skips the frame walk entirely: a still target writes the first frame, so the
    remaining frames are never decoded and ``frame_durations`` describes only what was written
    (``frames_dropped`` still reports how many source frames that left behind).
    """
    frames: List[Any] = []
    durations: List[Optional[int]] = []
    if int(getattr(im, "n_frames", 1) or 1) > 1:
        if first_only:
            # Take the first frame through Pillow's own iterator and stop: it seeks to the format's
            # first frame (a multi-layer PSD starts at 1, not 0) and the walk itself is the cost, so
            # iterating one step avoids it without assuming frame 0 exists.
            frames.append(next(iter(ImageSequence.Iterator(im))).copy())
            durations.append(im.info.get("duration"))
        else:
            for frame in ImageSequence.Iterator(im):
                frames.append(frame.copy())
                durations.append(frame.info.get("duration"))
    else:
        frames.append(im.copy())
        durations.append(im.info.get("duration"))
    return frames, durations


def _flatten_frame(frame: Any, rgb: Tuple[int, int, int]) -> Any:
    """Composite an alpha-bearing frame onto a solid background (never ``convert('RGB')``)."""
    rgba = frame.convert("RGBA")
    base = Image.new("RGB", frame.size, rgb)
    base.paste(rgba, mask=rgba.split()[3])
    return base


def _to_jpeg_mode(frame: Any) -> Any:
    if frame.mode in ("1", "L", "RGB", "CMYK", "YCbCr"):
        return frame
    return frame.convert("RGB")
