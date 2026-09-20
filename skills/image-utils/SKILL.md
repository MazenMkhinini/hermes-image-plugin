---
name: image-utils
description: "Use when working with local image files (inspect, resize, crop, rotate, convert, optimize): tool selection and the safety conventions of the image-utils plugin."
version: 1.0.0
---

# Image files — conventions for the `image_*` tools

Load this when the task involves an image file on disk: what it is, making it smaller, cutting it
down, turning it upright, changing its format, or re-encoding it. Install: plugin `image-utils`
(this repo, symlinked into the profile's `plugins/` directory) enabled per profile in
`plugins.enabled` (no `platform_toolsets` entry is needed — plugin toolsets are on by default).
Pillow runs in-process:
there is no shell, no ImageMagick, no network.

## Which tool for what

| Intent | Tool |
|---|---|
| "What is this file, how big, what camera, does it decode?" | `image_info` (always available; also the answer to "why are the image tools missing") |
| "Make it smaller / scale it to 800 px wide" | `image_resize` (one of `width` / `height` / `percent`) |
| "Cut out a region / square it up / crop to 16:9" | `image_crop` |
| "It's sideways / turn it 90°" | `image_rotate` (`auto_orient=true` fixes the EXIF case; omit `angle` to only fix orientation) |
| "Turn this PNG into a JPEG / a WebP" | `image_convert` |
| "Shrink the file without changing the format / cap the long side" | `image_optimize` |

## The rules that decide success or failure

- **A write needs a target that does not exist yet.** The default output is
  `<stem>_<operation>.<ext>` beside the input; because that name depends only on (input, operation),
  running the same tool twice collides and is refused. Prefer an explicit `output_path`. To replace
  an existing file (including the input, in place) you need **both** `overwrite=true` and
  `confirm=true` — one without the other is refused by design. `confirm` is your own parameter, not
  a human approval: state plainly in your reply when you replaced a file, and never set both flags
  on a household photo without saying so.
- **Everything is reported before → after.** Read `before`/`after` (dimensions, format, mode,
  bytes — `after` is read back from the file on disk), `frames_written`/`frames_dropped`, and
  `notes`; the notes are where "metadata dropped", "frames_dropped", "resample" and "effort mapping"
  appear. Animated inputs keep every frame through resize/crop/rotate/optimize/convert wherever the
  target format can hold them.
- **Refusals carry `how_to_fix`.** When a call comes back with `error` + `how_to_fix`, follow the
  fix instead of retrying the same arguments: the usual causes are a missing/zero-byte/truncated
  file, a parameter combination that is genuinely ambiguous (e.g. `width`+`height` without
  `allow_distort=true`), a crop box outside the image, or an output directory that does not exist
  (this plugin never creates directories).
- **Caps:** ≤ 50 MB file, ≤ 50 MP per frame, ≤ 100 MP total across frames — refuse, with the numbers
  in the message. A decompression-bomb-shaped file is refused at open, and `image_info` still
  answers with the header facts.
- **Never upscales by default** (`allow_upscale=true` if you really mean it); a crop cannot create
  pixels.
- **Alpha into JPEG/BMP/GIF must be declared**: pass `flatten=true` (optionally
  `background='#rrggbb'`) or convert to PNG/WebP. Without it the call is refused rather than
  dropping transparency silently.
- **Rotate clears the EXIF orientation** (writes 1) where the target can carry EXIF, because the
  pixels are made upright; other tools preserve EXIF/ICC/DPI as far as the target format allows (a
  TIFF source's orientation is applied by Pillow's reader at load and is not re-written). A format
  that cannot carry a block says so in `notes` (GIF/BMP drop EXIF+ICC; WebP/AVIF cannot carry DPI).
- **HEIC/HEIF does not work here** and no `pip install` will be suggested; convert such a file to
  JPEG/PNG with the device or an app that produced it.

## Examples

```
image_info(path="~/Pictures/IMG_2031.HEIC")            # refuses honestly, says what is decodable
image_resize(path="~/Pictures/scan.png", width=1200)   # keeps aspect, Lanczos, writes scan_resize.png
image_crop(path="~/Pictures/team.jpg", aspect="1:1")   # largest centred square
image_rotate(path="~/Pictures/beach.jpg")              # EXIF-orientation fix only
image_convert(path="~/Pictures/logo.png", format="webp", quality=80)
image_optimize(path="~/Pictures/IMG_2031.jpg", max_dimension=2048, quality=80)
```

Deeper reference: this repo's `README.md` — safety behaviour, caps, metadata honesty and the
decisions behind them.
