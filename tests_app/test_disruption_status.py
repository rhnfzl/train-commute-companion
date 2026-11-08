from app_rig import occ, view
from runtime_rig import at
from tcc_app.commands import render_status, status_tool, warning_lines

from train_commute_companion.providers.base import ActiveDisruption


def test_status_exposes_context_without_claiming_the_train_is_cancelled():
    incident = ActiveDisruption("42", "A signal fault.", frozenset({"UT"}), None)
    current = view(
        occ(
            disruptions=(incident,),
            disruptions_checked_at=at("07:50"),
            disruptions_coverage="complete",
        )
    )
    text = render_status(current, True)
    assert "A signal fault" in text and "Possible route impact" in text
    assert "your train is cancelled" not in text


def test_unknown_feed_warning_reaches_mcp_and_does_not_erase_last_context():
    incident = ActiveDisruption("42", "A signal fault.", frozenset({"UT"}), None)
    current = view(occ(disruptions=(incident,), disruptions_checked_at=at("07:50")))
    warnings = warning_lines(current, True)
    assert any("active-disruption data: unavailable" in w for w in warnings)
    result = status_tool(current, True, "p1")
    assert "A signal fault" in result["text"]
    assert all(w in result["text"] for w in warnings)
