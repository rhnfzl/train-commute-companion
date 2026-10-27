"""OwnTracks HTTP mode: a posted location message becomes a `Fix` (8)."""

import json
from collections.abc import Callable

from .follow import Fix, Follower, number, stamp


def parse(body: bytes) -> Fix | None:
    """A `location` message, else None (other types, pings, bad JSON, missing or malformed
    fields)."""
    try:
        msg = json.loads(body)
    except ValueError:  # UnicodeDecodeError included
        return None
    if not isinstance(msg, dict) or msg.get("_type") != "location":
        return None
    if msg.get("t") == "p":  # a ping resends an old position with a new tst: no evidence
        return None
    lat, lon, at = number(msg.get("lat")), number(msg.get("lon")), stamp(msg.get("tst"))
    acc = number(msg.get("acc"))
    if lat is None or lon is None or at is None or ("acc" in msg and acc is None):
        return None  # a malformed acc is not "accuracy unknown": the fix is not trusted
    regions = msg.get("inregions")
    names = [r for r in regions if isinstance(r, str)] if isinstance(regions, list) else []
    return Fix(lat, lon, at, acc, frozenset(names))


def handler(follower: Follower) -> Callable[[bytes], None]:
    def take(body: bytes) -> None:
        if (fix := parse(body)) is not None:
            follower.feed("owntracks", fix)

    return take
