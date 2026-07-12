"""Auto-discovery: imports every module in leadpilot.tools (except
base.py and this file) so each one's @tool(...) decorator runs and
registers it. This is the piece that makes adding a tool a
zero-shared-file-edit operation — no hand-maintained import list here
to conflict on. See base.py's module docstring for why this exists.
"""

import importlib
import pkgutil

import leadpilot.tools as _tools_package
from leadpilot.tools.base import ToolSpec, all_tools

_EXCLUDED_MODULES = {"base", "registry"}


def load_all_tools() -> dict[str, ToolSpec]:
    for _, module_name, _ in pkgutil.iter_modules(_tools_package.__path__):
        if module_name not in _EXCLUDED_MODULES:
            importlib.import_module(f"leadpilot.tools.{module_name}")
    return all_tools()
