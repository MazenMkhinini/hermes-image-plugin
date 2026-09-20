"""Contract tests for the Hermes ``image-utils`` plugin.

These cover the rules that neither ``hermes plugins doctor`` nor ``hermes plugins validate`` can see
(plan review A-7/A-13): source-level guarantees (no shell, no pillow_heif, no MAX_IMAGE_PIXELS
assignment), the cross-toolset naming rule, the JSON envelopes, the Pillow-absent behaviour, and
README/SKILL parity. Everything else is asserted through behaviour in ``test_image_utils.py``.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

import helpers  # noqa: E402
import schemas  # noqa: E402
import tools  # noqa: E402
import yaml  # noqa: E402

import image_utils_plugin_stub  # noqa: E402  (see conftest-free helper below)


def _manifest() -> dict:
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())


# --------------------------------------------------------------------------- schemas / manifest

def test_provides_tools_matches_schemas_and_handlers():
    manifest = _manifest()
    schema_names = [s["name"] for s in schemas.all_schemas()]
    assert manifest["provides_tools"] == schema_names
    for name in schema_names:
        assert callable(getattr(tools, name)), f"{name} has no handler"
    assert schema_names[0] == "image_info", "image_info must register first (toolset stays exposable)"


def test_manifest_required_fields_and_no_python_dependencies():
    manifest = _manifest()
    for key in ("name", "version", "description", "manifest_version", "kind"):
        assert manifest.get(key), f"plugin.yaml missing {key}"
    assert manifest["name"] == "image-utils"
    assert manifest["kind"] == "standalone"
    assert "python_dependencies" not in manifest          # A-14: Pillow is a venv fact, not a dep


def test_schema_shape_is_the_inner_function_object():
    for schema in schemas.all_schemas():
        assert set(schema) >= {"name", "description", "parameters"}
        assert isinstance(schema["parameters"], dict)
        assert schema["parameters"].get("type") == "object"
        assert schema["parameters"].get("properties")
        assert schema["description"].strip()


OTHER_TOOLSET_NAMES = {
    "vision_analyze", "image_generate", "video_generate", "browser_get_images", "web_search",
    "execute_code", "terminal", "read_file", "write_file", "search_files", "delegate_task",
    "session_search", "todo_list", "skill_view", "memory", "browser_exec",
}


def test_descriptions_name_no_tool_from_another_toolset():
    """tools/AGENTS.md:42 — such a tool may be unavailable and the model hallucinates the call."""
    for schema in schemas.all_schemas():
        text = schema["description"]
        hits = sorted(name for name in OTHER_TOOLSET_NAMES if name in text)
        assert hits == [], f"{schema['name']} description names {hits}"


# --------------------------------------------------------------------------- registration

def test_register_registers_six_tools_with_one_always_on_and_one_shared_probe():
    module = _load_plugin_module()
    stub = image_utils_plugin_stub.RecordingContext()
    module.register(stub)

    names = [entry["name"] for entry in stub.tools]
    assert names == [s["name"] for s in schemas.all_schemas()]
    assert stub.tools[0]["check_fn"] is None           # image_info is always visible
    probes = {id(entry["check_fn"]) for entry in stub.tools[1:]}
    assert len(probes) == 1 and None not in [
        entry["check_fn"] for entry in stub.tools[1:]]   # the five writes share one Pillow probe
    assert all(entry["toolset"] == "image_utils" for entry in stub.tools)
    assert stub.skills and stub.skills[0]["name"] == "image-utils"
    assert Path(stub.skills[0]["path"]).exists()
    assert stub.hooks == []


def test_check_fn_is_a_zero_arg_never_raising_probe():
    module = _load_plugin_module()
    fn = module._pillow_available
    assert len(inspect.signature(fn).parameters) == 0
    assert isinstance(fn(), bool)


def _load_plugin_module():
    import types

    module = types.ModuleType("image_utils_plugin_module")
    module.__file__ = str(PLUGIN_DIR / "__init__.py")
    code = compile((PLUGIN_DIR / "__init__.py").read_text(), str(PLUGIN_DIR / "__init__.py"), "exec")
    exec(code, module.__dict__)
    return module


# --------------------------------------------------------------------------- Pillow-absent

def test_image_info_stays_visible_and_explains_a_missing_pillow(monkeypatch):
    monkeypatch.setattr(tools, "_PIL_AVAILABLE", False)
    payload = json.loads(tools.image_info(path="/nonexistent.jpg"))
    assert payload["pillow_available"] is False
    assert "Pillow" in payload["error"] and payload["how_to_fix"]


def test_write_tools_refuse_cleanly_without_pillow(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_PIL_AVAILABLE", False)
    src = helpers.make_photo(tmp_path / "p.jpg", size=(40, 40))
    payload = json.loads(tools.image_resize(path=str(src), percent=50))
    assert "Pillow" in payload["error"] and payload["how_to_fix"]
    assert list(tmp_path.iterdir()) == [src]


def test_pillow_probe_is_false_when_pillow_is_gone(monkeypatch):
    module = _load_plugin_module()
    module._load_modules()
    monkeypatch.setattr(module.tools, "_PIL_AVAILABLE", False)
    assert module._pillow_available() is False


# --------------------------------------------------------------------------- envelopes

@pytest.mark.parametrize("name", ["image_resize", "image_crop", "image_rotate", "image_convert", "image_optimize"])
def test_every_write_tool_refusal_has_error_and_how_to_fix(name, tmp_path):
    payload = json.loads(getattr(tools, name)(path=str(tmp_path / "missing.png")))
    assert set(payload) >= {"error", "how_to_fix"}
    assert payload["how_to_fix"].strip()


def test_success_envelope_shape(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(60, 60))
    payload = json.loads(tools.image_resize(path=str(src), percent=50))
    assert set(payload) >= {"input", "output", "before", "after", "replaced", "notes"}
    assert set(payload["before"]) == {"dimensions", "format", "mode", "bytes"}
    assert set(payload["after"]) == {"dimensions", "format", "mode", "bytes"}


def test_handlers_accept_kwargs_and_return_strings(tmp_path):
    src = helpers.make_photo(tmp_path / "p.jpg", size=(40, 40))
    raw = tools.image_info(path=str(src), session_id="x", task="y")
    assert isinstance(raw, str) and json.loads(raw)["format"] == "JPEG"


# --------------------------------------------------------------------------- source-level rules

SOURCE_FILES = ["tools.py", "schemas.py", "__init__.py"]


def _source(name: str) -> str:
    return (PLUGIN_DIR / name).read_text()


def test_no_shell_anywhere():
    forbidden = ["import subprocess", "subprocess.", "os.system", "Popen(",
                 "shutil.which", "pty.spawn", "os.execv", "os.execp", "os.popen"]
    for name in SOURCE_FILES:
        text = _source(name)
        hits = [token for token in forbidden if token in text]
        assert hits == [], f"{name} contains shell-ish tokens: {hits}"


def test_no_pillow_heif_import_and_no_max_image_pixels_assignment():
    for name in SOURCE_FILES:
        text = _source(name)
        assert "import pillow_heif" not in text
        assert "from pillow_heif" not in text
        assert "register_heif_opener" not in text
        assert not re.search(r"MAX_IMAGE_PIXELS\s*=", text), f"{name} assigns MAX_IMAGE_PIXELS"
        assert "LOAD_TRUNCATED_IMAGES =" not in text and "LOAD_TRUNCATED_IMAGES=" not in text


def test_tools_module_reports_cap_numbers_in_refusals():
    text = _source("tools.py")
    assert "50 * 1024 * 1024" in text and "50_000_000" in text and "100_000_000" in text


def test_guarded_pillow_import_shape():
    text = _source("tools.py")
    assert "try:" in text and "from PIL import" in text
    assert "_PIL_AVAILABLE = False" in text          # the fallback branch exists


# --------------------------------------------------------------------------- docs parity

def test_readme_and_skill_name_every_tool():
    manifest = _manifest()
    readme = (PLUGIN_DIR / "README.md").read_text()
    skill = (PLUGIN_DIR / "skills" / "image-utils" / "SKILL.md").read_text()
    for name in manifest["provides_tools"]:
        assert name in readme, f"README.md does not mention {name}"
        assert name in skill, f"SKILL.md does not mention {name}"
    frontmatter = skill.split("---")[1]
    assert re.search(r"^name:\s*image-utils\s*$", frontmatter, re.M), "SKILL.md frontmatter name mismatch"
