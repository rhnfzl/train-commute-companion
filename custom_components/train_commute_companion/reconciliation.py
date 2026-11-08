"""Registry removals require a started view with known profile ownership."""

from enum import StrEnum

from train_commute_companion.view import RuntimeView


class ViewPhase(StrEnum):
    STARTING = "starting"
    NO_PROFILES = "no_profiles"
    READY = "ready"


def view_phase(view: RuntimeView) -> ViewPhase:
    if not view.started:
        return ViewPhase.STARTING
    return ViewPhase.READY if view.directions else ViewPhase.NO_PROFILES
