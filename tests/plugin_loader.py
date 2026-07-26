"""Load the hyphenated NoneBot plugin as an importable package for tests."""

from __future__ import annotations

import importlib.util
import importlib
import sys
import types
from pathlib import Path


def _load_standalone_core():
    """Load the sibling isolated Core only when the monorepo path is absent."""

    try:
        return importlib.import_module("src" + ".plugins." + "amia_core")
    except ModuleNotFoundError as exc:
        if exc.name not in {"src", "src.plugins", "src.plugins.amia_core"}:
            raise
    core_root = Path(__file__).resolve().parents[2] / "amia-core"
    if not (core_root / "__init__.py").is_file():
        raise ModuleNotFoundError(
            "src.plugins.amia_core is unavailable and sibling isolated amia-core was not found"
        )
    src_package = sys.modules.setdefault("src", types.ModuleType("src"))
    src_package.__path__ = [str(core_root.parent)]
    plugins_package = sys.modules.setdefault(
        "src.plugins", types.ModuleType("src.plugins")
    )
    plugins_package.__path__ = [str(core_root.parent)]
    spec = importlib.util.spec_from_file_location(
        "src.plugins.amia_core",
        core_root / "__init__.py",
        submodule_search_locations=[str(core_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load sibling isolated amia-core")
    module = importlib.util.module_from_spec(spec)
    sys.modules["src.plugins.amia_core"] = module
    spec.loader.exec_module(module)
    return module


def install_core_require_shim() -> None:
    """Make standalone tests resolve the public Core plugin without loading all plugins."""
    import nonebot
    core = _load_standalone_core()

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
