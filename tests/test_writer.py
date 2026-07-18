import unittest
import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from datetime import date, datetime

import nonebot
nonebot.init()

from plugin_loader import load_send_package

load_send_package()

from amia_plugin_send.config import SendConfig
from amia_plugin_send.models import ActivityRecord, ActivityScope
from amia_plugin_send.storage import ActivityStore
from amia_plugin_send.writer import ActivityWriter

class TestSendWriter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.config = SendConfig(
            db_path=self.db_path,
            adapter_instance_id="test-instance",
            bot_app_id="test-app",
            cross_context_user_id_stable=False,
            queue_size=10,
            batch_size=2,
            flush_interval_seconds=0.05,
            resolver_timeout_seconds=0.05
        )
        self.store = ActivityStore(self.db_path)
        self.writer = ActivityWriter(self.store, self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_store_initialization(self):
        async def run_test():
            await self.store.initialize()
            self.assertTrue(self.db_path.exists())
        asyncio.run(run_test())

    def test_writer_enqueue_and_persist(self):
        async def run_test():
            await self.store.initialize()
            await self.writer.start()

            scope = ActivityScope(
                adapter_type="onebot.v11",
                adapter_instance_id="test-instance",
                bot_id="1111",
                bot_app_id="test-app",
                scope_verified=True
            )
            record1 = ActivityRecord(
                activity_date=date.today(),
                activity_hour=12,
                scope=scope,
                context_type="group",
                context_id="2222",
                gensokyo_user_id="3333",
                canonical_user_id=None,
                display_name="User3333",
                message_bytes=512,
                occurred_at=datetime.now()
            )
            record2 = ActivityRecord(
                activity_date=date.today(),
                activity_hour=12,
                scope=scope,
                context_type="group",
                context_id="2222",
                gensokyo_user_id="4444",
                canonical_user_id=None,
                display_name="User4444",
                message_bytes=1024,
                occurred_at=datetime.now()
            )

            self.writer.enqueue(record1)
            self.writer.enqueue(record2)

            # Give it a moment to flush and process
            await asyncio.sleep(0.15)
            await self.writer.stop()

            # Check in DB
            rows = await self.store.fetch_all("SELECT gensokyo_user_id, message_count, total_bytes FROM activity_daily", ())
            self.assertEqual(len(rows), 2)
            row_map = {r[0]: (r[1], r[2]) for r in rows}
            self.assertEqual(row_map["3333"], (1, 512))
            self.assertEqual(row_map["4444"], (1, 1024))

        asyncio.run(run_test())

    def test_message_dedupe_key_counts_same_event_once(self):
        async def run_test():
            await self.store.initialize()
            record = ActivityRecord(
                activity_date=date.today(),
                activity_hour=12,
                scope=ActivityScope(
                    adapter_type="onebot.v11",
                    adapter_instance_id="test-instance",
                    bot_id="1111",
                    bot_app_id="test-app",
                    scope_verified=True,
                ),
                context_type="group",
                context_id="2222",
                gensokyo_user_id="3333",
                canonical_user_id=None,
                display_name="User3333",
                message_bytes=512,
                occurred_at=datetime.now(),
                message_id="message-1",
                dedupe_key="dedupe-1",
            )
            await self.store.upsert_batch([record, replace(record)])

            daily = await self.store.fetch_one(
                "SELECT message_count FROM activity_daily", ()
            )
            messages = await self.store.fetch_one(
                "SELECT COUNT(*) FROM activity_messages", ()
            )
            self.assertEqual(daily, (1,))
            self.assertEqual(messages, (1,))

        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
