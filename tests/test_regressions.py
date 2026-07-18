from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import nonebot

nonebot.init()

from plugin_loader import load_send_package

load_send_package()

from amia_plugin_send.commands import _range
from amia_plugin_send.config import SendConfig
from amia_plugin_send.models import ActivityRecord, ActivityScope
from amia_plugin_send.service import ActivityService
from amia_plugin_send.writer import ActivityWriter
from amia_plugin_send.core_contract import get_core

_core = get_core()
ResolvedIdentity = _core.ResolvedIdentity
UserIdentityKey = _core.UserIdentityKey


class FailingStore:
    async def upsert_batch(self, records):
        raise RuntimeError("forced database failure")


class TestSendRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "test.db"
        self.config = SendConfig(
            db_path=self.db_path,
            adapter_instance_id="test-instance",
            bot_app_id="test-app",
            cross_context_user_id_stable=True,
            queue_size=10,
            batch_size=1,
            flush_interval_seconds=0.01,
            resolver_timeout_seconds=0.01,
            dead_letter_path=self.root / "dead-letter.jsonl",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _record(
        *,
        bot_id: str,
        user_id: str,
        canonical_id: str | None,
        activity_date: date,
    ) -> ActivityRecord:
        return ActivityRecord(
            activity_date=activity_date,
            activity_hour=12,
            scope=ActivityScope(
                adapter_type="onebot.v11",
                adapter_instance_id="test-instance",
                bot_id=bot_id,
                bot_app_id="test-app",
                scope_verified=True,
            ),
            context_type="group",
            context_id="group-1",
            gensokyo_user_id=user_id,
            canonical_user_id=canonical_id,
            display_name=user_id,
            message_bytes=100,
            occurred_at=datetime.now(),
        )

    def test_half_open_command_ranges_include_today(self) -> None:
        today = date(2026, 7, 11)
        self.assertEqual(
            _range("day", today),
            (today, today + timedelta(days=1)),
        )
        self.assertEqual(
            _range("month", today),
            (date(2026, 7, 1), date(2026, 7, 12)),
        )
        self.assertEqual(
            _range("year", today),
            (date(2026, 1, 1), date(2026, 7, 12)),
        )

    def test_user_activity_is_isolated_by_self_id(self) -> None:
        async def run_test() -> None:
            service = ActivityService(self.config)
            await service.start()
            today = date.today()
            tomorrow = today + timedelta(days=1)
            await service.store.upsert_batch(
                [
                    self._record(
                        bot_id="bot-1",
                        user_id="user-1",
                        canonical_id="canonical-1",
                        activity_date=today,
                    ),
                    self._record(
                        bot_id="bot-2",
                        user_id="user-1",
                        canonical_id="canonical-1",
                        activity_date=today,
                    ),
                ]
            )

            identity = ResolvedIdentity(
                UserIdentityKey(self_id="bot-1", user_id="user-1"),
                canonical_user_id="canonical-1",
            )
            result = await service.get_user_activity(
                identity,
                today,
                tomorrow,
            )
            self.assertEqual(result["message_count"], 1)
            await service.stop()

        asyncio.run(run_test())

    def test_merged_dau_reconciles_binding_history(self) -> None:
        async def run_test() -> None:
            service = ActivityService(self.config)
            await service.start()
            today = date.today()
            yesterday = today - timedelta(days=1)
            tomorrow = today + timedelta(days=1)
            await service.store.upsert_batch(
                [
                    self._record(
                        bot_id="bot-1",
                        user_id="user-1",
                        canonical_id=None,
                        activity_date=yesterday,
                    ),
                    self._record(
                        bot_id="bot-1",
                        user_id="user-1",
                        canonical_id="canonical-1",
                        activity_date=today,
                    ),
                ]
            )

            result = await service.get_merged_dau(yesterday, tomorrow)
            self.assertTrue(result["available"])
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["bound_count"], 1)
            self.assertEqual(result["unbound_count"], 0)
            await service.stop()

        asyncio.run(run_test())

    def test_failed_batch_is_written_to_dead_letter_file(self) -> None:
        async def run_test() -> None:
            writer = ActivityWriter(FailingStore(), self.config)
            await writer.start()
            writer.enqueue(
                self._record(
                    bot_id="bot-1",
                    user_id="user-1",
                    canonical_id=None,
                    activity_date=date.today(),
                )
            )
            await asyncio.sleep(0.6)
            await writer.stop()

            self.assertEqual(writer.failed_batches, 1)
            self.assertEqual(writer.failed_records, 1)
            self.assertTrue(self.config.dead_letter_path.exists())

            payload = json.loads(
                self.config.dead_letter_path.read_text(encoding="utf-8")
            )
            self.assertEqual(payload["error_type"], "RuntimeError")
            self.assertEqual(len(payload["records"]), 1)

        asyncio.run(run_test())

    def test_health_check_does_not_expose_storage_handles(self) -> None:
        async def run_test() -> None:
            service = ActivityService(self.config)
            self.assertEqual(
                await service.health_check(),
                {"ok": False, "status": "not_ready"},
            )
            await service.start()
            health = await service.health_check()
            self.assertTrue(health["ok"])
            self.assertTrue(health["scope_verified"])
            self.assertNotIn("connection", health)
            await service.stop()

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
