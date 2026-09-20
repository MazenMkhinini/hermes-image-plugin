"""Behaviour tests for the Hermes ``image-utils`` plugin (offline; Pillow-generated fixtures).

Run from the plugin checkout (the Hermes venv has no pytest and must never be modified):

    uv run --python 3.11 --with pytest --with 'pillow==12.3.0' --with pyyaml python -m pytest tests/ -q

Every test asserts observable behaviour (the JSON envelope, the bytes on disk, the leftover temp
files) — never the source text, except in ``test_contracts.py`` where a source scan is the only way
to enforce a rule that the platform cannot.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402
import tools  # noqa: E402
from PIL import Image, ImageFile  # noqa: E402


def load(raw: str) -> dict:
    return json.loads(raw)


def no_temps(directory: Path) -> None:
    leftovers = [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"temp files left behind: {leftovers}"


# --------------------------------------------------------------------------- image_info

def test_info_reports_facts_and_gps_presence_only(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(120, 80), orientation=6, gps=True,
                             dpi=(300, 300), icc=b"\x00" * 128)
    info = load(tools.image_info(path=str(src)))
    assert info["width"] == 120 and info["height"] == 80
    assert info["format"] == "JPEG" and info["mode"] == "RGB"
    assert info["frames"] == 1 and info["animated"] is False
    assert info["dpi"] == [300, 300]
    assert info["icc_profile_present"] is True and info["icc_profile_bytes"] == 128
    assert info["exif"]["orientation"] == 6
    assert info["exif"]["camera_make"] == "TestCam"
    assert info["exif"]["datetime"] == "2026:09:17 10:00:00"
    assert info["exif"]["gps_present"] is True
    # presence only: no coordinates anywhere in the payload
    assert "51.5" not in json.dumps(info)
    assert info["decode_ok"] is True
    assert info["pillow_available"] is True


def test_info_animated_gif(tmp_path):
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, duration=80)
    info = load(tools.image_info(path=str(src)))
    assert info["frames"] == 3 and info["animated"] is True
    assert info["frame_durations"] == [80, 80, 80]
    assert info["loop"] == 0


def test_info_missing_zero_byte_and_not_an_image(tmp_path):
    missing = load(tools.image_info(path=str(tmp_path / "nope.png")))
    assert "does not exist" in missing["error"] and "how_to_fix" in missing

    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    assert "empty" in load(tools.image_info(path=str(empty)))["error"]

    text = tmp_path / "notes.png"
    text.write_text("definitely not an image")
    payload = load(tools.image_info(path=str(text)))
    assert "not a readable image" in payload["error"]


def test_info_truncated_file_still_answers_but_says_decode_failed(tmp_path):
    src = helpers.make_photo(tmp_path / "t.jpg", size=(200, 200))
    helpers.truncate(src, 0.4)
    payload = load(tools.image_info(path=str(src)))
    # header facts are present, and the fact that the pixels do not decode is stated
    assert payload["decode_ok"] is False
    assert "decode_error" in payload
    assert payload["format"] == "JPEG"


def test_info_refuses_over_cap_with_the_numbers_and_keeps_byte_facts(tmp_path, monkeypatch):
    src = helpers.make_photo(tmp_path / "big.jpg", size=(400, 300))
    monkeypatch.setattr(tools, "MAX_INPUT_BYTES", 100)
    payload = load(tools.image_info(path=str(src)))
    assert "over the" in payload["error"] and "cap" in payload["error"]
    assert payload["bytes"] == src.stat().st_size


def test_info_decompression_bomb_is_a_structured_refusal(tmp_path):
    bomb = helpers.make_png_header_only(tmp_path / "bomb.png", 20000, 10000)   # 200 MP
    payload = load(tools.image_info(path=str(bomb)))
    assert "decompression bomb" in payload["error"]
    assert payload["how_to_fix"]


def test_bomb_refused_by_the_write_tools_too(tmp_path):
    bomb = helpers.make_png_header_only(tmp_path / "bomb.png", 14000, 14000)   # 196 MP
    payload = load(tools.image_resize(path=str(bomb), percent=50))
    assert "error" in payload and payload["how_to_fix"]
    assert list(tmp_path.iterdir()) == [bomb]   # nothing written


# --------------------------------------------------------------------------- image_resize

def test_resize_percent_and_single_side_preserve_aspect(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(800, 600))
    out = load(tools.image_resize(path=str(src), percent=50))
    assert out["after"]["dimensions"] == [400, 300]
    assert out["output"].endswith("p_resize.jpg")
    assert out["before"]["dimensions"] == [800, 600]
    assert Path(out["output"]).exists()
    with Image.open(out["output"]) as im:
        assert im.size == (400, 300)
    no_temps(tmp_path)

    out2 = load(tools.image_resize(path=str(src), height=150,
                                   output_path=str(tmp_path / "by_height.jpg")))
    assert out2["after"]["dimensions"] == [200, 150]


def test_resize_refuses_up_and_distort_and_bad_values(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    assert "upscale" in load(tools.image_resize(path=str(src), width=500))["error"]
    assert "ambiguous" in load(tools.image_resize(path=str(src), width=50, height=50))["error"]
    assert "> 0" in load(tools.image_resize(path=str(src), width=0))["error"]
    assert "> 0" in load(tools.image_resize(path=str(src), percent=-10))["error"]
    assert "nothing to do" in load(tools.image_resize(path=str(src)))["error"]
    assert not (tmp_path / "p_resize.jpg").exists()


def test_resize_allow_flags_work(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    up = load(tools.image_resize(path=str(src), width=200, allow_upscale=True))
    assert up["after"]["dimensions"] == [200, 200]
    dist = load(tools.image_resize(path=str(src), width=50, height=80, allow_distort=True,
                                   output_path=str(tmp_path / "distorted.jpg")))
    assert dist["after"]["dimensions"] == [50, 80]


def test_resize_same_size_writes_nothing(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    out = load(tools.image_resize(path=str(src), percent=100))
    assert out["written"] is False and out["output"] is None
    assert list(tmp_path.iterdir()) == [src]


def test_resize_keeps_metadata_and_orientation(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(200, 200), orientation=6, dpi=(300, 300),
                             icc=b"\x11" * 64)
    out = load(tools.image_resize(path=str(src), percent=50))
    with Image.open(out["output"]) as im:
        assert im.getexif().get(0x0112) == 6            # a resize does not rotate: tag stays valid
        assert im.info.get("dpi") == (300, 300)
        assert len(im.info.get("icc_profile", b"")) == 64


# --------------------------------------------------------------------------- image_crop

def test_crop_box_and_aspect_and_centred(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(800, 600))
    boxed = load(tools.image_crop(path=str(src), box=[0, 0, 400, 300]))
    assert boxed["after"]["dimensions"] == [400, 300]

    square = load(tools.image_crop(path=str(src), aspect="1:1",
                                   output_path=str(tmp_path / "sq.jpg")))
    assert square["after"]["dimensions"] == [600, 600]

    wide = load(tools.image_crop(path=str(src), aspect="16:9",
                                 output_path=str(tmp_path / "wide.jpg")))
    assert wide["after"]["dimensions"] == [800, 450]

    centred = load(tools.image_crop(path=str(src), width=200, height=100,
                                    output_path=str(tmp_path / "centre.jpg")))
    assert centred["after"]["dimensions"] == [200, 100]
    assert "box=(300, 250, 500, 350)" in " ".join(centred["notes"])

    strip = load(tools.image_crop(path=str(src), height=100,
                                  output_path=str(tmp_path / "strip.jpg")))
    assert strip["after"]["dimensions"] == [800, 100]


def test_crop_refuses_out_of_bounds_and_oversize(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(800, 600))
    oob = load(tools.image_crop(path=str(src), box=[0, 0, 900, 300]))
    assert "outside the image bounds 800x600" in oob["error"]
    assert "black" in oob["how_to_fix"]        # explains why it is refused rather than padded
    assert "cannot crop" in load(tools.image_crop(path=str(src), width=900))["error"]
    assert "degenerate" in load(tools.image_crop(path=str(src), box=[0, 0, 0, 10]))["error"]
    assert "look like" in load(tools.image_crop(path=str(src), aspect="square"))["error"]
    assert not (tmp_path / "p_crop.jpg").exists()


def test_crop_rejects_two_modes_and_empty_call(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 60))
    assert "cannot be combined" in load(tools.image_crop(path=str(src), box=[0, 0, 10, 10], aspect="1:1"))["error"]
    assert "nothing to crop" in load(tools.image_crop(path=str(src)))["error"]


# --------------------------------------------------------------------------- image_rotate

def test_rotate_quarter_turns_are_exact_transposes(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 40), orientation=None)
    out = load(tools.image_rotate(path=str(src), angle=90))
    assert out["after"]["dimensions"] == [40, 80]
    assert out["orientation_tag_cleared"] is True
    with Image.open(out["output"]) as im:
        assert im.getexif().get(0x0112) == 1
    # 180 and 270 come back to the original orientation
    rotated = Path(out["output"])
    again = load(tools.image_rotate(path=str(rotated), angle=270, output_path=str(tmp_path / "back.jpg")))
    assert again["after"]["dimensions"] == [80, 40]


def test_rotate_applies_exif_orientation_and_clears_the_tag(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 40), orientation=6)
    out = load(tools.image_rotate(path=str(src), angle=None))
    assert out["orientation_applied"] is True
    assert out["after"]["dimensions"] == [40, 80]      # 6 = rotate 90 CW on display
    with Image.open(out["output"]) as im:
        assert im.getexif().get(0x0112) == 1
        assert im.getexif().get(0x010F) == "TestCam"   # other tags survive


def test_rotate_arbitrary_angle_expands_and_fills(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 40), orientation=None)
    out = load(tools.image_rotate(path=str(src), angle=45, background="black"))
    assert out["after"]["dimensions"][0] > 80 and out["after"]["dimensions"][1] > 40
    with Image.open(out["output"]) as im:
        assert im.getpixel((0, 0)) == (0, 0, 0)


def test_rotate_alpha_gets_transparent_corners(tmp_path):
    src = helpers.make_alpha_png(tmp_path / "a.png", size=(60, 60))
    out = load(tools.image_rotate(path=str(src), angle=45))
    with Image.open(out["output"]) as im:
        assert im.mode == "RGBA"
        assert im.getpixel((0, 0))[3] == 0


def test_rotate_nothing_to_do(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(40, 40), orientation=None)
    payload = load(tools.image_rotate(path=str(src)))
    assert "nothing to do" in payload["error"]
    payload2 = load(tools.image_rotate(path=str(src), angle=0))
    assert payload2["written"] is False


# --------------------------------------------------------------------------- image_convert

def test_convert_formats_and_metadata_drops(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 80), dpi=(300, 300), icc=b"\x22" * 32)
    webp = load(tools.image_convert(path=str(src), format="webp", quality=70))
    assert webp["output"].endswith("p_convert.webp")
    with Image.open(webp["output"]) as im:
        assert im.format == "WEBP"
        assert im.getexif().get(0x010F) == "TestCam"     # EXIF is carried
    assert any("dpi" in note for note in webp["notes"])  # WebP cannot carry DPI: stated

    bmp = load(tools.image_convert(path=str(src), format="BMP"))
    assert any("exif" in note for note in bmp["notes"])  # BMP drops EXIF: stated
    with Image.open(bmp["output"]) as im:
        assert im.format == "BMP"


def test_convert_alpha_to_jpeg_requires_flatten(tmp_path):
    src = helpers.make_alpha_png(tmp_path / "a.png", color=(255, 0, 0, 128))
    refused = load(tools.image_convert(path=str(src), format="jpeg"))
    assert "alpha" in refused["error"] and "flatten" in refused["how_to_fix"]
    assert not (tmp_path / "a_convert.jpg").exists()

    flat = load(tools.image_convert(path=str(src), format="jpeg", flatten=True))
    with Image.open(flat["output"]) as im:
        assert im.mode == "RGB"
        pixel = im.getpixel((0, 0))                      # composited, NOT naive convert ('RGB')
        assert pixel[0] == 255 and abs(pixel[1] - 127) <= 1 and abs(pixel[2] - 127) <= 1

    black = load(tools.image_convert(path=str(src), format="jpeg", flatten=True,
                                      background="black", output_path=str(tmp_path / "b.jpg")))
    with Image.open(black["output"]) as im:
        pixel = im.getpixel((0, 0))
        assert abs(pixel[0] - 127) <= 1 and pixel[1] == 0 and pixel[2] == 0


def test_convert_palette_transparency_counts_as_alpha(tmp_path):
    src = helpers.make_palette_transparency(tmp_path / "pal.png")
    refused = load(tools.image_convert(path=str(src), format="jpeg"))
    assert "alpha" in refused["error"]


def test_convert_animation_all_frames_then_dropped(tmp_path):
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, duration=80)
    webp = load(tools.image_convert(path=str(src), format="webp"))
    assert webp["frames_written"] == 3 and webp["frames_dropped"] == 0
    with Image.open(webp["output"]) as im:
        assert im.n_frames == 3 and im.is_animated

    jpg = load(tools.image_convert(path=str(src), format="jpeg"))
    assert jpg["frames_written"] == 1 and jpg["frames_dropped"] == 2
    assert any("frames_dropped=2" in note for note in jpg["notes"])


def test_convert_strip_metadata_and_unknown_format(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(60, 60))
    stripped = load(tools.image_convert(path=str(src), format="png", strip_metadata=True,
                                        output_path=str(tmp_path / "clean.png")))
    assert any("strip_metadata" in note for note in stripped["notes"])
    with Image.open(stripped["output"]) as im:
        assert not im.getexif() and "exif" not in im.info

    bad = load(tools.image_convert(path=str(src), format="heic"))
    assert "unsupported target format" in bad["error"]
    assert "no pip install is suggested" in bad["how_to_fix"]   # never tells the user to install


def test_convert_effort_is_mapped_and_reported(tmp_path):
    src = helpers.make_noise_jpeg(tmp_path / "noise.jpg", size=(300, 300))
    out = load(tools.image_convert(path=str(src), format="webp", effort=9))
    assert any("method" in note for note in out["notes"])
    out2 = load(tools.image_convert(path=str(src), format="png", effort=9, output_path=str(tmp_path / "n.png")))
    assert any("compress_level" in note for note in out2["notes"])


def test_optimize_and_extension_mismatch_note(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(80, 80))
    out = load(tools.image_optimize(path=str(src), quality=70, output_path=str(tmp_path / "wrong.webp")))
    assert any("does not match the written format JPEG" in note for note in out["notes"])


# --------------------------------------------------------------------------- image_optimize

def test_optimize_quality_changes_bytes_and_reports_before_after(tmp_path):
    src = helpers.make_noise_jpeg(tmp_path / "noise.jpg", size=(400, 400), quality=95)
    out = load(tools.image_optimize(path=str(src), quality=40))
    assert out["after"]["bytes"] < out["before"]["bytes"]
    assert out["after"]["format"] == "JPEG"
    assert any("quality=40" in note for note in out["notes"])


def test_optimize_max_dimension_resizes_and_never_upscales(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(800, 400))
    out = load(tools.image_optimize(path=str(src), max_dimension=200))
    assert out["after"]["dimensions"] == [200, 100]
    assert out["resized"] is True

    out2 = load(tools.image_optimize(path=str(src), max_dimension=4000,
                                     output_path=str(tmp_path / "same.jpg")))
    assert out2["resized"] is False
    assert any("already within the limit" in note for note in out2["notes"])


def test_optimize_strip_metadata(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(60, 60))
    out = load(tools.image_optimize(path=str(src), strip_metadata=True,
                                    output_path=str(tmp_path / "s.jpg")))
    assert out["stripped"] is True
    with Image.open(out["output"]) as im:
        assert not im.getexif()


def test_optimize_refuses_a_format_it_cannot_write(tmp_path):
    src = tmp_path / "p.ppm"
    Image.new("RGB", (20, 20), (1, 2, 3)).save(src)
    payload = load(tools.image_optimize(path=str(src)))
    assert "cannot re-encode" in payload["error"]


# --------------------------------------------------------------------------- shared write path

def test_write_gate_refuses_default_collision_then_allows_with_flags(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    first = load(tools.image_resize(path=str(src), percent=50))
    assert first["replaced"] is False

    collided = load(tools.image_resize(path=str(src), percent=25))
    assert "already exists" in collided["error"]
    assert first["output"] in collided["error"]

    replaced = load(tools.image_resize(path=str(src), percent=25, overwrite=True, confirm=True))
    assert replaced["replaced"] is True
    with Image.open(replaced["output"]) as im:
        assert im.size == (25, 25)


def test_overwrite_requires_both_flags(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    tools.image_resize(path=str(src), percent=50)
    only_overwrite = load(tools.image_resize(path=str(src), percent=30, overwrite=True))
    assert "confirm" in only_overwrite["error"]
    only_confirm = load(tools.image_resize(path=str(src), percent=30, confirm=True))
    assert "already exists" in only_confirm["error"]


def test_in_place_needs_both_flags_and_preserves_mode(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    os.chmod(src, 0o640)
    refused = load(tools.image_resize(path=str(src), percent=50, output_path=str(src)))
    assert "in place" in refused["error"]
    with Image.open(src) as im:
        assert im.size == (100, 100)          # untouched

    done = load(tools.image_resize(path=str(src), percent=50, output_path=str(src),
                                   overwrite=True, confirm=True))
    assert done["replaced"] is True
    with Image.open(src) as im:
        assert im.size == (50, 50)
    assert stat.S_IMODE(os.stat(src).st_mode) == 0o640   # permissions preserved
    no_temps(tmp_path)


def test_output_directory_must_exist(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(50, 50))
    payload = load(tools.image_resize(path=str(src), percent=50,
                                      output_path=str(tmp_path / "nope" / "x.jpg")))
    assert "output directory does not exist" in payload["error"]
    assert not (tmp_path / "nope").exists()


def test_failed_encode_leaves_no_temp_file(tmp_path, monkeypatch):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(300, 300))
    monkeypatch.setattr(tools, "MAX_OUTPUT_BYTES", 10)
    payload = load(tools.image_resize(path=str(src), percent=50))
    assert "over the" in payload["error"] and "cap" in payload["error"]
    assert not (tmp_path / "p_resize.jpg").exists()
    no_temps(tmp_path)


def test_refusal_leaves_no_temp_file_when_crop_box_is_invalid(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(60, 60))
    tools.image_crop(path=str(src), box=[0, 0, 100, 100])
    no_temps(tmp_path)


def test_source_is_never_modified_by_a_normal_write(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(100, 100))
    before = src.read_bytes()
    tools.image_resize(path=str(src), percent=50)
    tools.image_crop(path=str(src), aspect="1:1")
    tools.image_rotate(path=str(src), angle=90)
    assert src.read_bytes() == before


def test_convert_refuses_non_image_and_missing(tmp_path):
    text = tmp_path / "x.txt"
    text.write_text("hello")
    assert "not a readable image" in load(tools.image_convert(path=str(text), format="png"))["error"]
    assert "does not exist" in load(tools.image_convert(path=str(tmp_path / "no.png"), format="png"))["error"]
    assert "no input path" in load(tools.image_resize())["error"]


def test_pillow_guard_is_never_disabled():
    assert Image.MAX_IMAGE_PIXELS == 89_478_485
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False


def test_convert_to_a_still_target_reports_only_the_frames_it_wrote(tmp_path):
    """A still target decodes frame 0 only, so frame_durations describes what was written."""
    src = helpers.make_animated_gif(tmp_path / "loop.gif", frames=6, duration=40)
    jpg = load(tools.image_convert(path=str(src), format="jpeg"))
    assert jpg["frames_written"] == 1 and jpg["frames_dropped"] == 5
    assert len(jpg["frame_durations"]) == 1        # the frames that were not written are not listed
    assert any("frames_dropped=5" in note for note in jpg["notes"])


def test_default_effort_does_not_inherit_the_encoders_slow_setting(tmp_path):
    """No effort given must not mean "use the encoder's slowest sane default"."""
    photo = helpers.make_photo(tmp_path / "p.jpg", size=(90, 70))
    webp = load(tools.image_convert(path=str(photo), format="webp",
                                    output_path=str(tmp_path / "d.webp")))
    assert any("effort not given -> WebP method 2" in note for note in webp["notes"])
    avif = load(tools.image_convert(path=str(photo), format="avif",
                                    output_path=str(tmp_path / "d.avif")))
    assert any("effort not given -> AVIF speed 8" in note for note in avif["notes"])


def test_effort_nine_stops_short_of_the_slowest_webp_method(tmp_path):
    """effort=9 must not reach WebP method 6, which is the slowest for ~0.2% fewer bytes."""
    photo = helpers.make_photo(tmp_path / "p.jpg", size=(90, 70))
    top = load(tools.image_convert(path=str(photo), format="webp", effort=9,
                                   output_path=str(tmp_path / "e9.webp")))
    assert any("effort 9 -> WebP method 5" in note for note in top["notes"])


def test_optimize_always_states_the_size_outcome(tmp_path):
    """A re-encode used to report success without saying whether any bytes were saved."""
    flat = tmp_path / "flat.png"
    Image.new("RGB", (60, 40), (10, 20, 30)).save(flat)
    out = load(tools.image_optimize(path=str(flat), output_path=str(tmp_path / "opt.png")))
    assert os.path.getsize(out["output"]) == os.path.getsize(flat), "fixture can no longer shrink"
    # the "nothing was saved" branch specifically - the pre-existing "LARGER than the input"
    # warning would satisfy a looser assertion without the change being present at all
    assert any("nothing was saved" in n for n in out["notes"]), out["notes"]


def test_optimize_max_dimension_reports_the_true_source_geometry(tmp_path):
    """draft() shrinks im.size; the resize note must still name the file's real dimensions."""
    photo = helpers.make_photo(tmp_path / "big.jpg", size=(1200, 900))
    out = load(tools.image_optimize(path=str(photo), max_dimension=300,
                                    output_path=str(tmp_path / "small.jpg")))
    assert out["resized"] is True
    assert out["after"]["dimensions"] == [300, 225]
    assert any("resized 1200x900 -> 300x225" in note for note in out["notes"])
    assert any("JPEG draft decode used" in note for note in out["notes"])


def test_optimize_drafts_before_decoding(tmp_path, monkeypatch):
    """The draft must run before the decode, or the saving it exists for never happens."""
    from PIL import JpegImagePlugin

    calls = []
    original = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        calls.append({"decoded_already": not bool(getattr(self, "tile", None))})
        return original(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    src = helpers.make_noise_jpeg(tmp_path / "n.jpg", size=(1200, 900))
    out = load(tools.image_optimize(path=str(src), max_dimension=300,
                                    output_path=str(tmp_path / "o.jpg")))
    assert "error" not in out, out
    assert calls, "draft() was never called for a JPEG max_dimension downscale"
    assert calls[0]["decoded_already"] is False, "draft() ran after the pixels were decoded"


def test_save_kwargs_carry_the_default_encoder_settings(tmp_path):
    """The note and the encoder setting are one decision, so pin the knob, not the message."""
    photo = helpers.make_photo(tmp_path / "p.jpg", size=(80, 60))
    im = Image.open(photo)
    try:
        webp, notes, _, _ = tools._save_kwargs(im, "WEBP", quality=85)
        assert webp.get("method") == tools.WEBP_DEFAULT_METHOD == 2
        assert any("effort not given" in n for n in notes)
        avif, _, _, _ = tools._save_kwargs(im, "AVIF", quality=85)
        assert avif.get("speed") == tools.AVIF_DEFAULT_SPEED == 8
        top, _, _, _ = tools._save_kwargs(im, "WEBP", quality=85, effort=9)
        assert top.get("method") == tools.WEBP_MAX_METHOD == 5
        png, _, _, _ = tools._save_kwargs(im, "PNG")
        assert "method" not in png and "speed" not in png   # the defaults are WebP/AVIF only
    finally:
        im.close()


def test_default_encoder_settings_reach_the_encoder(tmp_path):
    """Pin the effect: the default must not be what Pillow would have done anyway."""
    src = helpers.make_noise_jpeg(tmp_path / "n.jpg", size=(400, 300))
    default = load(tools.image_convert(path=str(src), format="webp",
                                       output_path=str(tmp_path / "d.webp")))
    top = load(tools.image_convert(path=str(src), format="webp", effort=9,
                                   output_path=str(tmp_path / "e9.webp")))
    assert os.path.getsize(default["output"]) != os.path.getsize(top["output"])


def test_draft_note_is_absent_when_the_decode_was_not_reduced(tmp_path):
    """draft() is a no-op below a 2x reduction; the note must not claim a reduced decode."""
    src = helpers.make_noise_jpeg(tmp_path / "n.jpg", size=(1200, 900))
    shallow = load(tools.image_optimize(path=str(src), max_dimension=1100,
                                        output_path=str(tmp_path / "s.jpg")))
    assert shallow["resized"] is True
    assert not any("draft decode" in n for n in shallow["notes"]), shallow["notes"]

    deep = load(tools.image_optimize(path=str(src), max_dimension=300,
                                     output_path=str(tmp_path / "d.jpg")))
    assert any("draft decode" in n for n in deep["notes"])


def test_still_target_note_admits_the_dropped_frames_were_not_read(tmp_path):
    """The walk is skipped for a still target, so the note must say what was not checked."""
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=6, duration=40)
    out = load(tools.image_convert(path=str(src), format="jpeg"))
    assert out["frames_written"] == 1 and out["frames_dropped"] == 5
    assert any("neither decoded nor validated" in n for n in out["notes"]), out["notes"]


def test_a_damaged_later_frame_is_not_read_when_the_walk_is_skipped(tmp_path, monkeypatch):
    """The accepted cost of skipping the walk, pinned from both sides.

    Every frame after the first is made to fail when decoded, as a truncated animation would. A still
    target converts anyway - it never reads them - and says so in the note. An animated target still
    walks, so it still sees the damage and refuses. Removing the walk breaks the first half; restoring
    it breaks the second.
    """
    from PIL import ImageSequence

    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=6, duration=40)
    original = ImageSequence.Iterator

    class DamagedFromSecondFrame:
        def __init__(self, im):
            self._inner = iter(original(im))
            self._yielded = 0

        def __iter__(self):
            return self

        def __next__(self):
            self._yielded += 1
            if self._yielded > 1:
                raise OSError("image file is truncated (0 bytes not processed)")
            return next(self._inner)

    monkeypatch.setattr(ImageSequence, "Iterator", DamagedFromSecondFrame)

    still = load(tools.image_convert(path=str(src), format="jpeg"))
    assert "error" not in still, still
    assert still["frames_written"] == 1
    assert any("neither decoded nor validated" in n for n in still["notes"]), still["notes"]

    walked = load(tools.image_convert(path=str(src), format="png",
                                      output_path=str(tmp_path / "a.png")))
    assert "error" in walked, "an animated target still walks, so it must see the damage"


def test_header_stage_checks_run_before_the_pixel_decode(tmp_path):
    """Documents the precedence: parameter and path checks precede the decode, so a damaged file
    whose output path is taken is reported as the path collision."""
    src = helpers.make_noise_jpeg(tmp_path / "n.jpg", size=(400, 300))
    helpers.truncate(src, 0.5)
    clash = tmp_path / "clash.jpg"
    clash.write_bytes(b"x")
    out = load(tools.image_optimize(path=str(src), output_path=str(clash)))
    assert "error" in out
    assert "already exists" in out["error"], out["error"]


def test_geometry_tools_inherit_the_default_encoder_settings(tmp_path):
    """resize/crop/rotate expose no effort parameter, so they take the defaults silently."""
    src = tmp_path / "w.webp"
    Image.new("RGB", (160, 120), (40, 90, 160)).save(src, "WEBP")
    out = load(tools.image_resize(path=str(src), percent=50,
                                  output_path=str(tmp_path / "half.webp")))
    assert any("effort not given -> WebP method 2" in n for n in out["notes"]), out["notes"]
