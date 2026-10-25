"""Message identity conversion for Send.

Identity quality is process-local: the counters reset when Send restarts and
the database keeps no quality column, so the only persisted distinction is
``activity_daily.canonical_user_id IS NULL`` (raw/unresolved) versus a non-null
canonical id (merged/trusted).  A restart therefore forgets *why* a row is raw.

Historical boundary: when a later event reaches an existing row for the same
date/scope, ``activity_daily`` forwards that event's canonical id onto the row
(``COALESCE`` in ``storage.py``).  No past resolution is re-run, the event table
is never rewritten, earlier dates are untouched and aggregates are not
recomputed.
"""

from __future__ import annotations

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


# Coarse identity quality states.  Callers only need to tell "no resolver was
# registered", "the resolver failed" and "the user is genuinely unbound" apart,
# and none of them may carry identity payloads.
IDENTITY_STATUS_RESOLVED = "resolved"
IDENTITY_STATUS_UNBOUND = "unbound"
IDENTITY_STATUS_NO_RESOLVER = "no_resolver"
IDENTITY_STATUS_DISCOVERY_ERROR = "discovery_error"
IDENTITY_STATUS_TIMEOUT = "timeout"
IDENTITY_STATUS_FAILED = "failed"

# States that mean the conversion path was unavailable or broke.  "unbound" is
# a normal answer, not a problem.
IDENTITY_PROBLEM_STATUSES = frozenset(
    {
        IDENTITY_STATUS_NO_RESOLVER,
        IDENTITY_STATUS_DISCOVERY_ERROR,
        IDENTITY_STATUS_TIMEOUT,
        IDENTITY_STATUS_FAILED,
    }
)

_MISSING = object()


class ResolutionOutcome:
    """Per-event carrier for how identity resolution ended.

    ``build_activity_record`` writes the status here instead of widening its
    return type, so existing callers keep their signature.
    """

    __slots__ = ("status",)

    def __init__(self) -> None:
        self.status = IDENTITY_STATUS_NO_RESOLVER


async def resolve_event_identity(
    identity_key: Any,
    config: SendConfig,
    resolver: Any | None,
) -> tuple[Any | None, str]:
    """Resolve one event against the resolver handed in for *this* event.

    Callers discover the resolver per event; this function never caches either
    a resolver or a failure.  Timeouts go through the public Core safe-call
    policy (``call_provider_safe``) so one slow service cannot stall message
    collection, and a degraded result keeps the raw/unresolved write path.
    """

    if resolver is None:
        return None, IDENTITY_STATUS_NO_RESOLVER

    # Registry registration performs no protocol check, so an object without
    # ``resolve_identity`` can reach here.  Resolve the attribute before it is
    # used as an argument, otherwise AttributeError would escape the per-event
    # path (it used to be caught inside the old try block).
    method = getattr(resolver, "resolve_identity", None)
    if not callable(method):
        return None, IDENTITY_STATUS_FAILED

    try:
        core = get_core()
    except Exception:  # noqa: BLE001 - a missing contract degrades one event
        return None, IDENTITY_STATUS_DISCOVERY_ERROR

    timeout = config.resolver_timeout_seconds
    result = await core.call_provider_safe(
        method,
        identity_key,
        timeout=timeout,
    )
    if not result.success:
        # call_provider_safe reports an inner ``raise TimeoutError`` and a real
        # wait_for expiry with the same ``timed_out`` flag; only a call that
        # actually consumed the budget counts as a timeout here.
        if result.timed_out and result.elapsed_ms >= timeout * 1000:
            return None, IDENTITY_STATUS_TIMEOUT
        return None, IDENTITY_STATUS_FAILED
    return _validated_identity(result.value)


def _validated_identity(resolved: Any) -> tuple[Any | None, str]:
    """Classify one resolver answer as resolved, unbound or invalid."""

    canonical_user_id = getattr(resolved, "canonical_user_id", _MISSING)
    # None is a valid "unbound" answer; anything else must be a canonical id.
    if canonical_user_id is _MISSING or not isinstance(
        canonical_user_id, (str, type(None))
    ):
        return None, IDENTITY_STATUS_FAILED
    if not canonical_user_id:
        return None, IDENTITY_STATUS_UNBOUND
    return resolved, IDENTITY_STATUS_RESOLVED


async def build_activity_record(
    event: MessageEvent,
    config: SendConfig,
    resolver: Any | None = None,
    *,
    outcome: ResolutionOutcome | None = None,
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
    resolved, status = await resolve_event_identity(identity_key, config, resolver)
    if outcome is not None:
        outcome.status = status

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
