"""Load the hyphenated NoneBot plugin as an importable package for tests."""

from __future__ import annotations

import importlib.util
import importlib
import sys
from pathlib import Path


def install_core_require_shim() -> None:
    """Make standalone tests resolve the public Core plugin without loading all plugins."""
    import nonebot
    core = importlib.import_module("src" + ".plugins." + "amia_core")

    if getattr(nonebot.require, "_amia_core_test_shim", False):
        return

    real_require = nonebot.require

    def require(name: str):
        if name == "amia_core":
            return core
        return real_require(name)

    require._amia_core_test_shim = True
    nonebot.require = require


def load_send_package() -> None:
    name = "amia_plugin_send"
    if name in sys.modules:
        return
    install_core_require_shim()
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Amia-plugin-send")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
