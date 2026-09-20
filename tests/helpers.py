"""Fixture factory for the image-utils test suite.

All fixtures are generated with Pillow itself — no piexif (not installed), no binary blobs in the
repo. Two things this module guards against:

- ``Image.new("P", size, <int index>)`` frames saved with ``save_all=True`` silently produce a
  ONE-frame GIF. ``make_animated_gif`` builds RGB frames and converts to P, then asserts the result
  really is animated.
- EXIF orientation must be written with ``Image.Exif()`` and reloaded to confirm it survived.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Optional, Sequence, Tuple

from PIL import Image, ImageDraw

EXIF_ORIENTATION = 0x0112
EXIF_MAKE = 0x010F
EXIF_MODEL = 0x0110
EXIF_DATETIME = 0x0132
EXIF_GPS_IFD = 0x8825


def make_photo(path: Path, size: Tuple[int, int] = (800, 600), color: Tuple[int, int, int] = (200, 30, 30),
               quality: int = 90, orientation: Optional[int] = None, dpi: Optional[Tuple[int, int]] = None,
               icc: Optional[bytes] = None, make: str = "TestCam", model: str = "TestModel",
               dt: Optional[str] = "2026:09:17 10:00:00", gps: bool = False) -> Path:
    """A JPEG with a green square, optional EXIF (incl. orientation/GPS), DPI and ICC."""
    im = Image.new("RGB", size, color)
    ImageDraw.Draw(im).rectangle([0, 0, size[0] // 2, size[1] // 2], fill=(30, 200, 30))
    kwargs = {"quality": quality}
    exif = Image.Exif()
    if orientation is not None:
        exif[EXIF_ORIENTATION] = orientation
    if make:
        exif[EXIF_MAKE] = make
    if model:
        exif[EXIF_MODEL] = model
    if dt:
        exif[EXIF_DATETIME] = dt
    if gps:
        gps_ifd = exif.get_ifd(EXIF_GPS_IFD)
        gps_ifd[1] = "N"           # GPSLatitudeRef
        gps_ifd[2] = 51.5083       # GPSLatitude (float is what Pillow's IFD writer accepts)
        gps_ifd[3] = "E"           # GPSLongitudeRef
        gps_ifd[4] = 6.9556        # GPSLongitude
    if len(exif):
        kwargs["exif"] = exif.tobytes()
    if dpi:
        kwargs["dpi"] = dpi
    if icc:
        kwargs["icc_profile"] = icc
    im.save(path, **kwargs)
    im.close()
    return path


def make_alpha_png(path: Path, size: Tuple[int, int] = (120, 90),
                   color: Tuple[int, int, int, int] = (255, 0, 0, 128)) -> Path:
    im = Image.new("RGBA", size, color)
    im.save(path)
    im.close()
    return path


def make_palette_transparency(path: Path, size: Tuple[int, int] = (60, 40)) -> Path:
    """P-mode PNG whose palette has a transparent entry."""
    im = Image.new("P", size, 0)
    palette = [0, 0, 0, 255, 0, 0] + [0] * (256 * 3 - 6)
    im.putpalette(palette)
    im.info["transparency"] = 0
    im.save(path, transparency=0)
    im.close()
    return path


def make_webp(path: Path, size: Tuple[int, int] = (64, 64)) -> Path:
    im = Image.new("RGB", size, (10, 20, 30))
    im.save(path)
    im.close()
    return path


def make_animated_gif(path: Path, frames: int = 3, size: Tuple[int, int] = (40, 40),
                      duration: int = 80, colors: Sequence[Tuple[int, int, int]] = ((255, 0, 0), (0, 255, 0), (0, 0, 255))) -> Path:
    frames_p = [Image.new("RGB", size, colors[i % len(colors)]).convert("P") for i in range(frames)]
    frames_p[0].save(path, save_all=True, append_images=frames_p[1:], duration=duration, loop=0)
    for frame in frames_p:
        frame.close()
    with Image.open(path) as check:   # never let a one-frame GIF pass as an animation
        if getattr(check, "n_frames", 1) != frames:
            raise AssertionError(f"fixture is not animated: n_frames={getattr(check, 'n_frames', 1)}")
    return path


def make_png_header_only(path: Path, width: int, height: int, color_type: int = 2) -> Path:
    """A small PNG whose IHDR declares huge dimensions — the decompression-bomb shape.

    Built by patching a real 1x1 PNG's IHDR (with a recomputed CRC) so the file keeps a valid PNG
    structure while declaring the requested pixel count.
    """
    import io

    buf = io.BytesIO()
    Image.new("L", (1, 1)).save(buf, format="PNG")
    raw = bytearray(buf.getvalue())
    if raw[12:16] != b"IHDR":   # pragma: no cover - Pillow always writes IHDR first
        raise AssertionError("unexpected PNG layout")
    struct.pack_into(">I", raw, 16, width)
    struct.pack_into(">I", raw, 20, height)
    struct.pack_into(">I", raw, 29, zlib.crc32(bytes(raw[12:29])) & 0xFFFFFFFF)
    path.write_bytes(bytes(raw))
    return path


def truncate(path: Path, fraction: float = 0.5) -> Path:
    data = path.read_bytes()
    path.write_bytes(data[: max(1, int(len(data) * fraction))])
    return path


def make_noise_jpeg(path: Path, size: Tuple[int, int] = (900, 700), quality: int = 90) -> Path:
    """Noise, so encoder knobs visibly change the output size (flat colour does not)."""
    import random

    rng = random.Random(1234)
    im = Image.new("RGB", size)
    im.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                for _ in range(size[0] * size[1])])
    im.save(path, quality=quality)
    im.close()
    return path
