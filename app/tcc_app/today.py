"""A private, read-only page showing the same journey information as Telegram status."""

from html import escape

from train_commute_companion.calendar import TZ
from train_commute_companion.calendar_export import CalendarExportError, export_id, planned_bounds
from train_commute_companion.model import display_name
from train_commute_companion.view import RuntimeView

from .commands import render_status

_STYLE = """
:root { color-scheme: light dark; font-family: system-ui, sans-serif; line-height: 1.6; }
body { margin: 0; background: #eef2f7; color: #172338; }
main { max-width: 58rem; margin: auto; padding: 1.5rem; }
header { margin-bottom: 1.5rem; }
h1 { margin: 0; font-size: clamp(2rem, 6vw, 3rem); }
h2 { margin-top: 0; font-size: 1.25rem; }
a { color: #134fa3; text-underline-offset: .2em; }
a:focus-visible { outline: 3px solid #c57900; outline-offset: 4px; }
section { background: white; border: 1px solid #ccd5e2; border-radius: .75rem;
          padding: 1.25rem; margin-bottom: 1rem; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; margin: 0; }
li { margin-bottom: .5rem; overflow-wrap: anywhere; }
footer { font-size: .9rem; }
@media (max-width: 35rem) { main { padding: 1rem; } section { padding: 1rem; } }
@media (prefers-color-scheme: dark) {
  body { background: #111a29; color: #e7edf6; }
  section { background: #1b293e; border-color: #43546c; }
  a { color: #a1c7ff; }
}
"""


def render(view: RuntimeView, confirmed: bool) -> str:
    """Escape every value from the read model. Download links use opaque occurrence hashes and contain no credentials."""
    freshness = []
    downloads = []
    for occurrence in view.occurrences:
        if occurrence.resolved:
            continue
        health = occurrence.health
        checked = (
            health.last_ok_at.astimezone(TZ).strftime("%a %d %b %Y, %H:%M %Z")
            if health.last_ok_at
            else "unknown"
        )
        text = (
            f"{display_name(occurrence.profile_name)}, {occurrence.key.direction.value}: "
            f"NS data {'fresh' if health.fresh else 'not fresh'}; "
            f"last successful NS check: {checked}."
        )
        freshness.append("<li>" + escape(text) + "</li>")
    for occurrence in view.occurrences:
        try:
            planned_bounds(occurrence)
        except CalendarExportError:
            continue
        target = f"/calendar/{export_id(occurrence.key)}.ics"
        label = escape(
            f"{display_name(occurrence.profile_name)}, {occurrence.key.direction.value}, {occurrence.key.service_date:%a %d %b %Y}"
        )
        downloads.append(
            f'<li>{label}: <a href="{target}">Download this journey to calendar</a>. '
            f'<a href="/preview/{export_id(occurrence.key)}.html">Download a route preview to share</a></li>'
        )
    calendar = (
        '<section aria-labelledby="calendar-heading"><h2 id="calendar-heading">Save one journey</h2>'
        "<p>Download one tentative saved plan. It is not live and does not update.</p>"
        "<p>The route preview includes stations, trains and planned dates. Share the downloaded file deliberately; it opens without a login.</p>"
        "<ul>" + "".join(downloads) + "</ul></section>"
        if downloads
        else ""
    )
    data = (
        "<ul>" + "".join(freshness) + "</ul>"
        if freshness
        else "<p>No current journey has a provider freshness report.</p>"
    )
    snapshot = escape(view.at.astimezone(TZ).strftime("%a %d %b %Y, %H:%M %Z"))
    snapshot_iso = escape(view.at.isoformat(timespec="seconds"))
    status = escape(render_status(view, confirmed))
    service = "Ready" if view.started else "Starting"
    delivery = "Confirmed" if confirmed else "Not confirmed"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Today | Train Commute Companion</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<header>
<h1>Today</h1>
<p>Your current journeys and service health. This page is read-only.</p>
<p>Snapshot: <time datetime="{snapshot_iso}">{snapshot}</time>. <a href="/today">Refresh status</a></p>
</header>
<section aria-labelledby="journeys-heading">
<h2 id="journeys-heading">Journeys and service health</h2>
<p>Companion: {service}. Telegram delivery: {delivery}.</p>
<pre>{status}</pre>
</section>
<section aria-labelledby="freshness-heading">
<h2 id="freshness-heading">Data freshness</h2>
{data}
</section>
{calendar}
<footer>
<p>Unselected replacement plans are proposals, not followed journeys. Followed recovery progress uses your explicit confirmations. A timetable does not confirm boarding or arrival.</p>
<p>Use your private Telegram chat for journey actions. Data: NS.</p>
<p>Use <a href="https://www.ns.nl/en/travel/at-the-station/facilities" rel="noreferrer">NS station facilities</a> for toilets, shops and opening hours. Listed equipment does not establish accessibility or working lifts.</p>
</footer>
</main>
</body>
</html>"""
