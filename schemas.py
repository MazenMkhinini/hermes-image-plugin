"""LLM-facing tool schemas for the Hermes ``image-utils`` plugin.

Conventions that must live in the schema text (the bundled skill is load-on-demand only, and plugin
skills are not listed in ``<available_skills>``): every safety rule the model has to respect *before*
calling a tool is stated here — default output naming, the overwrite/confirm gate, the caps, and the
format/metadata behaviour.

This module deliberately imports no Pillow: the schema text is static, and a broken Pillow must not
stop the plugin from registering.
"""

from __future__ import annotations

from typing import Any, Dict, List

TOOLSET = "image_utils"

SAVE_FORMATS_TEXT = "PNG|JPEG|WEBP|TIFF|GIF|AVIF|BMP"


def _obj(props: Dict[str, Any], required: List[str] | None = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


_PATH = {
    "type": "string",
    "description": "Path to the image file (absolute, or ~-expanded). Must be an existing regular file.",
}
_OUTPUT_PATH = {
    "type": "string",
    "description": (
        "Where to write the result. Default: beside the input as '<stem>_<operation>.<ext>'. "
        "Refused if it already exists — or equals the input — unless overwrite=true AND confirm=true."
    ),
}
_OVERWRITE = {
    "type": "boolean",
    "description": (
        "Allow replacing an existing file (the default output name collides on a re-run). "
        "Requires confirm=true. Default false."
    ),
}
_CONFIRM = {
    "type": "boolean",
    "description": (
        "Second key for overwrite=true: both must be true or nothing is written. The response then "
        "names what was replaced. Note: this is a parameter, not a human approval step."
    ),
}


def all_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "image_info",
            "description": (
                "Read-only facts about one image file: dimensions, format, mode, frame count, DPI, "
                "whether an ICC profile and EXIF are present, camera make/model, EXIF datetime, "
                "orientation tag, whether GPS data is present (presence only — coordinates are never "
                "returned), file size and whether the pixels actually decode. Always available, and "
                "the tool to call when the other image tools seem missing: it reports whether Pillow "
                "is importable and it answers for damaged, truncated or over-limit files instead of "
                "refusing. Writes nothing."
            ),
            "parameters": _obj({"path": _PATH}, ["path"]),
        },
        {
            "name": "image_resize",
            "description": (
                "Resize an image to new dimensions. Pass exactly one of width / height / percent; "
                "aspect ratio is preserved from that single value (Lanczos by default; a JPEG "
                "downscale drafts before decoding, so the JPEG is decoded at the reduced scale). "
                "Passing both width and height requires allow_distort=true, otherwise the call is "
                "refused as ambiguous. Never upscales unless allow_upscale=true, and the target is "
                "capped at 50 MP like an input. The output stays in the input's format and keeps "
                "EXIF/ICC/DPI as far as that format allows. Animated input keeps every frame. "
                "Reports before -> after (dimensions, format, mode, bytes) and the output path."
            ),
            "parameters": _obj(
                {
                    "path": _PATH,
                    "width": {"type": "integer", "description": "Target width in pixels (> 0)."},
                    "height": {"type": "integer", "description": "Target height in pixels (> 0)."},
                    "percent": {"type": "number", "description": "Scale factor in percent, e.g. 50 for half size."},
                    "allow_distort": {
                        "type": "boolean",
                        "description": "Allow width+height to change the aspect ratio (default false).",
                    },
                    "allow_upscale": {
                        "type": "boolean",
                        "description": "Allow making the image larger than the source (default false).",
                    },
                    "resample": {
                        "type": "string",
                        "enum": ["lanczos", "bicubic", "bilinear", "nearest"],
                        "description": "Resampling filter (default lanczos).",
                    },
                    "output_path": _OUTPUT_PATH,
                    "overwrite": _OVERWRITE,
                    "confirm": _CONFIRM,
                },
                ["path"],
            ),
        },
        {
            "name": "image_crop",
            "description": (
                "Crop an image. Pass exactly one mode: 'box' = [left, top, right, bottom] in pixels "
                "(validated against the image bounds — an out-of-range box is refused instead of "
                "silently padded with black); 'aspect' = '16:9' / '1:1' / '4:3' largest centred "
                "rectangle of that ratio; or a centred 'width' and/or 'height' (must fit — a crop "
                "never creates pixels). Output stays in the input's format, keeps EXIF/ICC/DPI as "
                "far as that format allows, and an animated input keeps every frame."
            ),
            "parameters": _obj(
                {
                    "path": _PATH,
                    "box": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                        "description": "[left, top, right, bottom] in pixels; 0 <= left < right <= width, 0 <= top < bottom <= height.",
                    },
                    "aspect": {
                        "type": "string",
                        "description": "Centred aspect crop, e.g. '1:1', '16:9', '4:3' (largest rectangle that fits).",
                    },
                    "width": {"type": "integer", "description": "Centred crop width in pixels (with height, a centred box of that size)."},
                    "height": {"type": "integer", "description": "Centred crop height in pixels."},
                    "output_path": _OUTPUT_PATH,
                    "overwrite": _OVERWRITE,
                    "confirm": _CONFIRM,
                },
                ["path"],
            ),
        },
        {
            "name": "image_rotate",
            "description": (
                "Rotate an image counter-clockwise by angle degrees (90/180/270 use the exact, "
                "lossless transpose; other angles expand the canvas and leave background-coloured "
                "corners, transparent for images with alpha — the fill is shaped per mode and a "
                "palette-transparent P image is rotated as RGBA so its transparency survives). "
                "auto_orient=true (default) first normalises the pixels using the file's EXIF "
                "orientation tag; the written file gets orientation cleared (written as 1) where the "
                "target format can carry EXIF, and says so in the notes when it cannot (GIF/BMP). "
                "Omit 'angle' to only fix the EXIF orientation. Output stays in the input's format "
                "and an animated input keeps every frame."
            ),
            "parameters": _obj(
                {
                    "path": _PATH,
                    "angle": {"type": "number", "description": "Degrees counter-clockwise, e.g. 90, 180, 270, 45."},
                    "auto_orient": {
                        "type": "boolean",
                        "description": "Apply the EXIF orientation to the pixels first (default true).",
                    },
                    "expand": {
                        "type": "boolean",
                        "description": "Grow the canvas so nothing is cut off (default true; applies to non-90° angles).",
                    },
                    "resample": {
                        "type": "string",
                        "enum": ["lanczos", "bicubic", "bilinear", "nearest"],
                        "description": "Resampling filter for arbitrary angles (default bicubic; 90/180/270 never resample).",
                    },
                    "background": {
                        "type": "string",
                        "description": "Fill colour for the corners of an arbitrary-angle rotation: 'white' (default), 'black', '#rrggbb'. Images with alpha are filled transparently unless this is set.",
                    },
                    "output_path": _OUTPUT_PATH,
                    "overwrite": _OVERWRITE,
                    "confirm": _CONFIRM,
                },
                ["path"],
            ),
        },
        {
            "name": "image_convert",
            "description": (
                f"Convert an image to another format: {SAVE_FORMATS_TEXT} (whichever Pillow supports). "
                "Alpha into JPEG/BMP/GIF must be declared: without flatten=true the call is refused rather "
                "than silently dropping transparency, and with it the image is composited onto "
                "'background' (default white). Animated inputs write all frames when the target "
                "format supports animation (GIF/WebP/AVIF/TIFF/PNG), otherwise the first frame with "
                "frames_dropped reported. Says which metadata the target format cannot carry (GIF "
                "and BMP drop EXIF/ICC; WebP and AVIF cannot carry DPI). HEIC/HEIF is out of scope."
            ),
            "parameters": _obj(
                {
                    "path": _PATH,
                    "format": {
                        "type": "string",
                        "enum": ["PNG", "JPEG", "WEBP", "TIFF", "GIF", "AVIF", "BMP", "png", "jpeg", "jpg", "webp", "tiff", "tif", "gif", "avif", "bmp"],
                        "description": "Target format.",
                    },
                    "quality": {
                        "type": "integer",
                        "description": "Encoder quality 1-100 for JPEG/WebP/AVIF (default 85). Ignored with a note for formats that have no quality setting.",
                    },
                    "effort": {
                        "type": "integer",
                        "description": "Encoder effort 0-9 where the format has one: WebP method, AVIF speed, PNG compress_level, JPEG optimize/progressive, TIFF deflate. The mapping applied is reported.",
                    },
                    "flatten": {
                        "type": "boolean",
                        "description": "Required to convert an image with alpha into JPEG/BMP/GIF: composites onto 'background'. Default false (refused instead of losing transparency).",
                    },
                    "background": {
                        "type": "string",
                        "description": "Flatten background colour: 'white' (default), 'black', '#rrggbb'.",
                    },
                    "strip_metadata": {
                        "type": "boolean",
                        "description": "Do not write EXIF/ICC/DPI (default false — dates are usually wanted).",
                    },
                    "output_path": _OUTPUT_PATH,
                    "overwrite": _OVERWRITE,
                    "confirm": _CONFIRM,
                },
                ["path", "format"],
            ),
        },
        {
            "name": "image_optimize",
            "description": (
                "Re-encode an image in its own format to shrink it: 'quality' for JPEG/WebP/AVIF, "
                "'effort' where the encoder has one, and optional 'max_dimension' to cap the longest "
                "side (Lanczos, never upscales). Metadata is kept unless strip_metadata=true, in "
                "which case the response says so. Reports before -> after bytes and flags when the "
                "result came out larger than the input."
            ),
            "parameters": _obj(
                {
                    "path": _PATH,
                    "quality": {"type": "integer", "description": "Encoder quality 1-100 (default 85)."},
                    "effort": {"type": "integer", "description": "Encoder effort 0-9 where supported; the mapping applied is reported."},
                    "max_dimension": {
                        "type": "integer",
                        "description": "Cap the longest side at this many pixels (Lanczos; no upscale; no-op if the image is already smaller).",
                    },
                    "strip_metadata": {
                        "type": "boolean",
                        "description": "Remove EXIF/ICC/DPI (default false; stated in the response when true).",
                    },
                    "output_path": _OUTPUT_PATH,
                    "overwrite": _OVERWRITE,
                    "confirm": _CONFIRM,
                },
                ["path"],
            ),
        },
    ]
