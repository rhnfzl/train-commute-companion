from dataclasses import replace

from app_rig import ONE_PROFILE, occ, view
from recovery_sample import NOW, answer
from tcc_app.commands import connections_tool, render_status, status_buttons

from train_commute_companion.model import Direction
from train_commute_companion.profile import parse_profiles
from train_commute_companion.recovery import from_connection
from train_commute_companion.view import RecoveryPlanView


def test_telegram_and_mcp_show_replacement_walks_as_unconfirmed_not_followed():
    itinerary = from_connection(answer().connections[0])
    occurrence = replace(occ(), replacement_plans=(RecoveryPlanView(NOW, itinerary),))
    current = view(occurrence)
    text = render_status(current, True)
    assert "starting position not confirmed" in text
    assert "Proposed recovery itinerary (not followed) (unconfirmed: planned times)" in text
    assert "Walk: Gouda to Gouda, NSbushalte" in text
    assert "NS Bus 931374" in text
    result = connections_tool(current, True, "p1", Direction.OUTBOUND)
    steps = result["journeys"][0]["replacement_plans"][0]["itinerary"]["steps"]
    assert steps[1]["mode"] == "WALK" and steps[2]["mode"] == "BUS"
    assert steps[1]["origin"]["physical_id"] != steps[1]["destination"]["physical_id"]
    assert steps[2]["departure_provenance"] == "planned"
    assert status_buttons(current) == status_buttons(view(occ()))


def test_recovery_status_and_button_labels_follow_the_profile_language():
    itinerary = from_connection(answer().connections[0])
    occurrence = replace(occ(), replacement_plans=(RecoveryPlanView(NOW, itinerary),))
    profiles, errors = parse_profiles(ONE_PROFILE, None)
    assert not errors
    english = view(occurrence, profiles=tuple(profiles))
    dutch = replace(
        english,
        profiles=tuple(
            replace(p, alerts=replace(p.alerts, language="nl")) for p in english.profiles
        ),
    )
    text = render_status(dutch, True)
    assert "Vertrekpunt nog niet bevestigd" in text
    assert "Voorgestelde vervangende reis (nog niet gevolgd)" in text
    assert "Gouda, NSbushalte" in text
    en_buttons, nl_buttons = status_buttons(english), status_buttons(dutch)
    assert en_buttons is not None and nl_buttons is not None
    en, nl = en_buttons["inline_keyboard"][0], nl_buttons["inline_keyboard"][0]
    assert [button["callback_data"] for button in en] == [button["callback_data"] for button in nl]
    assert [button["text"] for button in nl] == ["Ik vertrek", "Ik reis niet"]
