"""A private, read-only page showing the same journey information as Telegram status."""

from datetime import datetime
from functools import partial
from html import escape
from pathlib import Path

from train_commute_companion.calendar import TZ
from train_commute_companion.calendar_export import CalendarExportError, export_id, planned_bounds
from train_commute_companion.catalogue import message, message_date
from train_commute_companion.history import stats_text
from train_commute_companion.model import SentAlert, display_name
from train_commute_companion.view import RuntimeView

from .commands import profile_language, render_status, shared_language

TODAY_PATH = "/today"
PAUSED_PATH = "/today/paused"
MANIFEST_PATH = "/today/manifest.webmanifest"
ICONS = {
    f"/today/icon-{size}.png": Path(__file__).with_name(f"icon-{size}.png") for size in (192, 512)
}
MANIFEST = {
    "name": "Train Commute Companion",
    "short_name": "Commute Today",
    "start_url": TODAY_PATH,
    "scope": TODAY_PATH,
    "display": "standalone",
    "background_color": "#eef2f7",
    "icons": [
        {"src": path, "sizes": f"{size}x{size}", "type": "image/png"}
        for path, size in zip(ICONS, (192, 512), strict=True)
    ],
}

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


def render(
    view: RuntimeView, confirmed: bool, *, paused: bool = False, alerts: tuple[SentAlert, ...] = ()
) -> str:
    """Escape every value from the read model. Download links use opaque occurrence hashes and contain no credentials."""
    language = shared_language(view)
    page = partial(message, language=language)

    def stamp(at: datetime) -> str:
        local = at.astimezone(TZ)
        if language == "en":
            return f"{local:%a %d %b %Y, %H:%M %Z}"
        return f"{message_date(local.date(), language)} {local:%Y, %H:%M %Z}"

    freshness = []
    downloads = []
    for occurrence in view.occurrences:
        if occurrence.resolved:
            continue
        health = occurrence.health
        checked = stamp(health.last_ok_at) if health.last_ok_at else page("today.unknown")
        locale = profile_language(view, occurrence.key.profile_id)
        text = message(
            "today.checked",
            locale,
            name=display_name(occurrence.profile_name),
            direction=message(f"direction.{occurrence.key.direction.value}", locale),
            freshness=message("today.fresh" if health.fresh else "today.stale", locale),
            checked=checked,
        )
        freshness.append("<li>" + escape(text) + "</li>")
    for occurrence in view.occurrences:
        try:
            planned_bounds(occurrence)
        except CalendarExportError:
            continue
        target = f"/calendar/{export_id(occurrence.key)}.ics"
        locale = profile_language(view, occurrence.key.profile_id)
        direction = message(f"direction.{occurrence.key.direction.value}", locale)
        day = occurrence.key.service_date
        date_text = (
            f"{day:%a %d %b %Y}" if locale == "en" else f"{message_date(day, locale)} {day.year}"
        )
        label = escape(f"{display_name(occurrence.profile_name)}, {direction}, {date_text}")
        downloads.append(
            f'<li>{label}: <a href="{target}">{page("today.calendar_download")}</a>. '
            f'<a href="/preview/{export_id(occurrence.key)}.html">{page("today.preview_download")}</a></li>'
        )
    calendar = (
        f'<section aria-labelledby="calendar-heading"><h2 id="calendar-heading">{page("today.save_journey")}</h2>'
        f"<p>{page('today.tentative')}</p><p>{page('today.preview_privacy')}</p>"
        f"<ul>{''.join(downloads)}</ul></section>"
        if downloads
        else ""
    )
    data = (
        "<ul>" + "".join(freshness) + "</ul>" if freshness else f"<p>{page('today.no_checks')}</p>"
    )
    history = ""
    if any(p.alerts.alert_history for p in view.profiles):
        items = []
        for alert in alerts:
            sent = alert.accepted_at.astimezone(TZ)
            items.append(
                f'<li><time datetime="{sent.isoformat(timespec="seconds")}">'
                f"{stamp(sent)}</time><pre>{escape(alert.text)}</pre></li>"
            )
        history = (
            f'<section aria-labelledby="history-heading"><h2 id="history-heading">{page("today.alerts")}</h2>'
            + ("<ul>" + "".join(items) + "</ul>" if items else f"<p>{page('today.no_alerts')}</p>")
            + "</section>"
        )
    snapshot = escape(stamp(view.at))
    snapshot_iso = escape(view.at.isoformat(timespec="seconds"))
    statistics = (
        f"<section><h2>{page('stats.heading')}</h2>"
        + "".join(
            f"<h3>{page('stats.period', days=days)}</h3><p>{escape(stats_text(stats, language))}</p>"
            for days, stats in view.commute_totals.items()
        )
        + "</section>"
        if view.commute_totals
        else ""
    )
    status = escape(render_status(view, confirmed))
    service = page("today.ready" if view.started else "today.starting")
    delivery = page("today.confirmed" if confirmed else "today.unconfirmed")
    live = bool(view.profiles) and all(p.alerts.live_today for p in view.profiles)
    metadata = controls = ""
    refresh_path = PAUSED_PATH if paused else TODAY_PATH
    if live:
        metadata = (
            f'<link rel="manifest" href="{MANIFEST_PATH}">\n'
            '<link rel="apple-touch-icon" href="/today/icon-192.png">\n'
        )
        if paused:
            controls = f' <a href="{TODAY_PATH}">{page("today.resume")}</a>.'
        else:
            metadata += f'<meta http-equiv="refresh" content="60; url={TODAY_PATH}">\n'
            controls = (
                f' {page("today.interval")} <a href="{PAUSED_PATH}">{page("today.pause")}</a>.'
            )
    link = (
        '<a href="https://www.ns.nl/en/travel/at-the-station/facilities" rel="noreferrer">'
        f"{page('today.facilities_label')}</a>"
    )
    return f"""<!doctype html>
<html lang="{language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{metadata}<title>{page("today.title")} | Train Commute Companion</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<header>
<h1>{page("today.title")}</h1>
<p>{page("today.intro")}</p>
<p>{page("today.snapshot")}: <time datetime="{snapshot_iso}">{snapshot}</time>. <a href="{refresh_path}">{page("today.refresh")}</a>.{controls}</p>
</header>
<section aria-labelledby="journeys-heading">
<h2 id="journeys-heading">{page("today.journeys")}</h2>
<p>{page("today.state", service=service, delivery=delivery)}</p>
<pre>{status}</pre>
</section>
<section aria-labelledby="freshness-heading">
<h2 id="freshness-heading">{page("today.freshness")}</h2>
{data}
</section>
{history}
{calendar}
{statistics}
<footer>
<p>{page("today.recovery")}</p>
<p>{page("today.telegram")}</p>
<p>{page("today.facilities", link=link)}</p>
</footer>
</main>
</body>
</html>"""
