from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent

nonebot.init()

from plugin_loader import load_send_package

load_send_package()

from amia_plugin_send.config import SendConfig
from amia_plugin_send.core_contract import get_core
from amia_plugin_send.identity import (
    IDENTITY_STATUS_DISCOVERY_ERROR,
    IDENTITY_STATUS_FAILED,
    IDENTITY_STATUS_NO_RESOLVER,
    IDENTITY_STATUS_RESOLVED,
    IDENTITY_STATUS_TIMEOUT,
    IDENTITY_STATUS_UNBOUND,
)
from amia_plugin_send.service import ActivityService

_core = get_core()
ResolvedIdentity = _core.ResolvedIdentity


def make_event(*, user_id: int, message_id: int) -> GroupMessageEvent:
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


class _RegistryUnavailableError(RuntimeError):
    """Discovery failure used by the registry test double."""


class FakeResolver:
    """Test double for the public ``IdentityResolver`` protocol only.

    It touches no plugin private module; Send must work with any object that
    exposes ``resolve_identity``.
    """

    def __init__(
        self,
        canonical_user_id: str | None = "canonical-a",
        *,
        error: Exception | None = None,
        delay: float = 0.0,
        result: Any = None,
    ) -> None:
        self.canonical_user_id = canonical_user_id
        self.error = error
        self.delay = delay
        self.result = result
        self.calls: list[str] = []

    async def resolve_identity(self, key: Any) -> Any:
        self.calls.append(key.user_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return ResolvedIdentity(
            external_key=key,
            canonical_user_id=self.canonical_user_id,
        )


class GatedResolver(FakeResolver):
    """Resolver that parks inside ``resolve_identity`` until released."""

    def __init__(self, canonical_user_id: str | None = "canonical-a") -> None:
        super().__init__(canonical_user_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve_identity(self, key: Any) -> Any:
        self.calls.append(key.user_id)
        self.started.set()
        await self.release.wait()
        return ResolvedIdentity(
            external_key=key,
            canonical_user_id=self.canonical_user_id,
        )


class TestResolverDiscovery(unittest.TestCase):
    def _make_config(
        self,
        *,
        resolver_timeout_seconds: float = 0.05,
    ) -> SendConfig:
        root = Path(self.temp_dir.name)
        return SendConfig(
            db_path=root / "test.db",
            adapter_instance_id="test-instance",
            bot_app_id="test-app",
            cross_context_user_id_stable=True,
            queue_size=50,
            batch_size=1,
            flush_interval_seconds=0.01,
            resolver_timeout_seconds=resolver_timeout_seconds,
            dead_letter_path=root / "dead-letter.jsonl",
        )

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = self._make_config()
        self.registry = _core.registry
        self._previous_resolver = self.registry.get_identity_resolver()
        # The frozen Core registry has no public unregister; restore the slot
        # directly so no fake resolver leaks into another test module.
        self.registry._identity_resolver = None

    def tearDown(self) -> None:
        self.registry._identity_resolver = self._previous_resolver
        self.temp_dir.cleanup()

    @staticmethod
    async def _stop_and_rows(service: ActivityService) -> dict[str, tuple[Any, ...]]:
        # stop() joins the writer queue, so every enqueued record is on disk.
        await service.stop()
        rows = await service.store.fetch_all(
            """
            SELECT gensokyo_user_id, canonical_user_id, message_count
            FROM activity_daily
            """,
            (),
        )
        return {row[0]: row for row in rows}

    def test_resolver_registered_after_start_is_used_by_next_event(self) -> None:
        async def run_test() -> None:
            service = ActivityService(self.config)
            await service.start()
            # Startup must not pin a resolver or fabricate one.
            self.assertIsNone(self.registry.get_identity_resolver())
            self.assertEqual(service.resolver_diagnostics()["counts"], {})

            await service.record_event(make_event(user_id=801, message_id=1))
            self.assertEqual(
                service.resolver_status_counts,
                {IDENTITY_STATUS_NO_RESOLVER: 1},
            )

            late = FakeResolver("canonical-late")
            self.registry.register_identity_resolver(late, replace=True)

            await service.record_event(make_event(user_id=802, message_id=2))
            self.assertEqual(late.calls, ["802"])
            self.assertEqual(
                service.resolver_status_counts[IDENTITY_STATUS_RESOLVED],
                1,
            )

            rows = await self._stop_and_rows(service)
            self.assertIsNone(rows["801"][1])
            self.assertEqual(rows["802"][1], "canonical-late")
            self.assertEqual(rows["802"][2], 1)

        asyncio.run(run_test())

    def test_replaced_resolver_applies_to_the_next_event(self) -> None:
        async def run_test() -> None:
            first = FakeResolver("canonical-a")
            self.registry.register_identity_resolver(first, replace=True)
            service = ActivityService(self.config)
            await service.start()

            await service.record_event(make_event(user_id=811, message_id=11))
            second = FakeResolver("canonical-b")
            self.registry.register_identity_resolver(second, replace=True)
            await service.record_event(make_event(user_id=812, message_id=12))

            self.assertEqual(first.calls, ["811"])
            self.assertEqual(second.calls, ["812"])

            rows = await self._stop_and_rows(service)
            self.assertEqual(rows["811"][1], "canonical-a")
            self.assertEqual(rows["812"][1], "canonical-b")

        asyncio.run(run_test())

    def test_resolver_error_degrades_one_event_then_recovers(self) -> None:
        async def run_test() -> None:
            failing = FakeResolver(error=RuntimeError("forced resolver failure"))
            self.registry.register_identity_resolver(failing, replace=True)
            service = ActivityService(self.config)
            await service.start()

            # The failure is isolated to the event and reported, not raised.
            with self.assertLogs("amia_plugin_send.service", level="WARNING") as logs:
                await service.record_event(make_event(user_id=821, message_id=21))
            rendered = "\n".join(logs.output)
            self.assertIn("degraded", rendered)
            self.assertIn(IDENTITY_STATUS_FAILED, rendered)
            self.assertNotIn("821", rendered)
            self.assertEqual(
                service.resolver_status_counts,
                {IDENTITY_STATUS_FAILED: 1},
            )

            healthy = FakeResolver("canonical-recovered")
            self.registry.register_identity_resolver(healthy, replace=True)
            await service.record_event(make_event(user_id=822, message_id=22))
            self.assertEqual(healthy.calls, ["822"])
            self.assertEqual(
                service.resolver_diagnostics()["last_status"],
                IDENTITY_STATUS_RESOLVED,
            )

            rows = await self._stop_and_rows(service)
            self.assertIsNone(rows["821"][1])
            self.assertEqual(rows["822"][1], "canonical-recovered")

        asyncio.run(run_test())

    def test_resolver_timeout_and_invalid_result_are_counted_separately(self) -> None:
        async def run_test() -> None:
            slow = FakeResolver("canonical-slow", delay=0.5)
            self.registry.register_identity_resolver(slow, replace=True)
            service = ActivityService(self.config)
            await service.start()

            await service.record_event(make_event(user_id=831, message_id=31))
            self.assertEqual(
                service.resolver_status_counts,
                {IDENTITY_STATUS_TIMEOUT: 1},
            )

            # An object without a canonical id is an invalid result, not a
            # successful "unbound" answer.
            self.registry.register_identity_resolver(
                FakeResolver(result=object()),
                replace=True,
            )
            await service.record_event(make_event(user_id=832, message_id=32))
            self.assertEqual(service.resolver_status_counts[IDENTITY_STATUS_FAILED], 1)

            rows = await self._stop_and_rows(service)
            self.assertIsNone(rows["831"][1])
            self.assertIsNone(rows["832"][1])
            self.assertEqual(rows["831"][2], 1)
            self.assertEqual(rows["832"][2], 1)

        asyncio.run(run_test())

    def test_past_records_are_not_backfilled_after_registration(self) -> None:
        async def run_test() -> None:
            service = ActivityService(self.config)
            await service.start()

            first_event = make_event(user_id=901, message_id=41)
            await service.record_event(first_event)

            late = FakeResolver("canonical-late")
            self.registry.register_identity_resolver(late, replace=True)
            # Replaying the earlier event must hit the dedupe path instead of
            # retroactively filling its canonical id.
            await service.record_event(first_event)
            await service.record_event(make_event(user_id=902, message_id=42))

            rows = await self._stop_and_rows(service)
            self.assertIsNone(rows["901"][1])
            self.assertEqual(rows["901"][2], 1)
            self.assertEqual(rows["902"][1], "canonical-late")
            self.assertEqual(rows["902"][2], 1)
            self.assertEqual(sum(row[2] for row in rows.values()), 2)
            # The replay is resolved again (discovery is per event) but the
            # dedupe key drops it before any write, so history is not backfilled.
            self.assertEqual(late.calls, ["901", "902"])

        asyncio.run(run_test())

    def test_non_protocol_resolver_object_is_degraded_without_raising(self) -> None:
        async def run_test() -> None:
            # The registry performs no protocol check, so any object can be
            # registered; it must degrade this event, not explode the matcher.
            self.registry.register_identity_resolver(object(), replace=True)
            service = ActivityService(self.config)
            await service.start()

            await service.record_event(make_event(user_id=841, message_id=57))

            self.assertEqual(
                service.resolver_status_counts,
                {IDENTITY_STATUS_FAILED: 1},
            )
            rows = await self._stop_and_rows(service)
            self.assertIsNone(rows["841"][1])
            self.assertEqual(rows["841"][2], 1)

        asyncio.run(run_test())

    def test_unbound_result_is_not_reported_as_resolved(self) -> None:
        async def run_test() -> None:
            resolver = FakeResolver(None)
            self.registry.register_identity_resolver(resolver, replace=True)
            service = ActivityService(self.config)
            await service.start()

            await service.record_event(make_event(user_id=881, message_id=51))

            self.assertEqual(
                service.resolver_status_counts,
                {IDENTITY_STATUS_UNBOUND: 1},
            )
            self.assertEqual(
                service.resolver_diagnostics()["last_status"],
                IDENTITY_STATUS_UNBOUND,
            )
            rows = await self._stop_and_rows(service)
            self.assertIsNone(rows["881"][1])
            self.assertEqual(rows["881"][2], 1)

        asyncio.run(run_test())

    def test_discovery_error_degrades_without_losing_the_event(self) -> None:
        async def run_test() -> None:
            service = ActivityService(self.config)
            await service.start()

            def boom() -> Any:
                raise _RegistryUnavailableError

            self.registry.get_identity_resolver = boom
            try:
                with self.assertLogs(
                    "amia_plugin_send.service",
                    level="WARNING",
                ) as logs:
                    await service.record_event(
                        make_event(user_id=891, message_id=52)
                    )
            finally:
                del self.registry.get_identity_resolver

            self.assertIn(IDENTITY_STATUS_DISCOVERY_ERROR, "\n".join(logs.output))
            self.assertEqual(
                service.resolver_status_counts,
                {IDENTITY_STATUS_DISCOVERY_ERROR: 1},
            )
            self.assertIsNone(self.registry.get_identity_resolver())
            rows = await self._stop_and_rows(service)
            self.assertIsNone(rows["891"][1])
            self.assertEqual(rows["891"][2], 1)

        asyncio.run(run_test())

    def test_resolver_swapped_while_an_event_is_in_flight(self) -> None:
        async def run_test() -> None:
            first = GatedResolver("canonical-a")
            self.registry.register_identity_resolver(first, replace=True)
            # A generous budget keeps the gate from racing the resolver timeout.
            service = ActivityService(
                self._make_config(resolver_timeout_seconds=5.0)
            )
            await service.start()

            in_flight = asyncio.create_task(
                service.record_event(make_event(user_id=851, message_id=53))
            )
            await first.started.wait()

            second = FakeResolver("canonical-b")
            self.registry.register_identity_resolver(second, replace=True)
            first.release.set()
            await in_flight
            await service.record_event(make_event(user_id=852, message_id=54))

            # The in-flight event keeps the resolver it started with; the next
            # event uses the replacement.
            self.assertEqual(first.calls, ["851"])
            self.assertEqual(second.calls, ["852"])
            rows = await self._stop_and_rows(service)
            self.assertEqual(rows["851"][1], "canonical-a")
            self.assertEqual(rows["852"][1], "canonical-b")

        asyncio.run(run_test())

    def test_inner_timeout_error_counts_as_failed_not_timeout(self) -> None:
        async def run_test() -> None:
            resolver = FakeResolver(error=TimeoutError("inner timeout"))
            self.registry.register_identity_resolver(resolver, replace=True)
            service = ActivityService(self.config)
            await service.start()

            await service.record_event(make_event(user_id=861, message_id=55))

            self.assertEqual(
                service.resolver_status_counts,
                {IDENTITY_STATUS_FAILED: 1},
            )
            rows = await self._stop_and_rows(service)
            self.assertIsNone(rows["861"][1])
            self.assertEqual(rows["861"][2], 1)

        asyncio.run(run_test())

    def test_health_check_reports_identity_diagnostics_without_identities(
        self,
    ) -> None:
        async def run_test() -> None:
            service = ActivityService(self.config)
            await service.start()
            await service.record_event(make_event(user_id=871, message_id=56))

            health = await service.health_check()
            self.assertEqual(
                set(health),
                {"ok", "status", "scope_verified", "identity"},
            )
            self.assertEqual(
                set(health["identity"]),
                {"mode", "last_status", "counts"},
            )
            self.assertEqual(health["identity"]["mode"], "per_event")
            self.assertEqual(
                health["identity"]["counts"],
                {IDENTITY_STATUS_NO_RESOLVER: 1},
            )
            rendered = str(health)
            self.assertNotIn("871", rendered)
            self.assertNotIn("tester", rendered)
            await service.stop()

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
