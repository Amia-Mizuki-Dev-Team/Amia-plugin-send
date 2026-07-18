from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import ActivityRecord


SCHEMA_VERSION = "send-activity-v2-0002"


class LegacyDatabaseDetected(RuntimeError):
    """Raised instead of mutating an unmigrated legacy send database."""


async def create_schema(db: aiosqlite.Connection) -> None:
    """Create the current schema one statement at a time.

    Keeping each DDL statement explicit is important for migration rollback:
    SQLite's ``executescript`` may commit an open transaction before running
    the script.
    """

    statements = (
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS activity_daily (
            date TEXT NOT NULL,
            adapter_type TEXT NOT NULL,
            adapter_instance_id TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            bot_app_id TEXT NOT NULL,
            context_type TEXT NOT NULL CHECK(context_type IN ('group', 'private')),
            context_id TEXT NOT NULL,
            gensokyo_user_id TEXT NOT NULL,
            canonical_user_id TEXT,
            display_name TEXT,
            message_count INTEGER NOT NULL DEFAULT 0 CHECK(message_count >= 0),
            total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes >= 0),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            legacy_source TEXT,
            PRIMARY KEY (
                date, adapter_type, adapter_instance_id, bot_id, bot_app_id,
                context_type, context_id, gensokyo_user_id
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS activity_hourly (
            date TEXT NOT NULL,
            hour INTEGER NOT NULL CHECK(hour BETWEEN 0 AND 23),
            adapter_type TEXT NOT NULL,
            adapter_instance_id TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            bot_app_id TEXT NOT NULL,
            context_type TEXT NOT NULL CHECK(context_type IN ('group', 'private')),
            context_id TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (
                date, hour, adapter_type, adapter_instance_id, bot_id,
                bot_app_id, context_type, context_id
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS legacy_daily_metrics (
            date TEXT NOT NULL,
            adapter_type TEXT NOT NULL,
            adapter_instance_id TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            bot_app_id TEXT NOT NULL,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            source_table TEXT NOT NULL,
            PRIMARY KEY (
                date, adapter_type, adapter_instance_id, bot_id,
                bot_app_id, source_table
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS legacy_hourly_metrics (
            date TEXT NOT NULL,
            hour INTEGER NOT NULL CHECK(hour BETWEEN 0 AND 23),
            message_count INTEGER NOT NULL,
            source_table TEXT NOT NULL,
            PRIMARY KEY (date, hour, source_table)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS activity_messages (
            dedupe_key TEXT PRIMARY KEY,
            message_id TEXT,
            occurred_at TEXT NOT NULL,
            adapter_type TEXT NOT NULL,
            adapter_instance_id TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            bot_app_id TEXT NOT NULL,
            context_type TEXT NOT NULL CHECK(context_type IN ('group', 'private')),
            context_id TEXT NOT NULL,
            gensokyo_user_id TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_group_range ON activity_daily (
            adapter_instance_id, bot_app_id, bot_id, context_type, context_id, date
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_instance_dau ON activity_daily (
            adapter_instance_id, bot_app_id, date, gensokyo_user_id
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_canonical_dau ON activity_daily (
            adapter_instance_id, bot_app_id, date, canonical_user_id
        ) WHERE canonical_user_id IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_user_range ON activity_daily (
            adapter_instance_id, bot_app_id, gensokyo_user_id, date
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_hour_range ON activity_hourly (
            adapter_instance_id, bot_app_id, context_type, context_id, date, hour
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_messages_scope ON activity_messages (
            adapter_instance_id, bot_app_id, bot_id, context_type, context_id,
            occurred_at
        )
        """,
    )
    for statement in statements:
        await db.execute(statement)


class ActivityStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cursor:
                existing_tables = {row[0] for row in await cursor.fetchall()}
            legacy_tables = {
                "msg_stats",
                "private_stats",
                "hourly_stats",
                "traffic_stats",
            }
            if legacy_tables.intersection(existing_tables) and "activity_daily" not in existing_tables:
                raise LegacyDatabaseDetected(
                    "legacy send database detected; run migrations.v0001_activity_v2.dry_run first"
                )
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS activity_daily (
                    date TEXT NOT NULL,
                    adapter_type TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    bot_app_id TEXT NOT NULL,
                    context_type TEXT NOT NULL CHECK(context_type IN ('group', 'private')),
                    context_id TEXT NOT NULL,
                    gensokyo_user_id TEXT NOT NULL,
                    canonical_user_id TEXT,
                    display_name TEXT,
                    message_count INTEGER NOT NULL DEFAULT 0 CHECK(message_count >= 0),
                    total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes >= 0),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    legacy_source TEXT,
                    PRIMARY KEY (
                        date, adapter_type, adapter_instance_id, bot_id, bot_app_id,
                        context_type, context_id, gensokyo_user_id
                    )
                );
                CREATE TABLE IF NOT EXISTS activity_hourly (
                    date TEXT NOT NULL,
                    hour INTEGER NOT NULL CHECK(hour BETWEEN 0 AND 23),
                    adapter_type TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    bot_app_id TEXT NOT NULL,
                    context_type TEXT NOT NULL CHECK(context_type IN ('group', 'private')),
                    context_id TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (
                        date, hour, adapter_type, adapter_instance_id, bot_id,
                        bot_app_id, context_type, context_id
                    )
                );
                CREATE TABLE IF NOT EXISTS legacy_daily_metrics (
                    date TEXT NOT NULL,
                    adapter_type TEXT NOT NULL,
                    adapter_instance_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    bot_app_id TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    source_table TEXT NOT NULL,
                    PRIMARY KEY (
                        date, adapter_type, adapter_instance_id, bot_id,
                        bot_app_id, source_table
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_activity_group_range ON activity_daily (
                    adapter_instance_id, bot_app_id, bot_id, context_type, context_id, date
                );
                CREATE INDEX IF NOT EXISTS idx_activity_instance_dau ON activity_daily (
                    adapter_instance_id, bot_app_id, date, gensokyo_user_id
                );
                CREATE INDEX IF NOT EXISTS idx_activity_canonical_dau ON activity_daily (
                    adapter_instance_id, bot_app_id, date, canonical_user_id
                ) WHERE canonical_user_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_activity_user_range ON activity_daily (
                    adapter_instance_id, bot_app_id, gensokyo_user_id, date
                );
                CREATE INDEX IF NOT EXISTS idx_activity_hour_range ON activity_hourly (
                    adapter_instance_id, bot_app_id, context_type, context_id, date, hour
                );
                """
            )
            await create_schema(db)
            await db.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, checksum, status, details_json)
                VALUES (?, ?, 'applied', ?)
                """,
                (SCHEMA_VERSION, "schema-created-in-code", json.dumps({"mode": "fresh"})),
            )
            await db.commit()

    async def upsert_batch(self, records: Iterable[ActivityRecord]) -> None:
        rows = list(records)
        if not rows:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            for record in rows:
                timestamp = record.occurred_at.isoformat(timespec="seconds")
                scope = record.scope
                if record.dedupe_key:
                    cursor = await db.execute(
                        """
                        INSERT OR IGNORE INTO activity_messages (
                            dedupe_key, message_id, occurred_at, adapter_type,
                            adapter_instance_id, bot_id, bot_app_id,
                            context_type, context_id, gensokyo_user_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.dedupe_key,
                            record.message_id,
                            timestamp,
                            scope.adapter_type,
                            scope.adapter_instance_id,
                            scope.bot_id,
                            scope.bot_app_id,
                            record.context_type,
                            record.context_id,
                            record.gensokyo_user_id,
                        ),
                    )
                    if cursor.rowcount == 0:
                        continue
                await db.execute(
                    """
                    INSERT INTO activity_daily (
                        date, adapter_type, adapter_instance_id, bot_id, bot_app_id,
                        context_type, context_id, gensokyo_user_id, canonical_user_id,
                        display_name, message_count, total_bytes, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(
                        date, adapter_type, adapter_instance_id, bot_id, bot_app_id,
                        context_type, context_id, gensokyo_user_id
                    ) DO UPDATE SET
                        canonical_user_id=COALESCE(excluded.canonical_user_id, activity_daily.canonical_user_id),
                        display_name=COALESCE(excluded.display_name, activity_daily.display_name),
                        message_count=activity_daily.message_count + 1,
                        total_bytes=activity_daily.total_bytes + excluded.total_bytes,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        record.activity_date.isoformat(), scope.adapter_type,
                        scope.adapter_instance_id, scope.bot_id, scope.bot_app_id,
                        record.context_type, record.context_id, record.gensokyo_user_id,
                        record.canonical_user_id, record.display_name, record.message_bytes,
                        timestamp, timestamp,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO activity_hourly (
                        date, hour, adapter_type, adapter_instance_id, bot_id, bot_app_id,
                        context_type, context_id, message_count, total_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(
                        date, hour, adapter_type, adapter_instance_id, bot_id,
                        bot_app_id, context_type, context_id
                    ) DO UPDATE SET
                        message_count=activity_hourly.message_count + 1,
                        total_bytes=activity_hourly.total_bytes + excluded.total_bytes
                    """,
                    (
                        record.activity_date.isoformat(), record.activity_hour,
                        scope.adapter_type, scope.adapter_instance_id, scope.bot_id,
                        scope.bot_app_id, record.context_type, record.context_id,
                        record.message_bytes,
                    ),
                )
            await db.commit()

    async def fetch_one(self, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, params) as cursor:
                return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, params) as cursor:
                return await cursor.fetchall()

    async def legacy_dry_run(self) -> dict[str, Any]:
        """Read-only preflight for a legacy send SQLite database.

        It deliberately does not copy data or alter old tables.
        """
        if not self.db_path.exists():
            return {"ok": True, "tables": {}, "message": "no existing database"}
        expected = ("msg_stats", "private_stats", "hourly_stats", "traffic_stats")
        result: dict[str, Any] = {"ok": True, "tables": {}, "db_path": str(self.db_path.resolve()), "file_size": self.db_path.stat().st_size}
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("PRAGMA integrity_check") as cursor:
                integrity = await cursor.fetchone()
            result["integrity_check"] = integrity[0] if integrity else "unknown"
            if result["integrity_check"] != "ok":
                result["ok"] = False
                result["reason"] = "sqlite integrity_check failed"
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
                names = {row[0] for row in await cursor.fetchall()}
            result["backup_tables"] = sorted(name for name in names if name.endswith("_legacy_bak"))
            result["new_tables"] = sorted(
                name
                for name in names
                if name
                in {
                    "activity_daily",
                    "activity_hourly",
                    "activity_messages",
                    "legacy_daily_metrics",
                    "schema_migrations",
                }
            )
            active_legacy_tables = set(expected).intersection(names)
            if result["backup_tables"] and not active_legacy_tables and "activity_daily" in names:
                result["already_migrated"] = True
                return result
            if result["backup_tables"]:
                result["ok"] = False
                result["reason"] = "legacy backup tables already exist"
            if result["new_tables"] and any(name in names for name in expected):
                result["ok"] = False
                result["reason"] = "partial migration state detected"
            for table in expected:
                async with db.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row or row[0] == 0:
                    result["tables"][table] = {"exists": False}
                    continue
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                    count = await cursor.fetchone()
                result["tables"][table] = {"exists": True, "rows": count[0]}
        return result
