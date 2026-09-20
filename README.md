# Hermes `image-utils` plugin

Local image-file tools for Hermes Agent: inspect, resize, crop, rotate, convert and re-encode photos
without a shell. Pillow (12.3.0, already in the Hermes venv) runs in-process — no `subprocess`, no
ImageMagick, no network, no new dependencies.
Toolset `image_utils`, six tools, no config and no secrets. The authoritative tool list is
`provides_tools` in `plugin.yaml`; the test suite pins it against the schemas, the handlers, and the
tables below.

**Prerequisite — Pillow in the Hermes venv.** 12.3.0 is the tested version. Pillow is a hermes-agent
dependency rather than something this plugin installs, so it is deliberately *not* declared in
`plugin.yaml` (no `python_dependencies` key). Without it, `image_info` still answers and reports
`pillow_available: false`, while the other five tools refuse with a `how_to_fix`.

## AI-generated code

The code in this repository was written with AI assistance, then reviewed and tested before
publication.

## What it gives the agent

| Tool | Type | Notes |
|---|---|---|
| `image_info` | read | dimensions, format, mode, frames, DPI, ICC present?, EXIF summary (camera, datetime, orientation, GPS **presence only** — never coordinates), file bytes, animation, and whether the pixels actually decode. Always visible, even when Pillow is missing — it is the diagnostic tool. |
| `image_resize` | write | `width` / `height` / `percent` (exactly one; both width+height needs `allow_distort=true`), aspect preserved, Lanczos, **never upscales** unless `allow_upscale=true`. A JPEG downscale calls `draft()` *before* decoding, so the JPEG is decoded at the reduced scale; the target size is capped like an input (50 MP). Animated input keeps every frame. |
| `image_crop` | write | `box=[left, top, right, bottom]` (validated against the image bounds — out-of-range is refused, Pillow would silently pad black), or `aspect="16:9"` / `"1:1"` (largest centred rectangle), or centred `width`/`height`. Animated input keeps every frame. |
| `image_rotate` | write | 90/180/270 by exact `transpose()`; other angles expand the canvas (`bicubic`, background-coloured corners, transparent for alpha; the fill is shaped per mode and a palette-transparent P image is rotated as RGBA so its transparency survives). `auto_orient=true` (default) applies the EXIF orientation first, and the written file gets the orientation tag cleared (written as 1) where the target can carry EXIF — GIF/BMP say so in `notes` instead. Omit `angle` to only fix orientation. Animated input keeps every frame. |
| `image_convert` | write | PNG/JPEG/WebP/TIFF/GIF/AVIF/BMP. Alpha → JPEG/BMP/GIF requires `flatten=true` (composited onto `background`, default white) instead of silently dropping transparency. Animated inputs write all frames where the target supports it (GIF/WebP/AVIF/TIFF/PNG), else the first frame alone: `frames_dropped` reports the rest and `frame_durations` describes only the frames that were written, because the others are never decoded. |
| `image_optimize` | write | re-encode in the input's own format: `quality`, `effort` (0–9, mapped per format and reported), optional `max_dimension` (Lanczos, never upscales), `strip_metadata` (default false and stated in the response). Animated input keeps every frame, including an in-place re-encode. A `max_dimension` JPEG downscale calls `draft()` before decoding, like `image_resize`. Every response states the size outcome: bytes saved, nothing saved, or larger than the input. |

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
- **Only the frames that are written are decoded.** A still target reads the first frame and stops;
  the rest are neither decoded nor validated, which is what turns a 60-frame animation into a ~26 ms
  conversion instead of ~200 ms. A damaged frame *after* the first is therefore not detected, and the
  response says so ("the dropped frames were neither decoded nor validated"). An animated target still
  walks every frame, so it still refuses a truncated file.
- **Header-stage checks run before the pixel decode.** The output path is resolved, gated and
  `max_dimension` validated before the pixels are read, so a corrupt file whose output path is already
  taken is reported as the path collision first; reading the pixels is what reports the corruption.
- **Memory.** A decoded image costs ≈ width × height × channels bytes (a 50 MP RGB image ≈ 150 MB),
  and a resize holds the source plus the result — budget ~3× the decoded size transiently. The caps
  are sized against that budget, not against the file size on disk: 100 MP summed over frames is
  ~300 MB decoded and ~900 MB transient, which is what this process can afford. That is why the
  per-frame cap is 50 MP.
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
5. Rotation looks doubled → the file carries an orientation tag its pixels no longer match, so
   whatever wrote it did not clear the tag. `image_rotate` writes orientation as 1 and `image_info`
   reports the tag it finds.
6. A write refused with "output already exists" → the default name is a function of (input,
   operation), so re-runs collide. Pass `output_path`, or `overwrite=true` with `confirm=true`.

## Tests

```bash
cd path/to/this/checkout
# offline
uv run --python 3.11 --with pytest --with 'pillow==12.3.0' --with pyyaml python -m pytest tests/ -q
```

Never `pip install` anything into the Hermes venv (its pins are load-bearing); `uv run` builds a
throwaway env from the uv cache. Offline tests generate all fixtures with Pillow itself (including
EXIF orientation via `Image.Exif()` — no piexif) and assert behaviour: the JSON envelopes, the bytes
on disk, mode-bit preservation, and that no temp file survives any failure path.

The fixture set covers what the format matrix can carry: a JPEG with EXIF/GPS/DPI/ICC, a PNG with
alpha, a header-only bomb-shaped PNG, an animated GIF and a multi-page TIFF. Checks against real
camera files are manual and are not part of the suite.

## Rollback

```bash
rm <profile-home>/plugins/image-utils
hermes -p <profile> plugins disable image-utils
hermes -p <profile> gateway restart
```

No state outside the files it writes; the plugin keeps no cache, no database and no config.

## Decisions and known limits

- The caps are per frame and summed: 50 MP per frame, 100 MP over all frames of one input. Every
  refusal message names the number it tripped, so the limit is never guessed from a generic error.
- Publishing is exclusive, not a check-then-replace: `os.link` (with an `O_CREAT|O_EXCL` fallback)
  fails if a target appeared while the encoder worked, so two concurrent writers cannot both report
  success with one file. The window is closed, not narrowed — only `overwrite=true` with
  `confirm=true` replaces an existing file.
- `confirm=true` is a model-supplied parameter, not a human approval: it is friction the model has to
  spend deliberately, and the response names the file it replaced. A `pre_tool_call` hook that blocks
  in-place overwrites outside an allow-listed directory tree is the v2 candidate; it is not
  implemented here.
- HEIC/HEIF read/write is out of scope; `image_convert` lists the seven supported targets.
- GIF frame surgery, drawing/text/watermarks, batch runs and colour correction are out of scope.
- `image_resize` re-encodes lossy targets at quality 95 unless the format is lossless; the note in
  the response says which quality was applied.
- Encoder defaults are picked for time, not inherited from Pillow. With no `effort`, WebP is written
  at `method=2` (Pillow's saver default is 4) and AVIF at `speed=8` (Pillow's default is 6), and the
  `notes` name the setting used. `effort=9` maps to WebP `method=5`, not 6: the last step is the
  slowest of the range for the smallest return. **To get the old behaviour back: `effort=4` selects
  AVIF `speed=6`, `effort=6` selects WebP `method=4`.** These defaults apply to *every* write tool,
  not only `image_convert` and `image_optimize`: `image_resize`, `image_crop` and `image_rotate` take
  no `effort` parameter, so they always use them.
- What the defaults cost and buy, measured (median of 3, interleaved before/after, a fresh process per
  run, one machine) on non-noise 12 MP fixtures — a smooth photo-like image and a flat block pattern:

  | case | before | after | speed | output bytes |
  |---|---|---|---|---|
  | WebP, photo-like | 556 ms | 282 ms | 1.97x | **+9.9%** |
  | WebP, flat pattern | 537 ms | 317 ms | 1.69x | **+17.6%** |
  | AVIF, photo-like | 412 ms | 237 ms | 1.74x | +2.2% |
  | AVIF, flat pattern | 715 ms | 293 ms | 2.44x | **+271%** |
  | `max_dimension=1600`, 12 MP JPEG | 144 ms | 64 ms | 2.24x | +0.02% |
  | 60-frame GIF -> JPEG, photo-like | 200 ms | 26 ms | 7.7x | identical |
  | 60-frame GIF -> JPEG, screen-capture-like | 99 ms | 25 ms | 4.0x | identical |
  | 2-frame GIF -> JPEG | 28 ms | 25 ms | 1.1x | identical |

  Read the byte column as a real cost, not a rounding error. A fast AVIF speed is worst exactly where
  the input compresses well: a flat 44 KB PNG became a 46 KB AVIF above, and the response warns
  whenever the output is larger than the input. The frame-walk saving scales with frame count and
  per-frame entropy — a 2-frame GIF gains almost nothing while a 60-frame one gains 4-8x — and its
  output is byte-identical in every case measured. Pass `effort` when bytes matter more than time.
