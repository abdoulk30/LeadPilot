"""Tool registration for the 11 PRD v1.05 §3a tools.

Built ahead of any actual tool implementation, specifically so two
people (Marc, Abdoul) can each build several tools in parallel without
both needing to edit the same file to "wire them in." Each tool gets
its own module in this package (e.g. tools/fetch_all_leads.py) and
registers itself with the @tool(...) decorator below — registry.py
auto-discovers every module in this package at import time (pkgutil,
not a hand-maintained import list), so adding a new tool file never
requires touching a file the other person might also be touching.

Deliberately SDK-agnostic. This is not yet wired to the actual Claude
Agent SDK — that integration is Step 4 ("Render Cron Job running the
full system-prompt sequence hourly"), and its exact shape depends on
that SDK's real tool-calling API, which this scaffold doesn't assume
anything specific about beyond "a tool has a name, a description, a
JSON input schema, and a Python function that runs it." Don't treat
input_schema's shape here as final — it may need adjusting once Step 4
wires this registry into the actual SDK call.

A tool's handler signature is intentionally not standardized across
tools — each tool's own parameters vary too much (some need only
session+rep_id, others need lead_id, message content, a target
row_ref, etc.) for one shared shape to make sense. What every handler
should share, per the execution-gating rule (PRD v1.05 3a): any tool
with a real-world side effect stages a draft via gate.create_draft and
must never call gate.try_execute itself — that authorization is the
rep's approval action, wired in Step 3, not something a tool grants
itself.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]


_REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, input_schema: dict):
    """Decorator: registers a tool by name. Raises immediately (at
    import time, not silently at call time) if two tool files ever
    claim the same name — the one real way two people's independent
    tool files could conflict, and this makes it loud rather than
    letting the second registration silently shadow the first.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise ValueError(
                f"Tool {name!r} is already registered (by {_REGISTRY[name].handler.__module__}) — "
                f"duplicate registration from {func.__module__}"
            )
        _REGISTRY[name] = ToolSpec(name=name, description=description, input_schema=input_schema, handler=func)
        return func

    return decorator


def all_tools() -> dict[str, ToolSpec]:
    return dict(_REGISTRY)


def reset_registry_for_tests() -> None:
    """Test-only. The registry is process-global by design (tools
    register once, at import time) — this exists so tests can verify
    registration/duplicate-detection behavior without permanently
    polluting the real registry for the rest of the test run.

    Callers that clear the registry mid-suite (test_tools_registry.py)
    are responsible for restoring whatever was there before — see that
    file's _clean_registry fixture, which snapshots before clearing and
    restores after. Blindly clearing with nothing to restore would
    permanently empty the registry for every test file that happens to
    run afterward: registry.py's load_all_tools() calls
    importlib.import_module(), which is a no-op for a module Python has
    already cached in sys.modules, so a tool's @tool(...) decorator
    would never re-run to re-register it.
    """
    _REGISTRY.clear()
