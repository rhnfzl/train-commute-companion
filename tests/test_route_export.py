from dataclasses import replace

import pytest
from lifecycle_fixtures import L1, L2, ans, at, conn, ctx, usual_locked
from recovery_sample import selection

from train_commute_companion.calendar_export import CalendarExportError
from train_commute_companion.route_export import export_preview
from train_commute_companion.view import occurrence_view


def journey():
    return occurrence_view(usual_locked(), ctx(at("07:50")), (ans("AAA", conn(L1, L2)),))


def test_preview_whitelists_one_public_planned_route_and_escapes_station_text():
    original = journey()
    changed = replace(
        original,
        profile_name="PRIVATE PROFILE",
        legs=(
            replace(original.legs[0], board='<img src="https://evil.test" onerror="alert(1)">'),
            original.legs[1],
        ),
    )
    text = export_preview(changed, exported_at=at("07:50")).decode()
    assert "PRIVATE PROFILE" not in text
    assert '<img src="https://evil.test"' not in text
    assert "&lt;img" in text
    assert "<script" not in text and "<form" not in text and "href=" not in text
    assert "default-src 'none'" in text and "not live" in text
    assert original.legs[0].train_number in text
    assert "27 Oct 2026 08:12 CET" in text
    assert "latitude" not in text and "source_query" not in text
    assert str(original.key.profile_id) not in text
    forecast_changed = replace(
        changed,
        legs=tuple(
            replace(leg, departure=at("10:00"), arrival=at("11:00")) for leg in changed.legs
        ),
    )
    assert export_preview(forecast_changed, exported_at=at("07:50")) == text.encode()


@pytest.mark.parametrize("change", [{"not_travelling": True}, {"selected_recovery": selection()}])
def test_preview_reuses_selected_journey_qualification(change):
    with pytest.raises(CalendarExportError):
        export_preview(replace(journey(), **change), exported_at=at("07:50"))


def test_preview_refuses_an_ambiguous_save_time():
    with pytest.raises(CalendarExportError, match="timezone"):
        export_preview(journey(), exported_at=at("07:50").replace(tzinfo=None))
