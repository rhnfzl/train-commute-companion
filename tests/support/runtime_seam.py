"""The one place tests reach inside a Runtime, for what no public method shows or sets.

A refactor of the runtime's internals changes this file, not the tests that read through it."""

from dataclasses import replace

from train_commute_companion.state_writer import Change


def seed_state(rt, **changes) -> None:
    """Sets fields of the state as a restart would find them, past the runtime's transitions."""
    rt._writer.propose(replace(rt._writer.st, **changes))


def save_state(rt) -> None:
    rt._writer.commit(rt._writer.st, Change.RECOMPUTED)


def held(rt):
    return rt._writer._journal.held


def owed_copies(rt):
    return rt._queue.copies


def held_copies(rt):
    return rt._queue.held


def webhook_port(rt):
    return rt._queue.port


def stop_webhooks(rt):
    return rt._queue.stop()


def memory(rt):
    return rt._memory


def budget(rt):
    return rt._gate.budget


def ledger(rt):
    return rt._ledger


def seen(rt):
    return rt._memory.seen


def location_epochs(rt):
    return rt._loc_epoch


def requires_stations(rt) -> bool:
    return rt._require_stations


def store(rt):
    return rt._store


def clock(rt):
    return rt._clock


def provider(rt):
    return rt._provider


def obligation(rt, event, s, now):
    return rt._obligation(event, s, now)


def app_warning(rt, kind, body, now, tag: str = "") -> None:
    rt._app_warning(kind, body, now, tag)


def deliver(rt):
    return rt._deliver()


def fetch(rt, q, live, window_end, deadline=None):
    return rt._gate.trips(q, live, window_end, deadline)


def lookahead_direction(rt, p, d, works):
    return rt._lookahead_direction(p, d, works)


def remember_usual(rt, key, ctx, merged) -> None:
    rt._remember_usual(key, ctx, merged)


def poll_memory(rt):
    return rt._memory
