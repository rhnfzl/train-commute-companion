"""A deliberately downloaded route preview contains public timetable fields only."""

from datetime import datetime
from html import escape

from train_commute_companion.calendar import TZ
from train_commute_companion.calendar_export import CalendarExportError, planned_bounds
from train_commute_companion.view import OccurrenceView


def export_preview(occurrence: OccurrenceView, *, exported_at: datetime) -> bytes:
    """Share one saved plan, without profile identity, location or live status."""
    planned_bounds(occurrence)
    if exported_at.tzinfo is None or exported_at.utcoffset() is None:
        raise CalendarExportError("export time has no timezone")
    rows = "".join(
        "<tr>"
        + "".join(
            "<td>" + escape(value) + "</td>"
            for value in (
                leg.planned_departure.astimezone(TZ).strftime("%d %b %Y %H:%M %Z"),
                leg.board,
                leg.alight,
                leg.train_number,
                leg.planned_arrival.astimezone(TZ).strftime("%d %b %Y %H:%M %Z")
                if leg.planned_arrival is not None
                else "unknown",
            )
        )
        + "</tr>"
        for leg in occurrence.legs
    )
    saved = escape(exported_at.astimezone(TZ).strftime("%d %b %Y %H:%M %Z"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer"><title>Saved route preview</title>
<style>body{{font:1rem/1.6 system-ui,sans-serif;max-width:65rem;margin:auto;padding:1rem}}
.route{{overflow-x:auto}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:.6rem;border:1px solid #777}}
caption{{text-align:left;font-weight:bold}}</style></head>
<body><main><h1>Saved route preview</h1><p>Data: NS. Saved {saved}.</p>
<p>This is a timetable snapshot. It is not live and does not update. It does not confirm boarding or arrival.</p>
<div class="route"><table><caption>Planned train journey</caption><thead><tr>
<th scope="col">Departure</th><th scope="col">From</th><th scope="col">To</th>
<th scope="col">Train</th><th scope="col">Arrival</th></tr></thead><tbody>{rows}</tbody></table></div>
<p>Check NS for current times, platforms and disruption before travelling.</p>
</main></body></html>""".encode()
