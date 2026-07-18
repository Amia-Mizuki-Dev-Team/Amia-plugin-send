"""Scoped message activity statistics for the local Amia deployment."""

from __future__ import annotations

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.plugin import PluginMetadata

from .commands import register_commands
from .service import activity_service

__plugin_meta__ = PluginMetadata(
    name="群聊活动统计",
    description="按 Gensokyo 实例与 AppID 作用域记录群聊活动和 DAU。",
    usage=(
        "今日发言 / 本月发言 / 今年发言：本群排行榜\n"
        "今日DAU / 本月DAU / 今年DAU：管理员活动概览"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

driver = get_driver()


@driver.on_startup
async def _start_activity_service() -> None:
    await activity_service.start()


@driver.on_shutdown
async def _stop_activity_service() -> None:
    await activity_service.stop()


recorder = on_message(priority=0, block=False)


@recorder.handle()
async def _record_message(event: MessageEvent) -> None:
    await activity_service.record_event(event)


register_commands(activity_service)


async def record_message(event: MessageEvent) -> None:
    """Record one message through the public Send contract."""

    await activity_service.record_event(event)


async def get_user_stats(identity, start_date, end_date):
    """Return user activity without exposing Send's storage implementation."""

    return await activity_service.get_user_activity(identity, start_date, end_date)


async def get_group_stats(self_id, group_id, start_date, end_date):
    return await activity_service.get_group_activity_summary(
        str(self_id), str(group_id), start_date, end_date
    )


async def get_dau(self_id, group_id, target_date):
    return await activity_service.get_group_dau(
        str(self_id), str(group_id), target_date
    )


async def get_user_ranking(self_id, group_id, start_date, end_date, limit=10):
    return await activity_service.get_group_rank(
        str(self_id), str(group_id), start_date, end_date, limit
    )


async def get_group_ranking(self_id, start_date, end_date):
    """Return group totals for one bot scope, sorted deterministically."""

    return await activity_service.get_group_ranking(
        str(self_id),
        start_date,
        end_date,
    )


async def health_check():
    return await activity_service.health_check()
