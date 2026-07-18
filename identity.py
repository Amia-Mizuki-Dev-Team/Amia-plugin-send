from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _event_time(event: MessageEvent, timezone_name: str) -> datetime:
    try:
        timezone_info = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone_info = ZoneInfo("Asia/Shanghai")

    raw_time = getattr(event, "time", None)
    if isinstance(raw_time, (int, float)) and raw_time > 0:
        return datetime.fromtimestamp(raw_time, tz=timezone.utc).astimezone(
            timezone_info
        )
    return datetime.now(timezone_info)


def _dedupe_key(
    event: MessageEvent,
    *,
    config: SendConfig,
    scope: ActivityScope,
    context_type: str,
    context_id: str,
) -> tuple[str | None, str]:
    message_id = getattr(event, "message_id", None)
    message_id_text = (
        str(message_id) if message_id not in (None, "", 0) else None
    )
    if message_id_text:
        seed = "|".join(
            (
                "message-id",
                scope.adapter_type,
                scope.adapter_instance_id,
                scope.bot_id,
                scope.bot_app_id,
                context_type,
                context_id,
                message_id_text,
            )
        )
    else:
        # OneBot deployments are allowed to omit message_id.  A stable hash of
        # the event's timestamp and serialized message prevents the same event
        # object from being counted twice without persisting its full body.
        event_time = str(getattr(event, "time", ""))
        raw_message = str(getattr(event, "raw_message", ""))
        if not raw_message:
            raw_message = str(getattr(event, "message", ""))
        seed = "|".join(
            (
                "event-fallback",
                scope.adapter_type,
                config.adapter_instance_id,
                scope.bot_id,
                scope.bot_app_id,
                context_type,
                context_id,
                str(event.user_id),
                event_time,
                raw_message,
            )
        )
    return message_id_text, hashlib.sha256(seed.encode("utf-8")).hexdigest()


async def build_activity_record(
    event: MessageEvent,
    config: SendConfig,
    resolver: Any | None = None,
) -> ActivityRecord | None:
    if (
        not config.record_bot_messages
        and str(event.user_id) == str(event.self_id)
    ):
        return None

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
    except Exception:  # noqa: BLE001 - resolver failure is isolated per event
        resolved = None

    now = _event_time(event, config.timezone_name)
    message_id, dedupe_key = _dedupe_key(
        event,
        config=config,
        scope=scope,
        context_type=context_type,
        context_id=context_id,
    )
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
        message_id=message_id,
        dedupe_key=dedupe_key,
    )
