"""Hermes plugin: image-utils — local image-file tools (info, resize, crop, rotate, convert, optimize).

Design notes (guarded Pillow import + one always-on diagnostic tool + one shared availability probe):

- ``tools.py`` imports Pillow **guarded**, so a missing/unimportable Pillow cannot make the plugin
  register nothing (plan review S-3/A-2). ``image_info`` is registered WITHOUT a ``check_fn`` so it
  stays visible as the diagnostic and reports ``pillow_available``; the five write tools share one
  Pillow-availability probe whose result the registry caches for 30 s.
- Toolset ``image_utils``. No config, no secrets, no network, no shell: Pillow in-process only.
- ``register()`` is exception-safe: a failure here logs and registers nothing rather than taking the
  gateway process down with it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger("hermes_image_utils")

_PLUGIN_DIR = Path(__file__).resolve().parent
TOOLSET = "image_utils"
_ALWAYS_ON = "image_info"


def _load_modules():
    """Import the plugin's own modules, tolerating both package and bare-module loading."""
    global tools, schemas
    try:
        from . import schemas as _schemas
        from . import tools as _tools
    except ImportError:  # loaded as a bare module (test harnesses, doctor probes)
        import sys

        if str(_PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(_PLUGIN_DIR))
        import schemas as _schemas  # type: ignore
        import tools as _tools  # type: ignore
    tools, schemas = _tools, _schemas


def _pillow_available() -> bool:
    """Availability probe: is Pillow importable for THIS process? Fail closed, never raise."""
    try:
        return bool(tools._PIL_AVAILABLE)
    except Exception:  # pragma: no cover
        return False


def _make_handler(fn: Callable[..., str]) -> Callable[..., str]:
    def handler(params: Dict[str, Any] | None = None, **kwargs: Any) -> str:
        del kwargs  # Hermes passes task/session context the handlers do not need
        try:
            return fn(**(params or {}))
        except Exception as exc:  # handlers catch their own errors; this is belt-and-braces
            logger.exception("image-utils: handler %s failed", getattr(fn, "__name__", "?"))
            return json.dumps({"error": f"{type(exc).__name__}: {exc}",
                               "how_to_fix": "unexpected failure; check the gateway log for 'hermes_image_utils'"})

    return handler


def register(ctx) -> None:
    try:
        _load_modules()

        handlers = {
            "image_info": tools.image_info,
            "image_resize": tools.image_resize,
            "image_crop": tools.image_crop,
            "image_rotate": tools.image_rotate,
            "image_convert": tools.image_convert,
            "image_optimize": tools.image_optimize,
        }

        probe = _pillow_available
        registered = []
        for schema in schemas.all_schemas():  # image_info is first: keeps the toolset exposable (A-2)
            name = schema["name"]
            handler = handlers.get(name)
            if handler is None:
                logger.warning("image-utils: schema %s has no handler; skipped", name)
                continue
            ctx.register_tool(
                name=name,
                toolset=TOOLSET,
                schema=schema,
                handler=_make_handler(handler),
                check_fn=None if name == _ALWAYS_ON else probe,
                description=schema.get("description", ""),
            )
            registered.append(name)

        skill_path = _PLUGIN_DIR / "skills" / "image-utils" / "SKILL.md"
        if skill_path.exists():
            try:
                ctx.register_skill("image-utils", skill_path,
                                   description="Image file conventions and tool selection")
            except Exception as exc:
                logger.warning("image-utils: bundled skill not registered: %s", exc)

        logger.debug("image-utils plugin: registered %d tools (%s)", len(registered), ", ".join(registered))
    except Exception:
        # Never let an image tool stop a gateway from starting.
        logger.exception("image-utils plugin failed to register; no image tools are available in this process")
