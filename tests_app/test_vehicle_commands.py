from tcc_app.commands import connections_tool, render_status
from vehicle_rig import make_rig

from train_commute_companion.model import Direction


async def test_telegram_today_and_mcp_preserve_context_uncertainty(tmp_path):
    rig = make_rig(tmp_path)
    await rig.rt.start()
    await rig.rt.vehicle_context_idle()
    current = rig.rt.view()
    text = render_status(current, True)
    assert "VIRM" in text and "Measurement time is unknown." in text
    fields = connections_tool(current, True, "p1", Direction.OUTBOUND)["journeys"][0][
        "train_context"
    ]
    assert fields["context"]["units"][0]["capacity"]["first_class"] == 10
    assert fields["position"]["measurement_at"] is None
    assert not fields["operating_date_confirmed"]
