from __future__ import annotations

from datetime import date, timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER

from .service import ActivityService


def _range(mode: str, today: date) -> tuple[date, date]:
    end_exclusive = today + timedelta(days=1)
    if mode == "month":
        return today.replace(day=1), end_exclusive
    if mode == "year":
        return today.replace(month=1, day=1), end_exclusive
    return today, end_exclusive


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.2f} KB"
    return f"{value / 1024 / 1024:.2f} MB"


def register_commands(service: ActivityService) -> None:
    for command, aliases, mode, title in (
        ("今日发言", {"今日排行榜"}, "day", "今日龙王榜"),
        ("本月发言", {"本月排行榜"}, "month", "本月龙王榜"),
        ("今年发言", {"今年排行榜"}, "year", "年度龙王榜"),
    ):
        matcher = on_command(
            command,
            aliases=aliases,
            priority=5,
            block=True,
        )

        @matcher.handle()
        async def _rank(
            event: GroupMessageEvent,
            matcher: Matcher,
            _mode: str = mode,
            _title: str = title,
        ) -> None:
            if not service.ready:
                await matcher.finish(
                    "统计库尚未启用：检测到旧库时请先执行迁移预检。"
                )

            start, end = _range(_mode, service._today())
            rows = await service.get_group_rank(
                str(event.self_id),
                str(event.group_id),
                start,
                end,
            )
            if not rows:
                await matcher.finish(f"📊 {_title}\n暂无数据，快来水群！")

            lines = [f"📊 {_title} (Top 10)"]
            for index, row in enumerate(rows, start=1):
                lines.append(
                    f"{index}. {row['display_name']} "
                    f"({row['message_count']})"
                )
            await matcher.finish("\n".join(lines))

    for command, aliases, mode, title in (
        ("今日DAU", {"全群统计", "bot数据"}, "day", "今日"),
        ("本月DAU", {"本月统计"}, "month", "本月"),
        ("今年DAU", {"年度统计"}, "year", "年度"),
    ):
        matcher = on_command(
            command,
            aliases=aliases,
            permission=SUPERUSER,
            priority=1,
            block=True,
        )

        @matcher.handle()
        async def _dashboard(
            bot: Bot,
            matcher: Matcher,
            _mode: str = mode,
            _title: str = title,
        ) -> None:
            if not service.ready:
                await matcher.finish(
                    "统计库尚未启用：检测到旧库时请先执行迁移预检。"
                )

            data = await service.get_admin_dashboard_data(
                str(bot.self_id),
                _mode,
            )
            dau = data["instance_dau"]
            dau_text = (
                str(dau["count"])
                if dau["available"]
                else "未验证（未启用）"
            )
            await matcher.finish(
                "\n".join(
                    (
                        f"📊 {_title} 活跃概览",
                        f"👥 活跃群聊: {data['active_groups']}",
                        f"👤 实例活跃用户: {dau_text}",
                        f"💬 群消息总数: {data['total_messages']}",
                        f"📡 流量记录: "
                        f"{_format_bytes(data['total_bytes'])}（估算）",
                    )
                )
            )
