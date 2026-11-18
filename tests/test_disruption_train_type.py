"""NS disruption notices explain cause and end, and stay quiet when only the other train type
is hit (Q11). Payload shape is the live 2026-11-17 brandmelding item, moved to the fixture day."""

import json
from dataclasses import replace

from builders import make_connection, make_leg
from lifecycle_fixtures import at, ctx, journey, occ

from train_commute_companion.disruptions import update_disruptions
from train_commute_companion.model import EventKind, LegState
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.providers.ns import map_active_disruptions
from train_commute_companion.serde import loads
from train_commute_companion.state import DisruptionNotice

LABEL = "Door een brandmelding: tussen Boxtel en Eindhoven Centraal rijden er geen Sprinters."
UIC = {"HT": "8400319", "BTL": "8400131", "EHV": "8400206", "TB": "8400597", "UT": "8400621"}


def section(codes, consequence):
    stations = [{"stationCode": c, "uicCode": UIC[c]} for c in codes]
    return {
        "section": {"stations": stations},
        "consequence": {
            "section": {"stations": stations},
            "description": consequence,
            "level": "LESS_TRAINS",
        },
    }


def incident(*consequences, codes=(("HT", "BTL", "EHV"), ("TB", "BTL", "EHV")), label=LABEL):
    raw = {
        "id": "6069241",
        "type": "DISRUPTION",
        "isActive": True,
        "title": "'s-Hertogenbosch - Eindhoven; Tilburg - Eindhoven",
        "url": "https://www.ns.nl/reisinformatie/actuele-situatie-op-het-spoor/storing?id=6069241",
        "cause": {"label": "brandmelding"},
        "end": "2026-10-27T16:45:00+0100",
        "expectedDuration": {"description": "Dit duurt tot ongeveer 16:45 uur."},
        "publicationSections": [
            section(c, consequence) for c, consequence in zip(codes, consequences, strict=True)
        ],
    }
    if label:
        raw["timespans"] = [{"situation": {"label": label}}]
    feed = map_active_disruptions({"payload": [raw]})
    assert feed.coverage == "complete"
    return feed


def riding(category, board="HT", alight="EHV", stops=("BTL",)):
    leg = replace(
        make_leg(board, alight, "08:12", "08:40", number="3500", stops=stops), category=category
    )
    answer = ProviderAnswer(
        Query(board, alight, leg.train.planned_departure), (make_connection([leg]),), at("07:50")
    )
    return occ(journey([leg])), (answer,)


def told(state, feed, answers=()):
    return [
        e
        for e in update_disruptions(state, ctx(at("07:50")), feed, answers).events
        if e.kind is EventKind.DISRUPTION
    ]


def test_sprinter_only_brandmelding_is_quiet_for_an_intercity_and_explained_for_a_sprinter():
    feed = incident("geen Sprinters", "geen Sprinters")
    state, answers = riding("IC")
    assert told(state, feed, answers) == []
    state, answers = riding("SPR")
    (event,) = told(state, feed, answers)
    assert f"NS: {LABEL} Until about 16:45. Check your journey." in event.text
    assert event.text.endswith("storing?id=6069241")


def test_intercity_only_notice_is_quiet_for_a_sprinter():
    state, answers = riding("SPR")
    assert told(state, incident("minder Intercity’s", "Geen intercitys"), answers) == []


def test_mixed_types_unknown_wording_or_unknown_train_still_tell():
    state, answers = riding("ICD")
    assert told(state, incident("geen Sprinters", "geen Intercity's"), answers)
    assert told(state, incident("geen Sprinters", "minder Sprinters en Intercity's"), answers)
    assert told(state, incident("geen Sprinters", "Er rijden minder treinen"), answers)
    assert (
        told(state, incident("geen Sprinters", "geen Sprinters")) != []
    )  # no answer: type unknown
    odd, odd_answers = riding("EST")
    assert told(odd, incident("geen Sprinters", "geen Sprinters"), odd_answers)


def test_without_a_lock_the_notice_tells_with_the_better_text():
    feed = incident("geen Sprinters", codes=(("UT", "EHV"),))
    (event,) = told(occ(None), feed)
    assert f"NS: {LABEL} Until about 16:45." in event.text
    (plain,) = told(occ(None), incident("geen Sprinters", codes=(("UT", "EHV"),), label=None))
    assert (
        "NS reports 's-Hertogenbosch - Eindhoven; Tilburg - Eindhoven involving your route's "
        "stations, until about 16:45; check your journey." in plain.text
    )


def test_revision_that_starts_to_hit_your_type_tells():
    state, answers = riding("IC")
    quiet = update_disruptions(
        state, ctx(at("07:50")), incident("geen Sprinters", "geen Sprinters"), answers
    )
    assert not quiet.events
    widened = incident("geen Sprinters", "minder Intercity's")
    assert told(quiet.state, widened, answers)


def test_notice_stored_before_cause_and_sections_existed_still_loads():
    old = {
        "item": {"id": "6069241", "title": "Old fault", "stations": ["HT"], "url": None},
        "active": True,
        "revision": 1,
    }
    notice = loads(DisruptionNotice, json.dumps(old))
    assert notice.item.summary == "" and notice.item.end is None and notice.item.sections == ()


def test_told_notice_is_not_told_again_after_upgrade_or_an_end_time_move():  # Q11 review
    state, answers = riding("SPR")
    feed = incident("geen Sprinters", "geen Sprinters")
    (n,) = update_disruptions(state, ctx(at("07:50")), feed, answers).state.disruptions.values()

    def told_as(item):
        return replace(
            state, disruptions={item.id: replace(n, item=item, notified=True, announced=True)}
        )

    # stored by 0.2.2: no summary, end or sections yet
    assert told(told_as(replace(n.item, summary="", end=None, sections=())), feed, answers) == []
    # NS moves the expected end only
    later = replace(feed, items=(replace(feed.items[0], end=at("17:15")),))
    assert told(told_as(n.item), later, answers) == []
    updated = update_disruptions(
        told_as(n.item), ctx(at("07:50")), later, answers
    ).state.disruptions[n.item.id]
    assert updated.item.end == at("17:15") and updated.revision == n.revision
    # a new cause or effect is news
    changed = replace(
        feed, items=(replace(feed.items[0], summary="Door een storing: geen treinen."),)
    )
    assert told(told_as(n.item), changed, answers) != []


def test_missed_train_without_replacement_has_no_disruption_route():
    state, answers = riding("SPR")
    assert state.lock is not None
    missed = replace(
        state.lock, legs=tuple(replace(leg, state=LegState.MISSED) for leg in state.lock.legs)
    )
    assert (
        told(replace(state, lock=missed), incident("geen Sprinters", "geen Sprinters"), answers)
        == []
    )


# --- 2026-11-18: a section is on your leg only along a stretch you travel ---


def test_a_section_sharing_only_your_end_station_is_another_line_in_both_directions():
    other_approach = incident("Er rijden minder treinen", codes=(("TB", "EHV"),))
    stretch = incident("Er rijden minder treinen", codes=(("BTL", "EHV"),))
    for board, alight in (("HT", "EHV"), ("EHV", "HT")):
        state, answers = riding("IC", board, alight)
        assert told(state, other_approach, answers) == []
        assert told(state, stretch, answers)


def test_one_station_on_your_leg_tells_and_unknown_stops_keep_station_overlap():
    state, answers = riding("IC")
    assert told(state, incident("Er rijden minder treinen", codes=(("BTL",),)), answers)
    assert told(state, incident("Er rijden minder treinen", codes=(("EHV",),)), answers)
    # no answer shows the leg's stops: any shared station still tells
    assert told(state, incident("Er rijden minder treinen", codes=(("TB", "EHV"),)))


def test_a_leg_listed_with_only_its_end_stations_keeps_station_overlap():
    state, answers = riding("IC", stops=())  # NS left the stops out: the ends are not a stretch
    assert told(state, incident("Er rijden minder treinen", codes=(("BTL", "EHV"),)), answers)


def test_a_section_on_any_copy_of_your_train_tells_in_either_answer_order():
    state, via_btl = riding("IC")
    _, via_tb = riding("IC", stops=("TB",))  # a newer answer shows the train diverted
    feed = incident("Er rijden minder treinen", codes=(("TB", "EHV"),))
    assert told(state, feed, (*via_btl, *via_tb))
    assert told(state, feed, (*via_tb, *via_btl))
    # quiet only when every copy shows it off your leg
    assert told(state, feed, (*via_btl, *via_btl)) == []


def test_fewer_stopping_trains_is_quiet_for_an_intercity_only():
    feed = incident("minder stoptreinen", "geen stoptreinen.")
    state, answers = riding("IC")
    assert told(state, feed, answers) == []
    state, answers = riding("SPR")
    assert told(state, feed, answers)


def test_a_notice_stored_before_sections_counted_stations_is_no_news():
    state, answers = riding("SPR")
    feed = incident("geen Sprinters", "geen Sprinters")
    first = update_disruptions(state, ctx(at("07:50")), feed, answers).state
    old = {
        k: replace(
            n, item=replace(n.item, sections=tuple(replace(x, size=0) for x in n.item.sections))
        )
        for k, n in first.disruptions.items()
    }
    again = update_disruptions(replace(first, disruptions=old), ctx(at("07:52")), feed, answers)
    assert [n.revision for n in again.state.disruptions.values()] == [1]
