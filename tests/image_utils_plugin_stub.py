"""A recording stub of Hermes' PluginContext, enough to observe what ``register(ctx)`` does.

Kept in the test tree (not the plugin package) so the plugin's own import surface stays exactly what
ships; this mirrors the probe used by ``hermes plugins doctor`` (which loads the plugin directly).
"""

from __future__ import annotations

from typing import Any, Dict, List


class RecordingContext:
    def __init__(self) -> None:
        self.tools: List[Dict[str, Any]] = []
        self.skills: List[Dict[str, Any]] = []
        self.hooks: List[str] = []

    def register_tool(self, name: str, toolset: str, schema: dict, handler: Any,
                      check_fn: Any = None, requires_env: Any = None, is_async: bool = False,
                      description: str = "", emoji: str = "", override: bool = False) -> None:
        self.tools.append({
            "name": name, "toolset": toolset, "schema": schema, "handler": handler,
            "check_fn": check_fn, "description": description,
        })

    def register_skill(self, name: str, path: Any, description: str = "", **kwargs: Any) -> None:
        self.skills.append({"name": name, "path": str(path), "description": description})

    def register_hook(self, event: str, callback: Any) -> None:
        self.hooks.append(event)

    def get_config(self, key: str, default: Any = None) -> Any:
        return default
