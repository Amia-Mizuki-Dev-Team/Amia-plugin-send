from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nonebot.adapters.onebot.v11 import MessageEvent
from .config import SendConfig, load_send_config
from .core_contract import get_core
from .identity import build_activity_record
from .models import ActivityScope
from .storage import ActivityStore, LegacyDatabaseDetected
from .writer import ActivityWriter


logger = logging.getLogger(__name__)


class ActivityService:
    def __init__(self, config: SendConfig | None = None) -> None:
        self.config = config or load_send_config()
        self.store = ActivityStore(self.config.db_path)
        self.writer = ActivityWriter(self.store, self.config)
        self.resolver: Any | None = None
        self.ready = False

    def _today(self) -> date:
        try:
            timezone = ZoneInfo(self.config.timezone_name)
        except ZoneInfoNotFoundError:
            logger.warning(
                "unknown AMIA_TIMEZONE %r; falling back to Asia/Shanghai",
                self.config.timezone_name,
            )
            timezone = ZoneInfo("Asia/Shanghai")
        return datetime.now(timezone).date()

    async def start(self) -> None:
        try:
            await self.store.initialize()
        except LegacyDatabaseDetected:
            logger.error(
                "send v2 did not start because the configured database is "
                "legacy; no data was changed"
            )
            return

        core = get_core()
        if self.resolver is None:
            self.resolver = core.UnresolvedIdentityResolver()

        resolver = core.registry.get_identity_resolver()
        if resolver:
            self.resolver = resolver

        core.registry.register_stats_provider("send", self, replace=True)
        await self.writer.start()
        self.ready = True

    async def stop(self) -> None:
        if self.ready:
            await self.writer.stop()
        self.ready = False

    async def record_event(self, event: MessageEvent) -> None:
        if not self.ready:
            return
        record = await build_activity_record(
            event,
            self.config,
            self.resolver,
        )
        if record is not None:
            self.writer.enqueue(record)

    def _scope(self, bot_id: str | None = None) -> ActivityScope:
        return ActivityScope(
            adapter_type="onebot.v11",
            adapter_instance_id=self.config.adapter_instance_id,
            bot_id=bot_id or "*",
            bot_app_id=self.config.bot_app_id or "unverified",
            scope_verified=bool(self.config.bot_app_id),
        )

    async def get_user_activity(
        self,
        identity: Any,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        """Return user activity in the current bot scope and half-open range."""
        scope = identity.external_key
        common_params = (
            self.config.adapter_instance_id,
            self.config.bot_app_id or "unverified",
            scope.self_id,
        )

        if identity.canonical_user_id:
            row = await self.store.fetch_one(
                """
                SELECT COALESCE(SUM(message_count), 0),
                       COALESCE(SUM(total_bytes), 0)
                FROM activity_daily
                WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
                  AND canonical_user_id=? AND date >= ? AND date < ?
                """,
                (
                    *common_params,
                    identity.canonical_user_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
            )
        else:
            row = await self.store.fetch_one(
                """
                SELECT COALESCE(SUM(message_count), 0),
                       COALESCE(SUM(total_bytes), 0)
                FROM activity_daily
                WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
                  AND gensokyo_user_id=? AND date >= ? AND date < ?
                """,
                (
                    *common_params,
                    scope.user_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
            )

        return {
            "message_count": row[0] if row else 0,
            "total_bytes": row[1] if row else 0,
        }

    async def get_group_rank(
        self,
        bot_id: str,
        context_id: str,
        start_date: date,
        end_date: date,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        scope = self._scope(bot_id)
        safe_limit = max(1, min(int(limit), 100))
        rows = await self.store.fetch_all(
            """
            SELECT COALESCE(canonical_user_id, 'external:' || gensokyo_user_id),
                   MAX(gensokyo_user_id), MAX(display_name),
                   SUM(message_count), SUM(total_bytes)
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND context_id=?
              AND date >= ? AND date < ?
            GROUP BY COALESCE(canonical_user_id, 'external:' || gensokyo_user_id)
            ORDER BY SUM(message_count) DESC,
                     MIN(gensokyo_user_id) ASC
            LIMIT ?
            """,
            (
                scope.adapter_instance_id,
                scope.bot_app_id,
                scope.bot_id,
                context_id,
                start_date.isoformat(),
                end_date.isoformat(),
                safe_limit,
            ),
        )
        return [
            {
                "identity_key": row[0],
                "gensokyo_user_id": row[1],
                "display_name": row[2] or row[1] or row[0],
                "message_count": row[3],
                "total_bytes": row[4],
            }
            for row in rows
        ]

    async def get_group_ranking(
        self,
        bot_id: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        scope = self._scope(bot_id)
        rows = await self.store.fetch_all(
            """
            SELECT context_id, SUM(message_count), COUNT(DISTINCT COALESCE(
                canonical_user_id, 'external:' || gensokyo_user_id
            ))
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND date >= ? AND date < ?
            GROUP BY context_id
            ORDER BY SUM(message_count) DESC, context_id ASC
            """,
            (
                scope.adapter_instance_id,
                scope.bot_app_id,
                scope.bot_id,
                start_date.isoformat(),
                end_date.isoformat(),
            ),
        )
        return [
            {
                "group_id": row[0],
                "message_count": row[1],
                "unique_users": row[2],
            }
            for row in rows
        ]

    async def get_group_dau(
        self,
        bot_id: str,
        context_id: str,
        target_date: date,
    ) -> dict[str, Any]:
        scope = self._scope(bot_id)
        row = await self.store.fetch_one(
            """
            SELECT COUNT(DISTINCT COALESCE(
                canonical_user_id,
                'external:' || gensokyo_user_id
            ))
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND context_id=? AND date=?
            """,
            (
                scope.adapter_instance_id,
                scope.bot_app_id,
                scope.bot_id,
                context_id,
                target_date.isoformat(),
            ),
        )
        return {
            "available": True,
            "count": row[0] if row else 0,
            "definition": "group scoped DAU",
        }

    async def get_group_activity_summary(
        self,
        self_id: str,
        group_id: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        row = await self.store.fetch_one(
            """
            SELECT COALESCE(SUM(message_count), 0),
                   COALESCE(SUM(total_bytes), 0),
                   COUNT(DISTINCT COALESCE(
                       canonical_user_id,
                       'external:' || gensokyo_user_id
                   ))
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND context_id=?
              AND date >= ? AND date < ?
            """,
            (
                self.config.adapter_instance_id,
                self.config.bot_app_id or "unverified",
                self_id,
                group_id,
                start_date.isoformat(),
                end_date.isoformat(),
            ),
        )
        return {
            "message_count": row[0] if row else 0,
            "total_bytes": row[1] if row else 0,
            "unique_users": row[2] if row else 0,
        }

    async def get_user_activity_summary(
        self,
        bot_id: str,
        context_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        scope = self._scope(bot_id)
        row = await self.store.fetch_one(
            """
            SELECT COALESCE(SUM(message_count), 0),
                   COALESCE(SUM(total_bytes), 0),
                   MAX(display_name)
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND context_id=?
              AND gensokyo_user_id=? AND date >= ? AND date < ?
            """,
            (
                scope.adapter_instance_id,
                scope.bot_app_id,
                scope.bot_id,
                context_id,
                user_id,
                start_date.isoformat(),
                end_date.isoformat(),
            ),
        )
        return {
            "message_count": row[0],
            "total_bytes": row[1],
            "display_name": row[2] or user_id,
        }

    async def get_instance_active_users(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        if (
            not self.config.cross_context_user_id_stable
            or not self.config.bot_app_id
        ):
            return {
                "available": False,
                "count": None,
                "reason": "identity stability is not verified",
            }

        row = await self.store.fetch_one(
            """
            SELECT COUNT(DISTINCT COALESCE(
                canonical_user_id,
                'external:' || gensokyo_user_id
            ))
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=?
              AND date >= ? AND date < ?
            """,
            (
                self.config.adapter_instance_id,
                self.config.bot_app_id,
                start_date.isoformat(),
                end_date.isoformat(),
            ),
        )
        return {
            "available": True,
            "count": row[0] if row else 0,
            "definition": "verified instance active users",
        }

    async def get_merged_dau(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        if (
            not self.config.bot_app_id
            or not self.config.cross_context_user_id_stable
        ):
            return {
                "available": False,
                "count": None,
                "reason": "cross-context user identity is not verified",
            }

        identity_rows = await self.store.fetch_all(
            """
            SELECT gensokyo_user_id,
                   COUNT(DISTINCT canonical_user_id),
                   MIN(canonical_user_id)
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=?
              AND canonical_user_id IS NOT NULL
            GROUP BY gensokyo_user_id
            """,
            (
                self.config.adapter_instance_id,
                self.config.bot_app_id,
            ),
        )

        conflicts = [row[0] for row in identity_rows if row[1] > 1]
        if conflicts:
            return {
                "available": False,
                "count": None,
                "reason": "conflicting historical identity mappings",
                "conflict_count": len(conflicts),
            }

        canonical_by_user = {
            user_id: canonical_id
            for user_id, _, canonical_id in identity_rows
            if canonical_id
        }

        rows = await self.store.fetch_all(
            """
            SELECT canonical_user_id, gensokyo_user_id
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=?
              AND date >= ? AND date < ?
            """,
            (
                self.config.adapter_instance_id,
                self.config.bot_app_id,
                start_date.isoformat(),
                end_date.isoformat(),
            ),
        )

        keys: set[str] = set()
        bound: set[str] = set()
        unbound: set[str] = set()
        for canonical_id, user_id in rows:
            reconciled_id = canonical_id or canonical_by_user.get(user_id)
            if reconciled_id:
                bound.add(reconciled_id)
                keys.add(f"c:{reconciled_id}")
            else:
                unbound.add(user_id)
                keys.add(f"g:{user_id}")

        return {
            "available": True,
            "count": len(keys),
            "bound_count": len(bound),
            "unbound_count": len(unbound),
            "warning": (
                "users without a historical canonical mapping remain "
                "unmerged"
            ),
        }

    async def get_admin_dashboard_data(
        self,
        bot_id: str,
        mode: str,
    ) -> dict[str, Any]:
        today = self._today()
        end_exclusive = today + timedelta(days=1)
        start = (
            today
            if mode == "day"
            else (
                today.replace(day=1)
                if mode == "month"
                else today.replace(month=1, day=1)
            )
        )
        scope = self._scope(bot_id)
        row = await self.store.fetch_one(
            """
            SELECT COALESCE(SUM(message_count), 0),
                   COALESCE(SUM(total_bytes), 0),
                   COUNT(DISTINCT context_id)
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND date >= ? AND date < ?
            """,
            (
                scope.adapter_instance_id,
                scope.bot_app_id,
                scope.bot_id,
                start.isoformat(),
                end_exclusive.isoformat(),
            ),
        )
        instance = await self.get_instance_active_users(
            start,
            end_exclusive,
        )
        return {
            "total_messages": row[0],
            "total_bytes": row[1],
            "active_groups": row[2],
            "instance_dau": instance,
        }

    async def health_check(self) -> dict[str, Any]:
        """Return a small operational status without exposing the DB handle."""

        if not self.ready:
            return {"ok": False, "status": "not_ready"}
        try:
            row = await self.store.fetch_one("SELECT 1", ())
        except Exception as exc:  # noqa: BLE001 - health must not raise
            return {
                "ok": False,
                "status": "database_error",
                "error_type": type(exc).__name__,
            }
        return {
            "ok": row == (1,),
            "status": "ready" if row == (1,) else "database_error",
            "scope_verified": bool(self.config.bot_app_id),
        }


activity_service = ActivityService()
