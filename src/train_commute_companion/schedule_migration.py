"""Move an idle Home Assistant profile's saved history into a Docker store."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from train_commute_companion.model import Config, Profile
from train_commute_companion.store import PersistedState
from train_commute_companion.travel_changes import new_regular_conflict


def _selected(config: Config, profile_id: str) -> tuple[Profile, ...]:
    return tuple(profile for profile in config.profiles if profile.id == profile_id)


def merge_profile_history(
    docker: PersistedState, home_assistant: PersistedState, profile_id: str, now: datetime
) -> PersistedState:
    """Preserve Docker plans and HA journey history. This runs with both owners stopped."""
    old = home_assistant.config
    if old is None or not (profiles := _selected(old, profile_id)):
        raise ValueError("The Home Assistant profile has no saved configuration.")
    if docker.config is not None and docker.config.profiles:
        raise ValueError("Docker already has regular commute profiles.")
    if (
        profile_id in docker.travel_plans
        or any(record.key.profile_id == profile_id for record in docker.journey_records.values())
        or any(item.key.profile_id == profile_id for item in docker.occurrences.values())
        or any(key.startswith(f"{profile_id}|") for key in docker.baselines)
    ):
        raise ValueError("Docker already uses this profile ID.")
    if any(
        occurrence.key.profile_id == profile_id
        for occurrence in home_assistant.occurrences.values()
    ):
        raise ValueError("Wait until the Home Assistant journey has finished and been archived.")

    config = Config(old.revision, profiles)
    snapshots = {
        revision: Config(revision, selected)
        for revision, snapshot in home_assistant.config_snapshots.items()
        if (selected := _selected(snapshot, profile_id))
    }
    snapshots[config.revision] = config
    if snapshots.keys() & docker.config_snapshots.keys():
        raise ValueError("Docker already uses one of the saved route revisions.")

    records = {
        key: replace(record, prompt=None)
        for key, record in home_assistant.journey_records.items()
        if record.key.profile_id == profile_id
    }

    def directional[T](values: Mapping[str, T]) -> dict[str, T]:
        return {key: value for key, value in values.items() if key.startswith(f"{profile_id}|")}

    history = {
        key: summary
        for key, summary in home_assistant.commute_history.items()
        if summary.key.profile_id == profile_id
    }
    merged = replace(
        docker,
        config=config,
        config_snapshots={**docker.config_snapshots, **snapshots},
        baselines={**docker.baselines, **directional(home_assistant.baselines)},
        journey_records={**docker.journey_records, **records},
        remembered_stations={
            **docker.remembered_stations,
            **directional(home_assistant.remembered_stations),
        },
        commute_history={**docker.commute_history, **history},
    )
    if conflict := new_regular_conflict(docker, merged, now):
        raise ValueError(conflict)
    return merged
