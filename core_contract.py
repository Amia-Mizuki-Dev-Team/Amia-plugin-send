"""Runtime bridge to the amia-core NoneBot plugin.

Cross-plugin imports must go through ``nonebot.require`` so the package does
not depend on a particular deployment directory such as ``src.plugins``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def get_core() -> Any:
    """Load the core contract through NoneBot's plugin loader."""

    from nonebot import require

    return require("amia_core")
