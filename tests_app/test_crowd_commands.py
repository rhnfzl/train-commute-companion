from dataclasses import replace

from app_rig import leg_view, occ, view
from builders import make_connection, make_leg
from tcc_app.commands import connections_tool, render_status

from train_commute_companion.model import Direction


def test_telegram_and_mcp_show_forecasts_for_followed_and_alternative_trains():
    leg = make_leg("AAA", "RTD", "08:25", "09:12")
    alternative = make_connection([replace(leg, crowd_forecast="low")])
    v = view(occ(legs=(replace(leg_view(), crowd_forecast="high"),), next_good=alternative))
    assert "crowd forecast: high per NS" in render_status(v, confirmed=True)
    assert "next good connection: 08:25 from AAA, crowd forecast: low per NS" in render_status(
        v, confirmed=True
    )
    result = connections_tool(v, True, "p1", Direction.OUTBOUND)
    assert result["journeys"][0]["next_good"]["legs"][0]["crowd_forecast"] == "low"


def test_unknown_alternative_forecast_is_not_reported_as_low():
    alternative = make_connection([make_leg("AAA", "RTD", "08:25", "09:12")])
    result = connections_tool(view(occ(next_good=alternative)), True, "p1", Direction.OUTBOUND)
    assert result["journeys"][0]["next_good"]["legs"][0]["crowd_forecast"] is None
    assert "crowd forecast: low" not in result["text"]
