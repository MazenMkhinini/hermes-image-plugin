# Hermes `image-utils` plugin

Local image-file tools for Hermes Agent: inspect, resize, crop, rotate, convert and re-encode photos
without a shell. Pillow (12.3.0, already in the Hermes venv) runs in-process — no `subprocess`, no
ImageMagick, no network, no new dependencies, and no Grocy or other household service is touched.
Toolset `image_utils`, six tools, no config and no secrets. The authoritative tool list is
`provides_tools` in `plugin.yaml`; the test suite pins it against the schemas, the handlers, and the
tables below.

**Prerequisite — Pillow in the Hermes venv.** 12.3.0 is the tested version. Pillow is a hermes-agent
dependency rather than something this plugin installs, so it is deliberately *not* declared in
`plugin.yaml` (no `python_dependencies` key). Without it, `image_info` still answers and reports
`pillow_available: false`, while the other five tools refuse with a `how_to_fix`.

Built 2026-09-17 through design review → implementation → adversarial review → fixes; the decisions
that came out of it are recorded in **Safety behaviour that matters** and **Decisions and known
limits** below.

## What it gives the agent

| Tool | Type | Notes |
|---|---|---|
| `image_info` | read | dimensions, format, mode, frames, DPI, ICC present?, EXIF summary (camera, datetime, orientation, GPS **presence only** — never coordinates), file bytes, animation, and whether the pixels actually decode. Always visible, even when Pillow is missing — it is the diagnostic tool. |
| `image_resize` | write | `width` / `height` / `percent` (exactly one; both width+height needs `allow_distort=true`), aspect preserved, Lanczos, **never upscales** unless `allow_upscale=true`. A JPEG downscale calls `draft()` *before* decoding, so the JPEG is decoded at the reduced scale; the target size is capped like an input (50 MP). Animated input keeps every frame. |
| `image_crop` | write | `box=[left, top, right, bottom]` (validated against the image bounds — out-of-range is refused, Pillow would silently pad black), or `aspect="16:9"` / `"1:1"` (largest centred rectangle), or centred `width`/`height`. Animated input keeps every frame. |
| `image_rotate` | write | 90/180/270 by exact `transpose()`; other angles expand the canvas (`bicubic`, background-coloured corners, transparent for alpha; the fill is shaped per mode and a palette-transparent P image is rotated as RGBA so its transparency survives). `auto_orient=true` (default) applies the EXIF orientation first, and the written file gets the orientation tag cleared (written as 1) where the target can carry EXIF — GIF/BMP say so in `notes` instead. Omit `angle` to only fix orientation. Animated input keeps every frame. |
| `image_convert` | write | PNG/JPEG/WebP/TIFF/GIF/AVIF/BMP. Alpha → JPEG/BMP/GIF requires `flatten=true` (composited onto `background`, default white) instead of silently dropping transparency. Animated inputs write all frames where the target supports it (GIF/WebP/AVIF/TIFF/PNG), else the first frame with `frames_dropped` reported. |
| `image_optimize` | write | re-encode in the input's own format: `quality`, `effort` (0–9, mapped per format and reported), optional `max_dimension` (Lanczos, never upscales), `strip_metadata` (default false and stated in the response). Animated input keeps every frame, including an in-place re-encode. |

Every write returns `{input, output, before, after, replaced, frames_written, frames_dropped,
notes}`; `before` comes from the header stage and `after` is read back **from the file on disk**, so
the envelope describes the bytes that actually landed. Every refusal returns
`{"error": ..., "how_to_fix": ...}`.

## Safety behaviour that matters

- **No shell, ever.** Pillow in-process; the test suite fails if `subprocess`, `os.system`, `Popen`,
  `shutil.which` or `os.exec*` ever appear in the plugin's source.
- **Writes never overwrite by default.** The output goes to `output_path`, or beside the input as
  `<stem>_<operation>.<ext>`. If that resolved path already exists — or *is* the input — the call is
  refused unless `overwrite=true` **and** `confirm=true`, and the response then names what was
  replaced. `confirm=true` is supplied by the model, not by a human: it is deliberate friction, not
  an approval gate (a `pre_tool_call` allowlist is deferred to v2, see below).
- **Writes are atomic and verified.** The encoder writes a hidden temp file in the target's
  directory, `fsync`s, checks the output cap, then **re-reads the encoded file** (dimensions, frame
  count, decodability, and — for `strip_metadata` — that the metadata really is gone) before
  publishing. Publishing is exclusive (`os.link`, with an `O_CREAT|O_EXCL` fallback): if the target
  appeared while the encoder worked, the call is refused instead of clobbering it. Replacing a file
  on purpose needs `overwrite=true` **and** `confirm=true`. Any failure deletes the temp file; a test
  asserts no `*.tmp` survives. The replaced file's mode bits are preserved (a 0644 photo stays
  0644); new files are 0o644.
- **Fail closed.** Missing / empty / non-regular / non-image / truncated inputs, an unsupported
  target format, a bad `width`/`height` (≤ 0), an out-of-range crop box, or an output directory that
  does not exist are all refused with a `how_to_fix` and nothing written. The plugin never creates
  directories.
- **Caps, and where they are enforced.** ≤ 50 MB file, ≤ 50 MP per frame, ≤ 100 MP summed over
  frames. Checks run in a fixed order: `lstat` (regular file, size) → `Image.open()` inside a
  warnings-as-errors guard for `Image.DecompressionBombWarning` and an explicit catch of
  `DecompressionBombError` (which is **not** an `OSError`) → header-dims check against the plugin
  caps → `im.load()`. Pillow's own `MAX_IMAGE_PIXELS` (89,478,485) is never assigned, disabled, or
  ignored, and `ImageFile.LOAD_TRUNCATED_IMAGES` is never set. The guard serialises overlapping
  calls with a process-wide lock, because `warnings.catch_warnings` mutates process-global state and
  Hermes runs tool calls in worker threads. A 200 MP header-only PNG **and** a 96–178 MP one (the
  band where Pillow only *warns*) are both refused with a `how_to_fix` — never a traceback, never a
  warning handed back to the model.
- **Memory.** A decoded image costs ≈ width × height × channels bytes (a 50 MP RGB image ≈ 150 MB),
  and a resize holds the source plus the result — budget ~3× the decoded size transiently. That is
  why the per-frame cap is 50 MP rather than the 100 MP the first plan draft assumed: in-process
  measurement showed a 95 MP decompression peaking at ~0.77 GB RSS, which left no headroom under the
  withdrawn cap (measured on a private dataset, not reproducible from this repo — the number sizes
  the decision, it is not a benchmark you can re-run here).
- **Metadata honesty.** `image_rotate` clears the EXIF orientation tag (it changes pixel
  orientation); resize/crop/convert/optimize preserve EXIF/ICC/DPI, except where the target format
  cannot carry a block — then the response's `notes` list what was dropped (GIF and BMP drop
  EXIF+ICC; WebP and AVIF cannot carry DPI). A TIFF source is special: Pillow's reader applies the
  orientation to the pixels and consumes the tag at load, so the output carries no stale orientation
  and the note says so. A TIFF's structural IFD (strip/tile offsets, bit depths, colour maps, …) is
  never round-tripped onto a re-encoded image — only user-level tags (camera, datetime, orientation,
  GPS presence, exposure) are carried. Metadata is attached by presence, never as `None` (that
  crashes the JPEG/TIFF/WebP/BMP savers). `strip_metadata=true` really removes ICC/EXIF/DPI, and the
  encoded file is re-read to prove it before it is published.
- **HEIC/HEIF is out of scope.** `pillow_heif 1.5.0` exists in the venv (a hermes-agent dependency)
  but Pillow does not register it and this plugin deliberately does not either — HEIC is refused with
  the detected state named, and no `pip install` is ever suggested. `image_convert` resolves the
  target from its own allow-list, so a `.heic` output path cannot reach Pillow's extension guessing.

## Install / configure

```bash
./install.sh                # every profile under the Hermes root, staged (non-default first)
./install.sh <profile>      # one profile
```

The script symlinks this checkout into `<profile-home>/plugins/image-utils`, runs
`hermes -p <profile> plugins enable image-utils` (which writes `plugins.enabled` plus the
`allow_tool_override` grant), verifies exposure through the same call the agent uses
(`get_tool_definitions(..., skip_tool_search_assembly=True)`), and restarts that profile's gateway.
No `platform_toolsets` entry is needed in Hermes 0.21.3: plugin toolsets are enabled by default on
every platform (`_enabled_plugin_toolsets`), proven live under a temp `HERMES_HOME` — add an entry
only to pin one platform's list.

No configuration keys and no secrets. Plugins are per-profile (`$HERMES_HOME/plugins/`) and
opt-in (`plugins.enabled`), so a directory alone loads nothing; the symlink keeps this checkout the
single source of truth.

## Where to look when something is off

1. `image_info` from a chat — it reports `pillow_available` and answers even for damaged or
   over-limit files, so it is the first diagnostic.
2. Tools missing entirely → `image-utils` is not in that profile's `plugins.enabled`, or the
   checkout is not symlinked into that profile's `plugins/`. `hermes -p <profile> plugins enable
   image-utils` fixes both.
3. A tool the docs mention is missing but nothing errors → plugin tools are listed per process when
   that process loads the plugin; a long-running process that started before the change keeps the
   old list. Restart the process that shows the stale list (the desktop app needs its own restart).
4. `hermes plugins doctor .` (from this checkout) proves import + registration;
   `hermes plugins validate .` is the strict gate (manifest
   fields, declared-vs-registered tools, security scan). Neither one can see the no-shell rule, the
   cross-toolset naming rule, or README/SKILL drift — the test suite covers those.
5. Rotation looks doubled → the file predates the fix or was written by another tool; `image_rotate`
   writes orientation as 1 and `image_info` reports the tag it finds.
6. A write refused with "output already exists" → the default name is a function of (input,
   operation), so re-runs collide. Pass `output_path`, or `overwrite=true` with `confirm=true`.

## Tests

```bash
cd path/to/this/checkout
uv run --python 3.11 --with pytest --with 'pillow==12.3.0' --with pyyaml python -m pytest tests/ -q   # offline
```

Never `pip install` anything into the Hermes venv (its pins are load-bearing); `uv run` builds a
throwaway env from the uv cache. Offline tests generate all fixtures with Pillow itself (including
EXIF orientation via `Image.Exif()` — no piexif) and assert behaviour: the JSON envelopes, the bytes
on disk, mode-bit preservation, and that no temp file survives any failure path.

Live checks (not part of the suite) were run against real files — a 8–12 MB JPEG, a PNG with alpha,
a WebP, an EXIF-rotated photo and an animated GIF — held in a private dataset, so those runs are not
reproducible from this repo; the numbers they produced are quoted below as measurements of the
decision, not as benchmarks. The suite itself needs nothing but Pillow.

## Rollback

```bash
rm <profile-home>/plugins/image-utils
hermes -p <profile> plugins disable image-utils
hermes -p <profile> gateway restart
```

No state outside the files it writes; the plugin keeps no cache, no database and no config.

## Decisions and known limits

- The 100 MP per-image cap from the first plan draft was **withdrawn** in favour of 50 MP per frame /
  100 MP total after in-process measurement showed ~0.77 GB RSS for a 95 MP decode (measured on a
  private dataset, not reproducible from this repo); the refusal messages state the numbers.
- Publishing is exclusive instead of `os.replace`-over-everything: a concurrency probe showed the old
  check-then-replace let two concurrent writers both report success with only one file left — a rare
  race (~1 in 30 trials on that private dataset, deterministic once the window is widened; not
  reproducible from this repo). The fix closes the window rather than narrowing it.
- `confirm=true` is a model-supplied parameter, not a human approval — same design as the house
  Grocy plugin's dry-run/confirm flags. A `pre_tool_call` hook that blocks in-place overwrites
  outside an allow-listed directory tree is the v2 candidate; it is not implemented here.
- HEIC/HEIF read/write is out of scope; `image_convert` lists the seven supported targets.
- GIF frame surgery, drawing/text/watermarks, batch runs and colour correction are out of scope.
- `image_resize` re-encodes lossy targets at quality 95 unless the format is lossless; the note in
  the response says which quality was applied.
