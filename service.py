from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any

from nonebot.adapters.onebot.v11 import MessageEvent

from src.plugins.amia_core.identity import IdentityResolver, UnresolvedIdentityResolver, ResolvedIdentity
from src.plugins.amia_core.registry import registry

from .config import SendConfig, load_send_config
from .identity import build_activity_record
from .models import ActivityScope
from .storage import ActivityStore, LegacyDatabaseDetected
from .writer import ActivityWriter


class ActivityService:
    def __init__(self, config: SendConfig | None = None) -> None:
        self.config = config or load_send_config()
        self.store = ActivityStore(self.config.db_path)
        self.writer = ActivityWriter(self.store, self.config)
        self.resolver: IdentityResolver = UnresolvedIdentityResolver()
        self.ready = False

    async def start(self) -> None:
        try:
            await self.store.initialize()
        except LegacyDatabaseDetected:
            logging.getLogger(__name__).error(
                "send v2 did not start because the configured database is legacy; no data was changed"
            )
            return
        await self.writer.start()
        
        # Load resolver and register stats provider
        resolver = registry.get_identity_resolver()
        if resolver:
            self.resolver = resolver
            
        registry.register_stats_provider("send", self, replace=True)
        self.ready = True

    async def stop(self) -> None:
        if self.ready:
            await self.writer.stop()
        self.ready = False

    async def record_event(self, event: MessageEvent) -> None:
        if not self.ready:
            return
        record = await build_activity_record(event, self.config, self.resolver)
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

    async def get_user_activity(self, identity: ResolvedIdentity, start_date: date, end_date: date) -> dict[str, Any]:
        """StatsProvider protocol implementation for user activity."""
        scope = identity.external_key
        if identity.canonical_user_id:
            row = await self.store.fetch_one(
                """
                SELECT COALESCE(SUM(message_count), 0), COALESCE(SUM(total_bytes), 0)
                FROM activity_daily
                WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
                  AND canonical_user_id=? AND date >= ? AND date < ?
                """,
                (self.config.adapter_instance_id, self.config.bot_app_id or "unverified", scope.self_id, identity.canonical_user_id, start_date.isoformat(), end_date.isoformat()),
            )
        else:
            row = await self.store.fetch_one(
                """
                SELECT COALESCE(SUM(message_count), 0), COALESCE(SUM(total_bytes), 0)
                FROM activity_daily
                WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
                  AND gensokyo_user_id=? AND date >= ? AND date < ?
                """,
                (self.config.adapter_instance_id, self.config.bot_app_id or "unverified", scope.self_id, scope.user_id, start_date.isoformat(), end_date.isoformat()),
            )
        return {"message_count": row[0] if row else 0, "total_bytes": row[1] if row else 0}

    async def get_group_rank(
        self, bot_id: str, context_id: str, start_date: date, end_date: date, limit: int = 10
    ) -> list[dict[str, Any]]:
        scope = self._scope(bot_id)
        rows = await self.store.fetch_all(
            """
            SELECT gensokyo_user_id, MAX(display_name), SUM(message_count), SUM(total_bytes)
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND context_id=? AND date >= ? AND date < ?
            GROUP BY gensokyo_user_id
            ORDER BY SUM(message_count) DESC, gensokyo_user_id ASC LIMIT ?
            """,
            (
                scope.adapter_instance_id, scope.bot_app_id, scope.bot_id, context_id,
                start_date.isoformat(), end_date.isoformat(), limit,
            ),
        )
        return [
            {"gensokyo_user_id": r[0], "display_name": r[1] or r[0], "message_count": r[2], "total_bytes": r[3]}
            for r in rows
        ]

    async def get_group_dau(self, bot_id: str, context_id: str, target_date: date) -> dict[str, Any]:
        scope = self._scope(bot_id)
        row = await self.store.fetch_one(
            """
            SELECT COUNT(DISTINCT gensokyo_user_id) FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND context_id=? AND date=?
            """,
            (scope.adapter_instance_id, scope.bot_app_id, scope.bot_id, context_id, target_date.isoformat()),
        )
        return {"available": True, "count": row[0] if row else 0, "definition": "group scoped DAU"}

    async def get_group_activity_summary(
        self, self_id: str, group_id: str, start_date: date, end_date: date
    ) -> dict[str, Any]:
        """StatsProvider protocol implementation for group activity."""
        row = await self.store.fetch_one(
            """
            SELECT COALESCE(SUM(message_count), 0), COALESCE(SUM(total_bytes), 0),
                   COUNT(DISTINCT gensokyo_user_id)
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND context_id=? AND date >= ? AND date < ?
            """,
            (self.config.adapter_instance_id, self.config.bot_app_id or "unverified", self_id, group_id, start_date.isoformat(), end_date.isoformat()),
        )
        return {"message_count": row[0] if row else 0, "total_bytes": row[1] if row else 0, "unique_users": row[2] if row else 0}

    async def get_user_activity_summary(
        self, bot_id: str, context_id: str, user_id: str, start_date: date, end_date: date
    ) -> dict[str, Any]:
        scope = self._scope(bot_id)
        row = await self.store.fetch_one(
            """
            SELECT COALESCE(SUM(message_count), 0), COALESCE(SUM(total_bytes), 0), MAX(display_name)
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND context_id=? AND gensokyo_user_id=?
              AND date >= ? AND date < ?
            """,
            (scope.adapter_instance_id, scope.bot_app_id, scope.bot_id, context_id, user_id, start_date.isoformat(), end_date.isoformat()),
        )
        return {"message_count": row[0], "total_bytes": row[1], "display_name": row[2] or user_id}

    async def get_instance_active_users(self, start_date: date, end_date: date) -> dict[str, Any]:
        if not self.config.cross_context_user_id_stable or not self.config.bot_app_id:
            return {"available": False, "count": None, "reason": "identity stability is not verified"}
        row = await self.store.fetch_one(
            """
            SELECT COUNT(DISTINCT gensokyo_user_id) FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND date >= ? AND date < ?
            """,
            (self.config.adapter_instance_id, self.config.bot_app_id, start_date.isoformat(), end_date.isoformat()),
        )
        return {"available": True, "count": row[0] if row else 0, "definition": "verified instance active users"}

    async def get_merged_dau(self, start_date: date, end_date: date) -> dict[str, Any]:
        if not self.config.bot_app_id or not self.config.cross_context_user_id_stable:
            return {"available": False, "count": None, "reason": "cross-context user identity is not verified"}
        rows = await self.store.fetch_all(
            """
            SELECT canonical_user_id, gensokyo_user_id
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND date >= ? AND date < ?
            """,
            (self.config.adapter_instance_id, self.config.bot_app_id, start_date.isoformat(), end_date.isoformat()),
        )
        keys = {f"c:{canonical}" if canonical else f"g:{user}" for canonical, user in rows}
        bound = {canonical for canonical, _ in rows if canonical}
        unbound = {user for canonical, user in rows if not canonical}
        return {
            "available": True,
            "count": len(keys),
            "bound_count": len(bound),
            "unbound_count": len(unbound),
            "warning": "unbound identities are not merged across unverified contexts",
        }

    async def get_admin_dashboard_data(self, bot_id: str, mode: str) -> dict[str, Any]:
        today = date.today()
        start = today if mode == "day" else (today.replace(day=1) if mode == "month" else today.replace(month=1, day=1))
        scope = self._scope(bot_id)
        row = await self.store.fetch_one(
            """
            SELECT COALESCE(SUM(message_count), 0), COALESCE(SUM(total_bytes), 0),
                   COUNT(DISTINCT context_id)
            FROM activity_daily
            WHERE adapter_instance_id=? AND bot_app_id=? AND bot_id=?
              AND context_type='group' AND date BETWEEN ? AND ?
            """,
            (scope.adapter_instance_id, scope.bot_app_id, scope.bot_id, start.isoformat(), today.isoformat()),
        )
        end_exclusive = today + timedelta(days=1)
        instance = await self.get_instance_active_users(start, end_exclusive)
        return {"total_messages": row[0], "total_bytes": row[1], "active_groups": row[2], "instance_dau": instance}


activity_service = ActivityService()
