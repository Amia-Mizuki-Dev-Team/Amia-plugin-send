from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal


ContextType = Literal["group", "private"]


@dataclass(frozen=True, slots=True)
class ActivityScope:
    adapter_type: str
    adapter_instance_id: str
    bot_id: str
    bot_app_id: str
    scope_verified: bool


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    activity_date: date
    activity_hour: int
    scope: ActivityScope
    context_type: ContextType
    context_id: str
    gensokyo_user_id: str
    canonical_user_id: str | None
    display_name: str | None
    message_bytes: int
    occurred_at: datetime
    message_id: str | None = None
    dedupe_key: str | None = None
