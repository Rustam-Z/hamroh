"""Tool plugin package — drop a new file here, subclass BaseTool, and you're done.

The MCP server auto-discovers every ``BaseTool`` subclass found in modules in
this package at startup. No registry edits required. Discovery recurses into
subpackages (e.g. ``browser/`` and ``telegram/``), so tools can be grouped in
folders without any registration change.

Bot-specific tools live outside the framework, in an *extension* package
named in ``plugins.json`` → ``extensions``. Such a package is walked exactly
like this one, so its tools are registered next to the built-ins.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterable

from .base import BaseTool


def discover_tool_classes(extensions: Iterable[str] = ()) -> list[type[BaseTool]]:
    """Return every concrete BaseTool subclass in ``hamroh.tools`` and in each
    extension package, in that order. Subpackages are descended into; the
    abstract ``base``/``BrowserSessionTool`` bases are skipped."""
    found: list[type[BaseTool]] = []
    seen: set[str] = set()
    for package in (__name__, *extensions):
        for cls in _walk_package(package):
            if cls.__name__ in seen:
                continue
            seen.add(cls.__name__)
            found.append(cls)
    return found


def _walk_package(package: str) -> list[type[BaseTool]]:
    """Import every module under ``package`` (recursively) and collect the
    concrete BaseTool subclasses defined in them."""
    path = importlib.import_module(package).__path__
    found: list[type[BaseTool]] = []
    for mod_info in pkgutil.walk_packages(path, prefix=f"{package}."):
        if mod_info.ispkg or mod_info.name.rsplit(".", 1)[-1] == "base":
            continue
        module = importlib.import_module(mod_info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseTool) or obj is BaseTool:
                continue
            if inspect.isabstract(obj):
                continue
            found.append(obj)
    return found
