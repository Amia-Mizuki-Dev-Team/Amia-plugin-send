from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .config import SendConfig
from .core_contract import get_core
from .models import ActivityRecord, ActivityScope


def message_bytes(event: MessageEvent) -> int:
    total = 0
    for segment in event.message:
        if segment.type == "text":
            total += len(segment.data.get("text", "").encode("utf-8"))
        elif segment.type == "image":
            total += 500 * 1024
        elif segment.type == "record":
            total += 50 * 1024
        elif segment.type == "video":
            total += 2 * 1024 * 1024
        else:
            total += len(str(segment).encode("utf-8"))
    return total


def _display_name(event: MessageEvent) -> str | None:
    sender = getattr(event, "sender", None)
    if sender is None:
        return None
    return getattr(sender, "card", None) or getattr(sender, "nickname", None)


async def build_activity_record(
    event: MessageEvent,
    config: SendConfig,
    resolver: Any | None = None,
) -> ActivityRecord | None:
    if isinstance(event, GroupMessageEvent):
        context_type = "group"
        context_id = str(event.group_id)
    elif isinstance(event, PrivateMessageEvent):
        context_type = "private"
        # C2C has no independent conversation ID in OneBot V11; preserve the
        # sender as context without claiming it is globally mergeable.
        context_id = str(event.user_id)
    else:
        return None

    app_id = config.bot_app_id
    scope = ActivityScope(
        adapter_type="onebot.v11",
        adapter_instance_id=config.adapter_instance_id,
        bot_id=str(event.self_id),
        bot_app_id=app_id or "unverified",
        scope_verified=bool(app_id),
    )
    core = get_core()
    identity_key = core.UserIdentityKey(
        self_id=str(event.self_id),
        user_id=str(event.user_id),
    )
    active_resolver = resolver or core.UnresolvedIdentityResolver()
    try:
        resolved = await asyncio.wait_for(
            active_resolver.resolve_identity(identity_key),
            timeout=config.resolver_timeout_seconds,
        )
    except (TimeoutError, Exception):
        resolved = None

    now = datetime.now()
    return ActivityRecord(
        activity_date=now.date(),
        activity_hour=now.hour,
        scope=scope,
        context_type=context_type,
        context_id=context_id,
        gensokyo_user_id=str(event.user_id),
        canonical_user_id=resolved.canonical_user_id if resolved else None,
        display_name=_display_name(event),
        message_bytes=message_bytes(event),
        occurred_at=now,
    )
