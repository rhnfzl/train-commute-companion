from dataclasses import replace

from app_rig import occ, view
from recovery_sample import NOW, answer
from tcc_app.commands import connections_tool, render_status, status_buttons

from train_commute_companion.model import Direction
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
