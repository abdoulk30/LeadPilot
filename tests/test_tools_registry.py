"""Real tests for the tool-registration scaffold — including one that
actually writes a temporary module file to disk and confirms
load_all_tools() discovers and registers it, rather than trusting
pkgutil's behavior by reasoning alone.
"""

import sys
import uuid
from pathlib import Path

import pytest

from leadpilot.tools import base
from leadpilot.tools.base import ToolSpec, all_tools, reset_registry_for_tests, tool
from leadpilot.tools.registry import load_all_tools

TOOLS_DIR = Path(base.__file__).parent


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot-and-restore, not just clear-on-both-sides. By the time
    any test runs, every real tools/*.py module has already been
    imported (each test_<tool>.py file imports it at module scope,
    which pytest resolves during collection) and thus already
    registered. reset_registry_for_tests() clearing _REGISTRY doesn't
    make those modules re-import — load_all_tools() would see them
    already in sys.modules and skip re-running their @tool(...)
    decorators — so without restoring the snapshot here, every real
    tool would silently vanish from the registry for the rest of the
    test session the first time this file's tests run.
    """
    snapshot = all_tools()
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()
    for spec in snapshot.values():
        base._REGISTRY[spec.name] = spec


def test_tool_decorator_registers_by_name():
    @tool(name="example_tool", description="does a thing", input_schema={"type": "object"})
    def handler(x):
        return x

    registered = all_tools()
    assert "example_tool" in registered
    assert registered["example_tool"].description == "does a thing"
    assert registered["example_tool"].handler is handler


def test_duplicate_tool_name_raises_at_registration_time():
    @tool(name="dup", description="first", input_schema={})
    def first():
        pass

    with pytest.raises(ValueError, match="already registered"):

        @tool(name="dup", description="second", input_schema={})
        def second():
            pass


def test_toolspec_is_immutable():
    spec = ToolSpec(name="x", description="y", input_schema={}, handler=lambda: None)
    with pytest.raises(AttributeError):
        spec.name = "changed"


def test_load_all_tools_really_discovers_a_new_module_on_disk():
    """The actual proof: write a real .py file into the real tools/
    directory, confirm load_all_tools() picks it up and registers its
    tool — not a simulation of what pkgutil should do.
    """
    module_name = f"_test_probe_{uuid.uuid4().hex[:8]}"
    module_path = TOOLS_DIR / f"{module_name}.py"
    module_path.write_text(
        "from leadpilot.tools.base import tool\n"
        "\n"
        "@tool(name='probe_tool', description='temporary test probe', input_schema={})\n"
        "def run():\n"
        "    return 'ran'\n"
    )
    try:
        discovered = load_all_tools()
        assert "probe_tool" in discovered
        assert discovered["probe_tool"].handler() == "ran"
    finally:
        module_path.unlink()
        sys.modules.pop(f"leadpilot.tools.{module_name}", None)
