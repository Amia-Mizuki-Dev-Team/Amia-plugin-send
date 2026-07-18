import unittest
import asyncio
import tempfile
import aiosqlite
import json
from pathlib import Path
from unittest.mock import patch

import nonebot
nonebot.init()

from plugin_loader import load_send_package

load_send_package()

from amia_plugin_send.storage import ActivityStore, LegacyDatabaseDetected
from amia_plugin_send.migrations.v0001_activity_v2 import dry_run, migrate

class TestSendMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.store = ActivityStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def _setup_legacy_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE msg_stats (
                    date TEXT, group_id TEXT, user_id TEXT, count INTEGER, 
                    PRIMARY KEY (date, group_id, user_id)
                );
                CREATE TABLE private_stats (
                    date TEXT, user_id TEXT, count INTEGER,
                    PRIMARY KEY (date, user_id)
                );
                CREATE TABLE hourly_stats (
                    date TEXT, hour INTEGER, count INTEGER,
                    PRIMARY KEY (date, hour)
                );
                CREATE TABLE traffic_stats (
                    date TEXT PRIMARY KEY, total_bytes INTEGER
                );

                INSERT INTO msg_stats VALUES ('2026-07-09', 'group1', 'user1', 5);
                INSERT INTO msg_stats VALUES ('2026-07-09', 'group1', 'user2', 12);
                INSERT INTO private_stats VALUES ('2026-07-09', 'user3', 20);
                INSERT INTO hourly_stats VALUES ('2026-07-09', 14, 37);
                INSERT INTO traffic_stats VALUES ('2026-07-09', 1048576);
                """
            )

    def test_legacy_detection(self):
        async def run_test():
            await self._setup_legacy_db()
            with self.assertRaises(LegacyDatabaseDetected):
                await self.store.initialize()
        asyncio.run(run_test())

    def test_migration_dry_run_and_execution(self):
        async def run_test():
            await self._setup_legacy_db()

            # Test dry run
            dr = await dry_run(self.db_path)
            self.assertTrue(dr["ok"])
            self.assertEqual(dr["tables"]["msg_stats"]["rows"], 2)
            self.assertEqual(dr["tables"]["private_stats"]["rows"], 1)
            self.assertEqual(dr["tables"]["hourly_stats"]["rows"], 1)
            self.assertEqual(dr["tables"]["traffic_stats"]["rows"], 1)

            # Test migrate execution
            await migrate(
                db_path=self.db_path,
                adapter_instance_id="test-instance",
                bot_app_id="test-app",
                bot_id="1111"
            )

            # Re-running store.initialize() should succeed now (legacy tables are renamed)
            await self.store.initialize()

            # Verify daily activity table
            rows_daily = await self.store.fetch_all(
                "SELECT context_type, context_id, gensokyo_user_id, message_count, legacy_source FROM activity_daily ORDER BY context_id", ()
            )
            self.assertEqual(len(rows_daily), 3)
            
            # Mapping results
            group_rows = [r for r in rows_daily if r[0] == "group"]
            self.assertEqual(len(group_rows), 2)
            self.assertEqual(group_rows[0][1], "group1")
            self.assertEqual(group_rows[0][2], "user1")
            self.assertEqual(group_rows[0][3], 5)
            self.assertEqual(group_rows[0][4], "msg_stats")

            private_rows = [r for r in rows_daily if r[0] == "private"]
            self.assertEqual(len(private_rows), 1)
            self.assertEqual(private_rows[0][1], "user3")
            self.assertEqual(private_rows[0][2], "user3")
            self.assertEqual(private_rows[0][3], 20)
            self.assertEqual(private_rows[0][4], "private_stats")

            # Legacy hourly data has no reliable group/private context, so it
            # stays in the non-semantic legacy metrics table.
            rows_hourly = await self.store.fetch_all(
                "SELECT hour, message_count FROM legacy_hourly_metrics", ()
            )
            self.assertEqual(len(rows_hourly), 1)
            self.assertEqual(rows_hourly[0][0], 14)
            self.assertEqual(rows_hourly[0][1], 37)

            # Verify legacy metrics table
            rows_metrics = await self.store.fetch_all(
                "SELECT total_bytes, source_table FROM legacy_daily_metrics", ()
            )
            self.assertEqual(len(rows_metrics), 1)
            self.assertEqual(rows_metrics[0][0], 1048576)
            self.assertEqual(rows_metrics[0][1], "traffic_stats")

            # Verify old tables renamed
            async def check_table(t_name):
                r = await self.store.fetch_one("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (t_name,))
                return r[0] == 1

            self.assertTrue(await check_table("msg_stats_legacy_bak"))
            self.assertTrue(await check_table("private_stats_legacy_bak"))
            self.assertTrue(await check_table("hourly_stats_legacy_bak"))
            self.assertTrue(await check_table("traffic_stats_legacy_bak"))

            # Original legacy tables should no longer exist
            self.assertFalse(await check_table("msg_stats"))

            # A completed migration is a safe no-op and does not require
            # deleting the preserved legacy backup tables.
            before = self.db_path.stat().st_size
            self.assertTrue((await dry_run(self.db_path))["already_migrated"])
            await migrate(
                db_path=self.db_path,
                adapter_instance_id="test-instance",
                bot_app_id="test-app",
                bot_id="1111",
            )
            self.assertEqual(self.db_path.stat().st_size, before)

        asyncio.run(run_test())

    def test_migration_rolls_back_schema_when_creation_fails(self):
        async def run_test():
            await self._setup_legacy_db()
            from amia_plugin_send.migrations import v0001_activity_v2 as migration

            async def fail_schema(_db):
                raise RuntimeError("forced schema failure")

            with patch.object(migration, "create_schema", fail_schema):
                with self.assertRaises(RuntimeError):
                    await migration.migrate(
                        db_path=self.db_path,
                        adapter_instance_id="test-instance",
                        bot_app_id="test-app",
                        bot_id="1111",
                    )

            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ) as cursor:
                    names = {row[0] for row in await cursor.fetchall()}
            self.assertIn("msg_stats", names)
            self.assertNotIn("activity_daily", names)

        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
