"""Tests for the competition-fix batch (judge action list J-01..J-26).

Every test here pins behaviour that was found broken by the review competition
(reviewer-A.md / reviewer-B.md / devil-advocate.md) or that the judge's action list changed.
They are written against observable behaviour: the JSON envelope, the bytes on disk, leftovers.

    uv run --python 3.11 --with pytest --with 'pillow==12.3.0' --with pyyaml python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402
import tools  # noqa: E402
from PIL import Image  # noqa: E402


def load(raw: str) -> dict:
    return json.loads(raw)


def no_temps(directory: Path) -> None:
    leftovers = [p.name for p in directory.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def make_tiff(path: Path, mode: str = "RGB", size: tuple = (64, 48), frames: int = 1) -> Path:
    if frames > 1:
        pages = [Image.new(mode, size, (10 * i, 20, 30) if mode == "RGB" else 0) for i in range(frames)]
        pages[0].save(path, save_all=True, append_images=pages[1:])
        for page in pages:
            page.close()
    else:
        im = Image.new(mode, size, (60, 90, 120) if mode == "RGB" else 0)
        im.save(path)
        im.close()
    return path


def frames_on_disk(path: Path) -> int:
    with Image.open(path) as im:
        return int(getattr(im, "n_frames", 1) or 1)


# --------------------------------------------------------------- J-01/J-14/J-16 caps and the bomb band

def test_per_frame_cap_refusal_carries_the_header_facts(tmp_path):
    """A 50.4 MP header-only PNG: refused by our cap, and image_info still reports the numbers."""
    src = helpers.make_png_header_only(tmp_path / "hb.png", 8000, 6300)   # 50.4 MP, under Pillow's line
    info = load(tools.image_info(path=str(src)))
    assert "over the 50 MP per-frame cap" in info["error"]
    assert info["width"] == 8000 and info["height"] == 6300        # J-14: facts, not just prose
    assert info["pixels"] == 8000 * 6300 and info["decode_ok"] is False
    out = load(tools.image_resize(path=str(src), percent=50))
    assert out["error"] and "50 MP per-frame cap" in out["error"]


def test_total_cap_refuses_many_frames(tmp_path, monkeypatch):
    """The summed-frames cap, exercised with a tiny budget so a real 3-frame GIF trips it (J-16)."""
    monkeypatch.setattr(tools, "MAX_PIXELS_TOTAL", 1000)
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, size=(40, 40))   # 4800 px total
    info = load(tools.image_info(path=str(src)))
    assert "MP total cap" in info["error"]
    assert info["frames"] == 3 and info["pixels"] == 1600          # J-14 + the real frame count


def test_bomb_warning_band_is_a_structured_refusal(tmp_path):
    """89.5-179 MP: Pillow warns instead of raising; the plugin must refuse, never hand back a warning."""
    src = helpers.make_png_header_only(tmp_path / "band.png", 12000, 10000)     # 120 MP
    info = load(tools.image_info(path=str(src)))
    assert info["error"].startswith("refused:")                    # the bomb branch, not the cap branch
    assert "decompression-bomb guard" in info["how_to_fix"]
    out = load(tools.image_resize(path=str(src), percent=50))
    assert out["error"].startswith("refused:")
    assert "re-run with the same arguments" not in out["how_to_fix"]            # A-1: no useless retry


def test_guard_filters_do_not_leak_across_threads(tmp_path):
    """DA-02: two overlapping guarded regions must not leave an 'error' filter installed process-wide."""
    holder: list = []
    start = threading.Event()

    def worker(delay: float) -> None:
        start.wait(2)
        with tools._guarded():
            time.sleep(delay)
        holder.append(1)

    threads = [threading.Thread(target=worker, args=(d,)) for d in (0.10, 0.01)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(5)
    leaked = [f for f in warnings.filters if f[2] is Image.DecompressionBombWarning and f[0] == "error"]
    assert leaked == [], f"warnings filters leaked: {leaked}"
    src = helpers.make_png_header_only(tmp_path / "hb96.png", 96000000 // 10000, 10000)  # 96 MP
    with warnings.catch_warnings(record=True) as caught:                  # outside any guard: warn only
        warnings.simplefilter("always")
        with Image.open(src) as im:
            assert im.size == (9600, 10000)
    assert any(isinstance(w.message, Image.DecompressionBombWarning) for w in caught)


# --------------------------------------------------------------- J-03/J-04 TIFF round trips

@pytest.mark.parametrize("tool_name,kwargs", [
    ("image_resize", {"percent": 50}),
    ("image_crop", {"box": [0, 0, 32, 24]}),
    ("image_rotate", {"angle": 90}),
])
def test_tiff_round_trip_is_readable(tmp_path, tool_name, kwargs):
    """A-2: the source's structural IFD must not be carried as EXIF, or the output is undecodable."""
    src = make_tiff(tmp_path / "src.tiff", "RGB", (64, 48))
    result = load(getattr(tools, tool_name)(path=str(src), **kwargs))
    assert "error" not in result, result
    out = Path(result["output"])
    with Image.open(out) as im:
        assert im.size == tuple(result["after"]["dimensions"])      # the file declares what we said
        im.load()                                                   # undecodable files raise here
    info = load(tools.image_info(path=str(out)))
    assert info["decode_ok"] is True and info["width"] == result["after"]["dimensions"][0]


def test_tiff_round_trip_keeps_user_tags_but_not_a_stale_orientation(tmp_path):
    """J-03 measured: Pillow applies a TIFF's orientation to the pixels at load and consumes the tag,
    so the output must carry no stale orientation (that would double-rotate) while user tags survive."""
    src = tmp_path / "cam.tiff"
    im = Image.new("RGB", (64, 48), (5, 6, 7))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "TestCam"
    im.save(src, exif=exif.tobytes())
    im.close()
    out = load(tools.image_resize(path=str(src), percent=50))
    assert "error" not in out, out
    with Image.open(out["output"]) as check:
        check.load()
        assert check.getexif().get(0x010F) == "TestCam"          # user tag survives the whitelist
        assert check.getexif().get(0x0112) in (None, 1)          # no stale orientation
    assert any("applied by Pillow's TIFF reader" in note for note in out["notes"])
    # control: a JPEG's orientation tag IS preserved by a resize (its pixels are not transposed)
    jpg = helpers.make_photo(tmp_path / "p.jpg", size=(64, 48), orientation=6)
    jpg_out = load(tools.image_resize(path=str(jpg), percent=50))
    with Image.open(jpg_out["output"]) as check:
        assert check.getexif().get(0x0112) == 6
    assert any("orientation tag preserved" in note for note in jpg_out["notes"])


def test_animated_gif_to_tiff_writes_every_frame(tmp_path):
    """J-04: the multi-page TIFF saver re-reads the file it writes, so the temp handle must be r+w."""
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, size=(40, 40))
    out = load(tools.image_convert(path=str(src), format="TIFF"))
    assert "error" not in out, out
    assert out["frames_written"] == 3 and out["frames_dropped"] == 0
    assert frames_on_disk(Path(out["output"])) == 3


# --------------------------------------------------------------- J-07 animation is not silently dropped

@pytest.mark.parametrize("tool_name,kwargs", [
    ("image_resize", {"percent": 50}),
    ("image_crop", {"box": [0, 0, 20, 20]}),
    ("image_rotate", {"angle": 90}),
    ("image_optimize", {}),
])
def test_animation_survives_every_geometry_tool(tmp_path, tool_name, kwargs):
    """A-4: a 3-frame GIF stayed a 3-frame GIF (or the drop is reported in the envelope)."""
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, size=(40, 40))
    result = load(getattr(tools, tool_name)(path=str(src), **kwargs))
    assert "error" not in result, result
    kept = frames_on_disk(Path(result["output"]))
    if result.get("frames_dropped"):
        assert kept == 1 and "frames_dropped" in " ".join(result["notes"])
    else:
        assert kept == 3 and result.get("frames_written") == 3


def test_in_place_optimize_never_replaces_an_animation_with_a_still(tmp_path):
    """A-4's data loss: overwrite+confirm on a GIF used to leave a single-frame file behind."""
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, size=(40, 40))
    result = load(tools.image_optimize(path=str(src), output_path=str(src),
                                       overwrite=True, confirm=True))
    assert "error" not in result, result
    assert result["replaced"] is True
    assert frames_on_disk(src) == 3
    no_temps(tmp_path)


# --------------------------------------------------------------- J-05 arbitrary-angle rotate, all modes

@pytest.mark.parametrize("mode,container", [
    ("L", "png"), ("1", "png"), ("LA", "png"), ("I;16", "png"), ("F", "tiff"),
    ("CMYK", "jpg"), ("RGB", "tiff"),
])
def test_rotate_arbitrary_angle_works_for_every_mode(tmp_path, mode, container):
    """A-3: the fill colour must match the mode's channel shape, or rotate(45) dies."""
    src = tmp_path / f"src_{mode.replace(';', '_')}.{container}"
    Image.new(mode, (40, 30), 0).save(src)
    out = load(tools.image_rotate(path=str(src), angle=45, background="white"))
    assert "error" not in out, out
    with Image.open(out["output"]) as im:
        im.load()
        assert im.size == tuple(out["after"]["dimensions"])


def test_rotate_palette_transparency_keeps_transparency(tmp_path):
    """A-3: a transparent fill cannot be added to an RGB palette — the plugin must route via RGBA."""
    src = helpers.make_palette_transparency(tmp_path / "p.png", size=(60, 40))
    out = load(tools.image_rotate(path=str(src), angle=45))
    assert "error" not in out, out
    assert out["after"]["mode"] == "RGBA"
    with Image.open(out["output"]) as im:
        assert im.mode == "RGBA" and im.getpixel((0, 0))[3] == 0
    assert "palette transparency preserved" in " ".join(out["notes"])


# --------------------------------------------------------------- J-06 alpha gate + after.mode honesty

def test_bmp_flatten_composites_onto_the_background(tmp_path):
    """A-5: flatten=true with a background must actually composite, and after.mode must be true."""
    src = helpers.make_alpha_png(tmp_path / "a.png", size=(40, 40), color=(255, 0, 0, 128))
    out = load(tools.image_convert(path=str(src), format="BMP", flatten=True, background="black"))
    assert "error" not in out, out
    assert out["after"]["mode"] == "RGB"
    with Image.open(out["output"]) as im:
        assert im.mode == "RGB"
        red, green, blue = im.convert("RGB").getpixel((0, 0))
    assert 120 <= red <= 135 and green == 0 and blue == 0   # 128-alpha red over black, not a naive drop


def test_bmp_and_gif_refuse_silent_alpha_loss(tmp_path):
    src = helpers.make_alpha_png(tmp_path / "a.png", size=(40, 40))
    for fmt in ("BMP", "GIF"):
        refused = load(tools.image_convert(path=str(src), format=fmt))
        assert f"target {fmt} cannot carry alpha" in refused["error"]
        assert "flatten=true" in refused["how_to_fix"]
        ok = load(tools.image_convert(path=str(src), format=fmt, flatten=True, background="white"))
        assert "error" not in ok, ok


def test_after_mode_is_read_back_from_the_file(tmp_path):
    """A-11: a 1-bit PNG written as JPEG is mode L on disk, and the envelope must say so."""
    src = tmp_path / "1bit.png"
    Image.new("1", (40, 40), 1).save(src)
    out = load(tools.image_convert(path=str(src), format="JPEG"))
    assert "error" not in out, out
    assert out["after"]["mode"] == "L"
    assert out["before"]["mode"] == "1"


# --------------------------------------------------------------- J-09/J-13/J-19 parameter truth

def test_resize_rejects_nonfinite_and_absurd_targets(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    for percent in ("nan", float("nan"), 1e400):
        payload = load(tools.image_resize(path=str(src), percent=percent))
        assert "finite" in payload["error"]
    huge = load(tools.image_resize(path=str(src), percent=1000000, allow_upscale=True))
    assert "per-frame cap" in huge["error"]


def test_resize_resample_none_is_lanczos_and_says_so(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(200, 120))
    for index, value in enumerate((None, "", "  ")):
        out = load(tools.image_resize(path=str(src), percent=50, resample=value,
                                      output_path=str(tmp_path / f"out{index}.jpg")))
        assert "error" not in out, out
        assert "resample=lanczos" in " ".join(out["notes"])


def test_resize_rejects_bool_and_fractional_dimensions(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    assert "whole number" in load(tools.image_resize(path=str(src), width=10.9))["error"]
    assert "must be a number" in load(tools.image_resize(path=str(src), width=True))["error"]
    out = load(tools.image_resize(path=str(src), width=50.0))
    assert "error" not in out, out


# --------------------------------------------------------------- J-11/J-15/J-17/J-18/J-20/J-21 texts

def test_strip_metadata_really_strips_icc_and_exif(tmp_path):
    """A-7: PNG/TIFF kept the ICC profile while the note claimed it had been dropped."""
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 60), dpi=(300, 300), icc=b"\x11" * 64)
    as_png = tmp_path / "p.png"
    Image.open(src).save(as_png, icc_profile=b"\x11" * 64)
    out = load(tools.image_optimize(path=str(as_png), strip_metadata=True))
    assert "error" not in out, out
    with Image.open(out["output"]) as im:
        im.load()
        assert not im.info.get("icc_profile")
        assert len(im.getexif()) == 0
        assert im.info.get("dpi") is None
    assert "were not written" in " ".join(out["notes"])


def test_output_suffix_mismatch_is_noted_for_any_suffix(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 60))
    out = load(tools.image_resize(path=str(src), percent=50, output_path=str(tmp_path / "x.heic")))
    assert "error" not in out, out
    assert any("does not match the written format JPEG" in note for note in out["notes"])


def test_forced_png_fallback_is_noted(tmp_path):
    """DA-03: an input format Pillow cannot write falls back to PNG — that must be visible."""
    src = tmp_path / "x.ppm"
    Image.new("RGB", (30, 20), (1, 2, 3)).save(src)
    out = load(tools.image_crop(path=str(src), box=[0, 0, 20, 20]))
    assert "error" not in out, out
    assert any("cannot be written by Pillow" in note for note in out["notes"])
    with Image.open(out["output"]) as im:
        assert im.format == "PNG"


def test_read_only_directory_error_names_the_directory(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 60))
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        out = load(tools.image_resize(path=str(src), percent=50, output_path=str(locked / "x.jpg")))
        assert "not writable" in out["how_to_fix"]
        assert ".tmp" not in out["error"] and ".tmp" not in out["how_to_fix"]
    finally:
        locked.chmod(0o700)


def test_rotate_auto_orient_false_names_the_real_orientation(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 40), orientation=6)
    out = load(tools.image_rotate(path=str(src), auto_orient=False))
    assert "orientation is 6" in out["error"]


def test_rotate_gif_does_not_claim_a_cleared_tag(tmp_path):
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=2, size=(40, 40))
    out = load(tools.image_rotate(path=str(src), angle=90))
    assert "error" not in out, out
    assert out["orientation_tag_cleared"] is False
    assert any("cannot carry EXIF" in note for note in out["notes"])


def test_cmyk_to_png_names_the_mode_in_the_hint(tmp_path):
    src = tmp_path / "c.tiff"
    Image.new("CMYK", (30, 20), 0).save(src)
    out = load(tools.image_convert(path=str(src), format="PNG"))
    assert "cannot write mode CMYK" in out["error"]
    assert "TIFF" in out["how_to_fix"] and "JPEG" in out["how_to_fix"]


# --------------------------------------------------------------- J-10 exclusive publish

def test_exclusive_publish_refuses_an_existing_target(tmp_path):
    target = tmp_path / "out.png"
    target.write_bytes(b"already here")
    tmp = tmp_path / ".out.tmp"
    tmp.write_bytes(b"new bytes")
    with pytest.raises(tools.ToolError) as excinfo:
        tools._publish_exclusive(tmp, target)
    assert "output appeared while writing" in str(excinfo.value)
    assert target.read_bytes() == b"already here"
    tmp.unlink()


def test_atomic_write_leaves_no_temp_and_writes_the_bytes(tmp_path):
    target = tmp_path / "out.bin"
    size = tools._write_atomic(target, lambda fh: fh.write(b"payload"), False, False)
    assert size == 7 and target.read_bytes() == b"payload"
    no_temps(tmp_path)


def test_atomic_write_refuses_a_mismatched_encoder(tmp_path):
    """J-03's guard: a file that reopens at the wrong size is refused instead of published."""
    target = tmp_path / "out.png"

    def liar(fh):
        Image.new("RGB", (10, 10), (0, 0, 0)).save(fh, format="PNG")

    with pytest.raises(tools.ToolError) as excinfo:
        tools._write_atomic(target, liar, False, False, expect={"size": (99, 99)})
    assert "the encoder wrote 10x10" in str(excinfo.value)
    assert not target.exists()
    no_temps(tmp_path)


def test_atomic_write_honours_the_strip_expectation(tmp_path):
    def with_icc(fh):
        Image.new("RGB", (10, 10), (0, 0, 0)).save(fh, format="PNG", icc_profile=b"\x22" * 32)

    with pytest.raises(tools.ToolError) as excinfo:
        tools._write_atomic(tmp_path / "out.png", with_icc, False, False, expect={"strip": True})
    assert "still carries metadata" in str(excinfo.value)
    assert not (tmp_path / "out.png").exists()
    no_temps(tmp_path)


# --------------------------------------------------------------- J-24/J-26 dead code and claims

def test_no_dead_helpers_remain():
    """J-24: the never-called envelope builder and the unused binding are gone."""
    import ast

    source = Path(tools.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef,))}
    assert "_public_result" not in names
    assert "orientation_in_output" not in source


def test_resize_drafts_the_jpeg_before_decoding(tmp_path, monkeypatch):
    """B-4/J-26: draft() must run before the decode, or the reported memory saving never happens."""
    from PIL import JpegImagePlugin

    calls = []
    original = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        calls.append({"size_at_call": tuple(self.size), "decoded_already": not bool(getattr(self, "tile", None))})
        return original(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    src = helpers.make_noise_jpeg(tmp_path / "n.jpg", size=(1200, 900))
    out = load(tools.image_resize(path=str(src), percent=25))
    assert "error" not in out, out
    assert calls, "draft() was never called for a JPEG downscale"
    assert calls[0]["decoded_already"] is False, "draft() ran after the pixels were decoded"
    assert calls[0]["size_at_call"] == (1200, 900)
    assert "draft decode" in " ".join(out["notes"])
