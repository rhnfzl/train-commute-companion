"""A profile can suppress alert categories while reliability feedback remains available."""

from collections.abc import Sequence
from typing import get_args

from train_commute_companion.model import (
    AlertSettings,
    EventKind,
    NotificationCategory,
    NotificationChannel,
    Profile,
    ProfileProblem,
)

CATEGORIES: tuple[NotificationCategory, ...] = get_args(NotificationCategory)

_GROUPS: dict[NotificationCategory, frozenset[EventKind]] = {
    "journey": frozenset(
        {
            EventKind.RECOVERY_UPDATE,
            EventKind.CANCELLED,
            EventKind.CUT_SHORT,
            EventKind.ROUTE_CHANGED,
            EventKind.MISSED_FIRST_TRAIN,
            EventKind.NO_CONNECTION,
            EventKind.RESTORED,
        }
    ),
    "transfers": frozenset(
        {
            EventKind.TRANSFER_BROKEN,
            EventKind.TRANSFER_AT_RISK,
            EventKind.MISSED_TRANSFER,
        }
    ),
    "advice": frozenset(
        {
            EventKind.LEAVE_NOW,
            EventKind.EARLIER_TRAIN_WITHDRAWN,
            EventKind.NEXT_CONNECTION,
        }
    ),
    "delays": frozenset({EventKind.DELAYED, EventKind.NEARLY_ON_TIME, EventKind.BACK_TO_NORMAL}),
    "platforms": frozenset({EventKind.PLATFORM_CHANGE}),
    "crowding": frozenset({EventKind.CROWD_CHANGE}),
    "planned_works": frozenset(
        {EventKind.WORKS_AHEAD, EventKind.WORKS_DIGEST, EventKind.TIMETABLE_ADOPTED}
    ),
    "disruptions": frozenset({EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED}),
    "routine": frozenset({EventKind.ALL_GOOD, EventKind.COMPENSATION_REMINDER}),
}


def category_for(kind: EventKind) -> NotificationCategory | None:
    """None means reliability or command feedback, which cannot be disabled."""
    return next((category for category, kinds in _GROUPS.items() if kind in kinds), None)


def enabled(settings: AlertSettings, kind: EventKind, *, look_ahead: bool = False) -> bool:
    category = "planned_works" if look_ahead and kind is EventKind.RESTORED else category_for(kind)
    return category is None or category not in settings.disabled_categories


CHANNELS: frozenset[NotificationChannel] = frozenset(get_args(NotificationChannel))


def channels_for(
    settings: AlertSettings, kind: EventKind, *, look_ahead: bool = False
) -> frozenset[NotificationChannel]:
    category = "planned_works" if look_ahead and kind is EventKind.RESTORED else category_for(kind)
    if category is None:
        return CHANNELS
    if category in settings.disabled_categories:
        return frozenset()
    return settings.channels.get(category, CHANNELS)


def channel_enabled(
    settings: AlertSettings,
    kind: EventKind,
    channel: NotificationChannel,
    *,
    look_ahead: bool = False,
) -> bool:
    return channel in channels_for(settings, kind, look_ahead=look_ahead)


def routing_ready(
    settings: AlertSettings,
    kind: EventKind,
    available: frozenset[NotificationChannel],
    *,
    look_ahead: bool = False,
) -> bool:
    category = "planned_works" if look_ahead and kind is EventKind.RESTORED else category_for(kind)
    if category is None or category in settings.disabled_categories:
        return True
    explicit = settings.channels.get(category)
    return explicit is None or explicit <= available


def validate_channels(
    profiles: Sequence[Profile], available: frozenset[NotificationChannel]
) -> list[ProfileProblem]:
    return [
        ProfileProblem(
            profile.id,
            f"profiles[{index}].alerts.channels.{category}",
            f"channel {channel!r} is not configured on this host",
            "configure the transport or select an available channel",
        )
        for index, profile in enumerate(profiles)
        for category, selected in profile.alerts.channels.items()
        for channel in sorted(selected - available)
    ]
