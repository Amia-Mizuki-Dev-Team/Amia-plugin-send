"""Offline baseline/candidate benchmark for Send's SQLite aggregation path."""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = next(
    (
        candidate
        for candidate in (ROOT, *ROOT.parents)
        if (candidate / "pyproject.toml").exists()
        and (candidate / "src" / "plugins" / "amia_core").exists()
    ),
    ROOT,
)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(PROJECT_ROOT))

import nonebot  # noqa: E402

nonebot.init()

from plugin_loader import load_send_package  # noqa: E402

load_send_package()

from amia_plugin_send.models import ActivityRecord, ActivityScope  # noqa: E402
from amia_plugin_send.storage import ActivityStore  # noqa: E402


def measure(function):
    async def run():
        for _ in range(2):
            await function()
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            await function()
            samples.append((time.perf_counter() - started) * 1000)
        return {
            "median_ms": statistics.median(samples),
            "min_ms": min(samples),
            "max_ms": max(samples),
        }

    return run


def make_records(count: int) -> list[ActivityRecord]:
    target_date = date(2026, 7, 18)
    scope = ActivityScope(
        adapter_type="onebot.v11",
        adapter_instance_id="benchmark-instance",
        bot_id="benchmark-bot",
        bot_app_id="benchmark-app",
        scope_verified=True,
    )
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    fields = set(getattr(ActivityRecord, "__dataclass_fields__", {}))
    records = []
    for index in range(count):
        values = {
            "activity_date": target_date,
            "activity_hour": index % 24,
            "scope": scope,
            "context_type": "group",
            "context_id": "benchmark-group",
            "gensokyo_user_id": f"user-{index}",
            "canonical_user_id": None,
            "display_name": f"User {index}",
            "message_bytes": 100,
            "occurred_at": now,
        }
        if "message_id" in fields:
            values["message_id"] = f"message-{index}"
        if "dedupe_key" in fields:
            values["dedupe_key"] = f"benchmark-dedupe-{index}"
        records.append(ActivityRecord(**values))
    return records


async def benchmark_write(count: int):
    records = make_records(count)

    async def run():
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / "send.db")
            await store.initialize()
            await store.upsert_batch(records)

    return await measure(run)()


async def benchmark_dau(count: int):
    records = make_records(count)

    async def run():
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / "send.db")
            await store.initialize()
            await store.upsert_batch(records)
            await store.fetch_one(
                """
                SELECT COUNT(DISTINCT COALESCE(
                    canonical_user_id, 'external:' || gensokyo_user_id
                ))
                FROM activity_daily
                WHERE adapter_instance_id=? AND bot_app_id=? AND date=?
                """,
                ("benchmark-instance", "benchmark-app", "2026-07-18"),
            )

    return await measure(run)()


async def benchmark_ranking(count: int):
    records = make_records(count)

    async def run():
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / "send.db")
            await store.initialize()
            await store.upsert_batch(records)
            await store.fetch_all(
                """
                SELECT gensokyo_user_id, SUM(message_count)
                FROM activity_daily
                WHERE adapter_instance_id=? AND bot_app_id=?
                  AND context_type='group' AND context_id=?
                GROUP BY gensokyo_user_id
                ORDER BY SUM(message_count) DESC, gensokyo_user_id ASC
                LIMIT 100
                """,
                ("benchmark-instance", "benchmark-app", "benchmark-group"),
            )

    return await measure(run)()


async def main_async() -> None:
    report = {
        "python": sys.version.split()[0],
        "write": {
            str(count): await benchmark_write(count)
            for count in (1000, 10000)
        },
        "dau": {
            str(count): await benchmark_dau(count)
            for count in (1000, 10000)
        },
        "ranking": {
            str(count): await benchmark_ranking(count)
            for count in (10000, 100000)
        },
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main_async())
