"""Transfer minimum and transfer state (7.3, 7.12 rules 9 and 20). Pure, no clock."""

from collections.abc import Mapping
from datetime import datetime

from train_commute_companion.model import (
    ClassifiedLeg,
    LegKind,
    Observation,
    Provenance,
    Stop,
    TimeObs,
    TransferCheck,
    TransferState,
)

FALLBACK_MIN = 5
AT_RISK_SLACK_MIN = 2


def transfer_minimum(
    station: str,
    profile_min: Mapping[str, int],
    provider_time: int | None,
    provider_message_min: int | None,
) -> int:
    """cross_platform is deliberately not a parameter: it never lowers the minimum."""
    provider = max(
        (v for v in (provider_time, provider_message_min) if v is not None), default=None
    )
    user = profile_min.get(station)
    if user is not None:
        return max(user, provider or 0)
    if provider is not None:
        return provider
    return max(profile_min.values(), default=FALLBACK_MIN)


def _expected(obs: TimeObs | None) -> datetime | None:
    return None if obs is None else obs.value or obs.planned


def _solid(obs: TimeObs | None) -> bool:
    return obs is not None and obs.provenance is Provenance.REPORTED and obs.value is not None


def _arrival_at(cl: ClassifiedLeg, station: str) -> Stop:
    return next((s for s in cl.leg.stops[1:] if s.code == station), cl.leg.alight)


def check_transfer(
    incoming: ClassifiedLeg,
    outgoing: ClassifiedLeg,
    station: str,
    profile_min: Mapping[str, int],
) -> TransferCheck:
    inc, out = incoming.leg, outgoing.leg
    arr_obs, dep_obs = _arrival_at(incoming, station).arrival, out.board.departure
    arr, dep = _expected(arr_obs), _expected(dep_obs)
    margin = None if arr is None or dep is None else int((dep - arr).total_seconds() // 60)
    minimum = transfer_minimum(
        station, profile_min, inc.transfer_time_to_next, inc.transfer_message_min
    )

    def broken(reason: str) -> TransferCheck:
        return TransferCheck(TransferState.BROKEN, margin, minimum, reason)

    if outgoing.kind is LegKind.CANCELLED:
        return broken("outgoing leg cancelled")
    if incoming.kind is LegKind.CANCELLED:
        return broken("incoming leg cancelled")
    if incoming.kind is LegKind.CUT_SHORT:
        return broken("incoming leg cut short before the transfer station")
    if inc.change_possible is False:
        return broken("provider: change not possible")
    if inc.reachable is False:
        return broken("provider: not reachable")
    if margin is not None and margin < minimum:
        return broken("margin below minimum")

    def risk(reason: str) -> TransferCheck:
        return TransferCheck(TransferState.AT_RISK, margin, minimum, reason)

    if Observation.SEEN is not incoming.observation or Observation.SEEN is not outgoing.observation:
        return risk("leg not reliably seen")
    if margin is None or not (_solid(arr_obs) and _solid(dep_obs)):
        return risk("planned or missing time")
    if margin < minimum + AT_RISK_SLACK_MIN:
        return risk("margin below minimum plus 2")
    return TransferCheck(TransferState.OK, margin, minimum, "ok")
