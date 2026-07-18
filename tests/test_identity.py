from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent

nonebot.init()

from plugin_loader import load_send_package

load_send_package()

from amia_plugin_send.config import SendConfig
from amia_plugin_send.identity import _dedupe_key, build_activity_record
from amia_plugin_send.models import ActivityScope


def make_event(*, user_id: int = 7, message_id: int = 12) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1720000000,
        self_id=42,
        post_type="message",
        message_type="group",
        sub_type="normal",
        user_id=user_id,
        message_id=message_id,
        message="hello",
        raw_message="hello",
        font=0,
        sender={"user_id": user_id, "nickname": "tester"},
        group_id=99,
    )


class TestSendIdentity(unittest.TestCase):
    def config(self) -> SendConfig:
        return SendConfig(
            db_path=Path("test.db"),
            adapter_instance_id="instance-1",
            bot_app_id="app-1",
            cross_context_user_id_stable=False,
            queue_size=10,
            batch_size=1,
            flush_interval_seconds=0.05,
            resolver_timeout_seconds=0.05,
            timezone_name="Asia/Shanghai",
        )

    def test_bot_messages_are_ignored_by_default(self) -> None:
        async def run_test() -> None:
            self.assertIsNone(
                await build_activity_record(make_event(user_id=42), self.config())
            )

        asyncio.run(run_test())

    def test_message_id_and_timezone_are_preserved_without_body_storage(self) -> None:
        async def run_test() -> None:
            record = await build_activity_record(make_event(), self.config())
            assert record is not None
            self.assertEqual(record.message_id, "12")
            self.assertIsNotNone(record.dedupe_key)
            self.assertIsInstance(record.occurred_at, datetime)
            self.assertIsNotNone(record.occurred_at.tzinfo)

        asyncio.run(run_test())

    def test_fallback_dedupe_key_is_stable_for_same_event_shape(self) -> None:
        scope = ActivityScope(
            adapter_type="onebot.v11",
            adapter_instance_id="instance-1",
            bot_id="42",
            bot_app_id="app-1",
            scope_verified=True,
        )
        first = _dedupe_key(
            make_event(message_id=0),
            config=self.config(),
            scope=scope,
            context_type="group",
            context_id="99",
        )
        second = _dedupe_key(
            make_event(message_id=0),
            config=self.config(),
            scope=scope,
            context_type="group",
            context_id="99",
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
