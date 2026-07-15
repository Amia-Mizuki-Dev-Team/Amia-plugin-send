import sys
import types
import unittest

from core_contract import get_core


class TestCoreContract(unittest.TestCase):
    def test_core_is_loaded_via_nonebot_require(self) -> None:
        calls: list[str] = []
        fake_nonebot = types.ModuleType("nonebot")

        def fake_require(plugin_id: str):
            calls.append(plugin_id)
            return types.SimpleNamespace(plugin_id=plugin_id)

        fake_nonebot.require = fake_require
        original = sys.modules.get("nonebot")
        sys.modules["nonebot"] = fake_nonebot
        try:
            core = get_core()
        finally:
            if original is None:
                sys.modules.pop("nonebot", None)
            else:
                sys.modules["nonebot"] = original

        self.assertEqual(core.plugin_id, "amia_core")
        self.assertEqual(calls, ["amia_core"])


if __name__ == "__main__":
    unittest.main()
