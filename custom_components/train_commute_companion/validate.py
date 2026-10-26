"""Checks behind the config and options flows: the profiles path, and the profiles file itself."""

import os
from functools import partial

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from train_commute_companion.profile import check_lines

PERSON_DOMAINS = ("person", "device_tracker")


def _check_path(hass: HomeAssistant, path: str) -> str | None:
    if not hass.config.is_allowed_path(path):
        return "path_not_allowed"
    if not os.path.isfile(path):
        return "file_missing"
    try:
        with open(path, "rb"):
            pass
    except OSError:
        return "file_unreadable"
    return None


async def check_path(hass: HomeAssistant, path: str) -> str | None:
    """One executor job: resolving and stat-ing the path must not block `hass.loop`."""
    return await hass.async_add_executor_job(partial(_check_path, hass, path))


def _read(path: str) -> str:
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def _entity_line(
    hass: HomeAssistant, where: str, entity: str, domains: tuple[str, ...]
) -> str | None:
    if entity.partition(".")[0] not in domains:
        what = " or ".join(domains)
        return f"{where}: {entity} must be a {what}"
    if hass.states.get(entity) is None:
        return f"{where}: {entity} not found in Home Assistant"
    return None


async def validate_file(hass: HomeAssistant, path: str, known: frozenset[str] | None) -> list[str]:
    try:
        text = await hass.async_add_executor_job(_read, path)
    except (OSError, UnicodeDecodeError) as err:
        return [f"{path}: cannot read ({err})"]
    cfg, lines = check_lines(text, known, dt_util.now().date())
    for p in cfg.profiles if cfg else ():
        loc = p.location
        if loc is None or loc.source != "ha":
            continue
        checks = [(f"{p.name}.location.entity", loc.entity or "", PERSON_DOMAINS)]
        zones = dict(loc.zones)
        zones.setdefault("home", "zone.home")  # an unmapped home is HA's own home zone
        checks += [(f"{p.name}.location.zones.{r}", z, ("zone",)) for r, z in zones.items()]
        lines += [x for w, e, d in checks if (x := _entity_line(hass, w, e, d))]
    if known is None:
        lines.append("station codes not checked: the NS station list is unavailable")
    return lines
