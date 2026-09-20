"""Regression tests: each test pins one guarantee that the plugin must keep.

The guarantees are the stated behaviour of the code — the caps and the refusal messages that carry
them, TIFF round trips that carry no structural IFD, animation that is never silently dropped, the
alpha gate, the parameter checks, exclusive publishing, and the no-dead-code rule. Assertions are
observable: the JSON envelope, the bytes on disk, the leftover temp files.

    uv run --python 3.11 --with pytest --with 'pillow==12.3.0' --with pyyaml python -m pytest tests/ -q
"""

from __future__ import annotations

import gc
import io
import json
import mmap
import os
import stat
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


# --------------------------------------------------------------- caps and the bomb band

def test_per_frame_cap_refusal_carries_the_header_facts(tmp_path):
    """A 50.4 MP header-only PNG: refused by our cap, and image_info still reports the numbers."""
    src = helpers.make_png_header_only(tmp_path / "hb.png", 8000, 6300)   # 50.4 MP, under Pillow's line
    info = load(tools.image_info(path=str(src)))
    assert "over the 50 MP per-frame cap" in info["error"]
    assert info["width"] == 8000 and info["height"] == 6300        # facts, not just prose
    assert info["pixels"] == 8000 * 6300 and info["decode_ok"] is False
    out = load(tools.image_resize(path=str(src), percent=50))
    assert out["error"] and "50 MP per-frame cap" in out["error"]


def test_total_cap_refuses_many_frames(tmp_path, monkeypatch):
    """The summed-frames cap, exercised with a tiny budget so a real 3-frame GIF trips it."""
    monkeypatch.setattr(tools, "MAX_PIXELS_TOTAL", 1000)
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, size=(40, 40))   # 4800 px total
    info = load(tools.image_info(path=str(src)))
    assert "MP total cap" in info["error"]
    assert info["frames"] == 3 and info["pixels"] == 1600          # and the real frame count


def test_bomb_warning_band_is_a_structured_refusal(tmp_path):
    """89.5-179 MP: Pillow warns instead of raising; the plugin must refuse, never hand back a warning."""
    src = helpers.make_png_header_only(tmp_path / "band.png", 12000, 10000)     # 120 MP
    info = load(tools.image_info(path=str(src)))
    assert info["error"].startswith("refused:")                    # the bomb branch, not the cap branch
    assert "decompression-bomb guard" in info["how_to_fix"]
    out = load(tools.image_resize(path=str(src), percent=50))
    assert out["error"].startswith("refused:")
    assert "re-run with the same arguments" not in out["how_to_fix"]            # no useless retry


def test_guard_filters_do_not_leak_across_threads(tmp_path):
    """Two overlapping guarded regions must not leave an 'error' filter installed process-wide."""
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


# --------------------------------------------------------------- TIFF round trips

@pytest.mark.parametrize("tool_name,kwargs", [
    ("image_resize", {"percent": 50}),
    ("image_crop", {"box": [0, 0, 32, 24]}),
    ("image_rotate", {"angle": 90}),
])
def test_tiff_round_trip_is_readable(tmp_path, tool_name, kwargs):
    """The source's structural IFD must not be carried as EXIF, or the output is undecodable."""
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
    """Pillow applies a TIFF's orientation to the pixels at load and consumes the tag,
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
    """The multi-page TIFF saver re-reads the file it writes, so the temp handle must be r+w."""
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, size=(40, 40))
    out = load(tools.image_convert(path=str(src), format="TIFF"))
    assert "error" not in out, out
    assert out["frames_written"] == 3 and out["frames_dropped"] == 0
    assert frames_on_disk(Path(out["output"])) == 3


# --------------------------------------------------------------- animation is not silently dropped

@pytest.mark.parametrize("tool_name,kwargs", [
    ("image_resize", {"percent": 50}),
    ("image_crop", {"box": [0, 0, 20, 20]}),
    ("image_rotate", {"angle": 90}),
    ("image_optimize", {}),
])
def test_animation_survives_every_geometry_tool(tmp_path, tool_name, kwargs):
    """A 3-frame GIF stayed a 3-frame GIF (or the drop is reported in the envelope)."""
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, size=(40, 40))
    result = load(getattr(tools, tool_name)(path=str(src), **kwargs))
    assert "error" not in result, result
    kept = frames_on_disk(Path(result["output"]))
    if result.get("frames_dropped"):
        assert kept == 1 and "frames_dropped" in " ".join(result["notes"])
    else:
        assert kept == 3 and result.get("frames_written") == 3


def test_in_place_optimize_never_replaces_an_animation_with_a_still(tmp_path):
    """An in-place optimize of an animated GIF keeps all frames in the file it replaces."""
    src = helpers.make_animated_gif(tmp_path / "a.gif", frames=3, size=(40, 40))
    result = load(tools.image_optimize(path=str(src), output_path=str(src),
                                       overwrite=True, confirm=True))
    assert "error" not in result, result
    assert result["replaced"] is True
    assert frames_on_disk(src) == 3
    no_temps(tmp_path)


IN_PLACE_TOOLS = ("image_resize", "image_crop", "image_rotate", "image_optimize", "image_convert")


def in_place_kwargs(tool: str, path: Path, fmt: str) -> dict:
    """An in-place write: output_path is the input, with the flags the tools demand for that."""
    kwargs = {"path": str(path), "output_path": str(path)}
    if tool == "image_resize":
        kwargs["percent"] = 50
    elif tool == "image_crop":
        kwargs["box"] = [0, 0, 20, 20]
    elif tool == "image_rotate":
        kwargs["angle"] = 90
    elif tool == "image_convert":
        kwargs["format"] = fmt
    return kwargs


def make_multiframe(path: Path, fmt: str) -> Path:
    """A lazily-read source: Pillow keeps the read handle until the last frame is out."""
    if fmt == "gif":
        return helpers.make_animated_gif(path, frames=3, size=(40, 40))
    frames = [Image.new("L", (40, 40), level) for level in (10, 120, 240)]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    return path


def handles_open_on(path) -> list:
    """Everything in this process still holding that path: open files and live mappings.

    Both block a Windows rename, and a mapping is not an ``io.IOBase``, so the GC walk covers the
    mapping through the image that owns it (``mmap`` exposes no path of its own).
    """
    want = os.path.realpath(path)
    found = []
    gc.collect()
    for obj in gc.get_objects():
        try:
            if isinstance(obj, io.IOBase):
                if not obj.closed and obj.fileno() >= 0:
                    name = getattr(obj, "name", None)
                    if isinstance(name, (str, os.PathLike)) and os.path.realpath(name) == want:
                        found.append(f"{type(obj).__name__}({name})")
            elif isinstance(obj, mmap.mmap) and not obj.closed:
                for owner in gc.get_referrers(obj):
                    name = getattr(owner, "filename", None)
                    if isinstance(name, (str, os.PathLike)) and os.path.realpath(name) == want:
                        found.append(f"mmap held by {type(owner).__name__}")
                        break
        except (ValueError, OSError, AttributeError):
            continue
    return found


def test_the_handle_detector_sees_a_handle_it_should_see(tmp_path):
    """The detector itself, pinned: a blind detector would make every assertion below vacuous."""
    src = make_multiframe(tmp_path / "c.gif", "gif")
    assert handles_open_on(src) == []
    probe = open(src, "rb")
    try:
        assert handles_open_on(src), "the detector missed a handle this test opened itself"
    finally:
        probe.close()
    assert handles_open_on(src) == []


@pytest.mark.parametrize("fmt", ["gif", "tiff"])
@pytest.mark.parametrize("tool", IN_PLACE_TOOLS)
def test_in_place_publish_releases_the_source_handle(tmp_path, monkeypatch, tool, fmt):
    """No handle may still be open on the target when it is replaced in place — for every tool.

    Windows refuses to replace a file that still has an open handle (WinError 5); POSIX allows it,
    so on Linux the bug is invisible and only the Windows job in CI sees it. The check runs against
    every in-place call site, on a GIF (a lazily-read handle) and a TIFF (which Pillow maps).

    It validates its own instrument first: with the release neutered the handle MUST show up. Without
    that control, "nothing is open" cannot be told apart from a blind detector or a spy watching the
    wrong path, and the assertions would pass for the wrong reason.
    """
    src = make_multiframe(tmp_path / f"{tool}_{fmt}.{'gif' if fmt == 'gif' else 'tif'}", fmt)
    assert handles_open_on(src) == [], "the fixture itself holds a handle"

    open_at_publish: list = []
    real_replace = os.replace

    def spy_replace(source, destination):
        assert os.path.realpath(destination) == os.path.realpath(src), (
            f"{tool}: the publish spy saw {destination}, expected {src} — it is watching the "
            f"wrong path and would pass regardless")
        open_at_publish.append(handles_open_on(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(tools.os, "replace", spy_replace)
    real_release = tools._release_input

    # control half: neutered release -> the handle must be visible, or this test is blind
    monkeypatch.setattr(tools, "_release_input", lambda *a, **k: None)
    control = load(getattr(tools, tool)(**in_place_kwargs(tool, src, fmt), overwrite=True, confirm=True))
    if "error" in control:
        # Windows enforces the invariant in the OS: with no release the rename is refused outright.
        # That refusal is the bug seen directly, so it counts as the control firing.
        assert "WinError 5" in str(control["error"]) or "PermissionError" in str(control["error"]), control
    else:
        assert control.get("replaced") is True, control
        assert open_at_publish and open_at_publish[-1], (
            f"{tool}/{fmt}: nothing was open even with the release disabled — this test cannot see "
            f"the bug it claims to catch")

    # assertion half: the real code publishes with nothing left open on the target
    monkeypatch.setattr(tools, "_release_input", real_release)
    open_at_publish.clear()
    result = load(getattr(tools, tool)(**in_place_kwargs(tool, src, fmt), overwrite=True, confirm=True))
    assert "error" not in result, result
    assert result["replaced"] is True
    assert open_at_publish, f"{tool}: the in-place write never published through os.replace"
    assert not open_at_publish[-1], (
        f"{tool}/{fmt}: handle(s) still open on the target when it was replaced: "
        f"{open_at_publish[-1]} — Windows refuses this with WinError 5")
    assert frames_on_disk(src) == 3
    no_temps(tmp_path)


def test_release_input_closes_the_image_and_its_mapping(tmp_path):
    """The release contract, pinned directly: the mapping goes too, not just the file handle.

    ``Image.close()`` only drops ``self.map`` (it closes the mapping itself under win32 + PyPy
    alone), so relying on it would leave the unmapping to the garbage collector — timing a rename
    cannot depend on.
    """
    path = tmp_path / "mapped.bin"
    path.write_bytes(b"0123456789abcdef")

    class Mapped:
        def __init__(self, mapping):
            self.map = mapping
            self.closed = False

        def close(self):
            self.closed = True

    with open(path, "rb") as handle:
        mapping = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
    holder = Mapped(mapping)
    tools._release_input(holder)
    assert holder.closed, "the image itself was left open"
    with pytest.raises(ValueError):
        mapping.read(1)   # a closed mmap refuses to read


def test_a_failed_publish_still_restores_the_mode(tmp_path, monkeypatch):
    """A publish that fails must not leave behind a file the read-only escape hatch made writable.

    That branch only runs where the read-only attribute was cleared (Windows), so the clearing is
    simulated here — but the *failure path itself* is what this exercises. An except clause no test
    can reach is where a NameError hides, and one did: this path shipped referring to contextlib,
    which tools.py never imports, and only the Windows job ever ran it.
    """
    src = helpers.make_noise_jpeg(tmp_path / "ro.jpg", size=(40, 40))
    os.chmod(src, 0o444)

    def fake_clear(path):
        os.chmod(path, stat.S_IWRITE)   # what the Windows branch does to the target
        return True

    monkeypatch.setattr(tools, "_make_writable_for_replace", fake_clear)
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)

    def broken(source, destination):
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(tools.os, "replace", broken)

    out = load(tools.image_optimize(path=str(src), output_path=str(src), overwrite=True, confirm=True))
    assert "error" in out, out
    assert "synthetic publish failure" in out["error"], (
        f"the real failure was masked: {out['error']}")
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(src).st_mode) == 0o444, "the write left the file writable"
    os.chmod(src, 0o644)


def test_make_writable_for_replace_is_windows_only(tmp_path):
    """The read-only escape hatch runs on Windows and leaves POSIX modes alone.

    Windows refuses to replace a read-only file (WinError 5); POSIX does not care, and clearing a
    bit there would be a permission change nobody asked for. The platform is a parameter, so the
    branch that only CI could otherwise reach is exercised here.
    """
    path = tmp_path / "ro.bin"
    path.write_bytes(b"x")
    os.chmod(path, 0o444)

    assert tools._make_writable_for_replace(path, platform="posix") is False
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o444, "a POSIX mode was changed"

    assert tools._make_writable_for_replace(path, platform="nt") is True
    assert stat.S_IMODE(os.stat(path).st_mode) & stat.S_IWRITE, "the file is still not writable"
    os.chmod(path, 0o644)


def test_the_output_mode_is_applied_after_the_publish(tmp_path, monkeypatch):
    """The mode is restored *after* the rename, never written onto the temp file before it.

    Windows denies replacing a read-only file (WinError 5 — the same signature as the bug this
    branch fixes), so chmod'ing the temp file to the target's mode first breaks in-place writes on a
    read-only input. The ordering is the fix, so the ordering is what this pins.
    """
    src = helpers.make_noise_jpeg(tmp_path / "ro.jpg", size=(60, 60))
    real_chmod = os.chmod
    os.chmod(src, 0o444)

    events: list = []
    real_replace = os.replace

    def spy_replace(source, destination):
        events.append(("publish", os.path.realpath(destination)))
        return real_replace(source, destination)

    def spy_chmod(path, mode, *args, **kwargs):
        events.append(("chmod", os.path.realpath(path)))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(tools.os, "replace", spy_replace)
    monkeypatch.setattr(tools.os, "chmod", spy_chmod)

    out = load(tools.image_optimize(path=str(src), output_path=str(src), overwrite=True, confirm=True))
    assert "error" not in out, out
    assert out["replaced"] is True

    kinds = [kind for kind, _ in events]
    assert "publish" in kinds, "the write never published"
    published_at = kinds.index("publish")
    temp_chmods = [p for kind, p in events[:published_at]
                   if kind == "chmod" and Path(p).suffix == ".tmp"]
    assert not temp_chmods, (
        f"the temp file was chmod'ed before the rename ({temp_chmods}) — a read-only temp file is "
        f"the same WinError 5 this branch exists to avoid")
    assert any(kind == "chmod" and path == os.path.realpath(src)
               for kind, path in events[published_at:]), "the target's mode was never restored"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(src).st_mode) == 0o444, "the mode was not preserved"
    real_chmod(src, 0o644)


def test_convert_to_tiff_accepts_the_orientation_its_reader_applies(tmp_path):
    """A TIFF written with a rotating EXIF orientation reads back transposed — and that is correct.

    Pillow's TIFF reader applies orientation 5-8 as it opens the file and consumes the tag at load,
    so the encoded file is legitimately transposed relative to the in-memory image. The write used
    to fail its own verification instead: "the encoder wrote 60x80 but the tool reported 80x60".
    """
    src = tmp_path / "photo.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (80, 60), (120, 60, 30)).save(src, quality=90, exif=exif.tobytes())

    env = load(tools.image_convert(path=str(src), format="TIFF", output_path=str(tmp_path / "out.tif")))
    assert "error" not in env, env
    assert Path(env["output"]).exists(), "nothing was written"
    with Image.open(env["output"]) as check:
        assert check.size == (60, 80), "the TIFF should read back transposed by its own tag"


def test_verify_encoded_rejects_a_transposed_size_without_a_rotating_tag(tmp_path):
    """The relaxation above is not a blanket "either orientation is fine"."""
    path = tmp_path / "still.png"
    Image.new("RGB", (10, 20), (5, 5, 5)).save(path)
    with pytest.raises(tools.ToolError, match="the encoder wrote 10x20"):
        tools._verify_encoded(path, {"size": (20, 10)})


def test_verify_encoded_accepts_a_transposed_size_when_the_file_declares_the_rotation(tmp_path):
    path = tmp_path / "rot.tif"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (10, 20), (5, 5, 5)).save(path, format="TIFF", exif=exif.tobytes())
    with Image.open(path) as back:
        assert back.size == (20, 10)          # the reader applied the tag
    tools._verify_encoded(path, {"size": (10, 20)})   # must not raise


def test_the_publish_runs_while_the_guard_lock_is_held(tmp_path, monkeypatch):
    """Release -> rename -> chmod is one critical section; a worker thread must not slip inside it.

    Hermes runs tool calls in worker threads, so another call could open the target in that window —
    and on Windows an open handle at rename time is WinError 5 all over again.
    """
    src = make_multiframe(tmp_path / "locked.gif", "gif")
    witnessed: list = []
    real_replace = os.replace

    def spy_replace(source, destination):
        result: list = []

        def probe():
            acquired = tools._GUARD_LOCK.acquire(blocking=False)
            result.append(acquired)
            if acquired:
                tools._GUARD_LOCK.release()

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        witnessed.append(result[0])
        return real_replace(source, destination)

    monkeypatch.setattr(tools.os, "replace", spy_replace)

    out = load(tools.image_optimize(path=str(src), output_path=str(src), overwrite=True, confirm=True))
    assert "error" not in out, out
    assert witnessed, "the write never published"
    assert not any(witnessed), (
        "another thread could acquire the guard lock while the target was being published — that is "
        "the window a Windows rename cannot survive")


def test_transient_windows_publish_errors_are_retried_and_real_ones_are_not(tmp_path, monkeypatch):
    """WinError 5/32 is retried (an AV scanner or indexer holding the target); anything else is not.

    No in-process fix can stop an external holder, so those two codes get a short backoff; a genuine
    failure must still surface immediately rather than being papered over by retries.
    """
    src = make_multiframe(tmp_path / "flaky.gif", "gif")
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)
    real_replace = os.replace

    def winerror(code: int) -> OSError:
        exc = OSError(f"synthetic error {code}")
        exc.winerror = code
        return exc

    # two sharing violations, then success
    transient_calls: list = []
    left = {"n": 2}

    def flaky(source, destination):
        transient_calls.append(1)
        if left["n"]:
            left["n"] -= 1
            raise winerror(32)
        return real_replace(source, destination)

    monkeypatch.setattr(tools.os, "replace", flaky)
    out = load(tools.image_optimize(path=str(src), output_path=str(src), overwrite=True, confirm=True))
    assert "error" not in out, out
    assert len(transient_calls) == 3, f"expected 2 retries before success, saw {len(transient_calls)}"

    # and a non-transient error is reported on the first attempt
    permanent_calls: list = []

    def permanent(source, destination):
        permanent_calls.append(1)
        raise winerror(2)

    monkeypatch.setattr(tools.os, "replace", permanent)
    failed = load(tools.image_optimize(path=str(src), output_path=str(src), overwrite=True, confirm=True))
    assert "error" in failed, "a real publish failure was swallowed"
    assert len(permanent_calls) == 1, f"a non-transient error was retried {len(permanent_calls)} times"


# --------------------------------------------------------------- arbitrary-angle rotate, all modes


# --------------------------------------------------------------- arbitrary-angle rotate, all modes

@pytest.mark.parametrize("mode,container", [
    ("L", "png"), ("1", "png"), ("LA", "png"), ("I;16", "png"), ("F", "tiff"),
    ("CMYK", "jpg"), ("RGB", "tiff"),
])
def test_rotate_arbitrary_angle_works_for_every_mode(tmp_path, mode, container):
    """The fill colour must match the mode's channel shape, or rotate(45) dies."""
    src = tmp_path / f"src_{mode.replace(';', '_')}.{container}"
    Image.new(mode, (40, 30), 0).save(src)
    out = load(tools.image_rotate(path=str(src), angle=45, background="white"))
    assert "error" not in out, out
    with Image.open(out["output"]) as im:
        im.load()
        assert im.size == tuple(out["after"]["dimensions"])


def test_rotate_palette_transparency_keeps_transparency(tmp_path):
    """A transparent fill cannot be added to an RGB palette — rotate must route via RGBA."""
    src = helpers.make_palette_transparency(tmp_path / "p.png", size=(60, 40))
    out = load(tools.image_rotate(path=str(src), angle=45))
    assert "error" not in out, out
    assert out["after"]["mode"] == "RGBA"
    with Image.open(out["output"]) as im:
        assert im.mode == "RGBA" and im.getpixel((0, 0))[3] == 0
    assert "palette transparency preserved" in " ".join(out["notes"])


# --------------------------------------------------------------- alpha gate + after.mode honesty

def test_bmp_flatten_composites_onto_the_background(tmp_path):
    """flatten=true with a background must actually composite, and after.mode must be true."""
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
    """A 1-bit PNG written as JPEG is mode L on disk, and the envelope must say so."""
    src = tmp_path / "1bit.png"
    Image.new("1", (40, 40), 1).save(src)
    out = load(tools.image_convert(path=str(src), format="JPEG"))
    assert "error" not in out, out
    assert out["after"]["mode"] == "L"
    assert out["before"]["mode"] == "1"


# --------------------------------------------------------------- parameter truth

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


# --------------------------------------------------------------- notes and refusal texts

def test_strip_metadata_really_strips_icc_and_exif(tmp_path):
    """strip_metadata=true must really remove ICC, EXIF and DPI from a PNG, not only claim it."""
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
    """An input format Pillow cannot write falls back to PNG — that must be visible."""
    src = tmp_path / "x.ppm"
    Image.new("RGB", (30, 20), (1, 2, 3)).save(src)
    out = load(tools.image_crop(path=str(src), box=[0, 0, 20, 20]))
    assert "error" not in out, out
    assert any("cannot be written by Pillow" in note for note in out["notes"])
    with Image.open(out["output"]) as im:
        assert im.format == "PNG"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows ignores the read-only attribute on a directory, so a directory that refuses "
           "file creation cannot be constructed there",
)
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


# --------------------------------------------------------------- exclusive publish

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
    """A file that reopens at the wrong size is refused instead of published."""
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


# --------------------------------------------------------------- dead code and claims

def test_no_dead_helpers_remain():
    """No dead code: no unused envelope builder and no unused binding in tools.py."""
    import ast

    source = Path(tools.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef,))}
    assert "_public_result" not in names
    assert "orientation_in_output" not in source


def test_resize_drafts_the_jpeg_before_decoding(tmp_path, monkeypatch):
    """draft() must run before the decode, or the reported memory saving never happens."""
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
