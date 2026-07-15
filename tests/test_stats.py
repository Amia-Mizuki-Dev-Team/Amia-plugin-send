import unittest
import asyncio
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta

import nonebot
nonebot.init()

from plugin_loader import load_send_package

load_send_package()

from amia_plugin_send.config import SendConfig
from amia_plugin_send.models import ActivityRecord, ActivityScope
from amia_plugin_send.storage import ActivityStore
from amia_plugin_send.service import ActivityService
from amia_plugin_send.core_contract import get_core

_core = get_core()
UserIdentityKey = _core.UserIdentityKey
ResolvedIdentity = _core.ResolvedIdentity

class TestSendStats(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.config = SendConfig(
            db_path=self.db_path,
            adapter_instance_id="test-instance",
            bot_app_id="test-app",
            cross_context_user_id_stable=True,
            queue_size=10,
            batch_size=1,
            flush_interval_seconds=0.01,
            resolver_timeout_seconds=0.01
        )
        self.service = ActivityService(self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_service_stats_methods(self):
        async def run_test():
            await self.service.start()
            today = self.service._today()
            tomorrow = today + timedelta(days=1)

            scope = ActivityScope(
                adapter_type="onebot.v11",
                adapter_instance_id="test-instance",
                bot_id="1111",
                bot_app_id="test-app",
                scope_verified=True
            )
            
            # Insert records directly
            records = [
                ActivityRecord(
                    activity_date=today,
                    activity_hour=12,
                    scope=scope,
                    context_type="group",
                    context_id="2222",
                    gensokyo_user_id="3333",
                    canonical_user_id="qq3333",
                    display_name="User3333",
                    message_bytes=500,
                    occurred_at=datetime.now()
                ),
                ActivityRecord(
                    activity_date=today,
                    activity_hour=13,
                    scope=scope,
                    context_type="group",
                    context_id="2222",
                    gensokyo_user_id="4444",
                    canonical_user_id=None,
                    display_name="User4444",
                    message_bytes=1500,
                    occurred_at=datetime.now()
                )
            ]
            
            await self.service.store.upsert_batch(records)

            # Test get_group_rank
            rank = await self.service.get_group_rank("1111", "2222", today, tomorrow)
            self.assertEqual(len(rank), 2)
            self.assertEqual(rank[0]["gensokyo_user_id"], "3333")

            # Test get_group_dau
            dau = await self.service.get_group_dau("1111", "2222", today)
            self.assertEqual(dau["count"], 2)

            # Test get_group_activity_summary
            summary = await self.service.get_group_activity_summary("1111", "2222", today, tomorrow)
            self.assertEqual(summary["message_count"], 2)
            self.assertEqual(summary["total_bytes"], 2000)
            self.assertEqual(summary["unique_users"], 2)

            # Test get_user_activity
            identity_key = UserIdentityKey("1111", "3333")
            identity = ResolvedIdentity(identity_key, "qq3333")
            user_act = await self.service.get_user_activity(identity, today, tomorrow)
            self.assertEqual(user_act["message_count"], 1)
            self.assertEqual(user_act["total_bytes"], 500)

            # Test get_instance_active_users
            active_u = await self.service.get_instance_active_users(today, tomorrow)
            self.assertEqual(active_u["count"], 2)

            # Test get_merged_dau
            merged = await self.service.get_merged_dau(today, tomorrow)
            self.assertEqual(merged["count"], 2)
            self.assertEqual(merged["bound_count"], 1)
            self.assertEqual(merged["unbound_count"], 1)

            # Test get_admin_dashboard_data
            dashboard = await self.service.get_admin_dashboard_data("1111", "day")
            self.assertEqual(dashboard["total_messages"], 2)
            self.assertEqual(dashboard["active_groups"], 1)

            await self.service.stop()

        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
