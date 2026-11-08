from dataclasses import replace

from app_rig import leg_view, occ, view
from builders import make_connection, make_leg
from tcc_app.commands import connections_tool, render_status

from train_commute_companion.model import Direction


def test_status_and_alternatives_preserve_provider_accessibility_notes():
    leg = make_leg("AAA", "RTD", "08:25", "09:12")
    leg = replace(
        leg,
        stops=(
            replace(leg.board, accessibility_notes=("Book boarding assistance",)),
            replace(leg.alight, accessibility_notes=("Alighting assistance",), exit_side="right"),
        ),
    )
    followed = replace(
        leg_view(),
        boarding_notes=("Book boarding assistance",),
        alighting_notes=("Alighting assistance",),
        exit_side="right",
    )
    v = view(occ(legs=(followed,), next_good=make_connection([leg])))
    text = render_status(v, confirmed=True)
    assert "boarding notes per NS: Book boarding assistance" in text
    assert "alighting notes per NS: Alighting assistance" in text
    assert "exit side: right per NS" in text
    result = connections_tool(v, True, "p1", Direction.OUTBOUND)
    fields = result["journeys"][0]["next_good"]["legs"][0]
    assert fields["boarding_notes"] == ["Book boarding assistance"]
    assert fields["alighting_notes"] == ["Alighting assistance"]
    assert fields["exit_side"] == "right"


def test_missing_accessibility_data_does_not_claim_accessible():
    result = connections_tool(
        view(occ(next_good=make_connection([make_leg("AAA", "RTD", "08:25", "09:12")]))),
        True,
        "p1",
        Direction.OUTBOUND,
    )
    fields = result["journeys"][0]["next_good"]["legs"][0]
    assert fields["boarding_notes"] == [] and fields["alighting_notes"] == []
    assert fields["exit_side"] is None
