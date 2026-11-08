"""The owed webhook copies (7.12 rules 5, 16, 22): one durable record per alert handed to the phone,
posted in a task of their own so a slow receiver never holds up a phone send."""

import asyncio
import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from enum import StrEnum

from train_commute_companion.calendar import to_utc
from train_commute_companion.delivery import (
    JOURNEY_WARNINGS,
    Ledger,
    copy_destinations,
    covers,
    post_copy,
)
from train_commute_companion.model import Obligation, OccurrenceKey, WebhookCopy, alert_facts
from train_commute_companion.notify import WebhookPort, destination_id
from train_commute_companion.store import PersistedState
from train_commute_companion.webhook import event_id

log = logging.getLogger(__name__)

# webhooks: a copy is given up GIVE_UP after its first try; at most
# MAX_COPIES are owed (one try a minute cannot keep up with faster alerts)
GIVE_UP = timedelta(hours=2)
MAX_COPIES = 200


class QueuePhase(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    RERUN_REQUESTED = "rerun_requested"
    STOPPING = "stopping"
    STOPPED = "stopped"


class CopyQueue:
    """The one live record of owed webhook copies, written with every save and never rebuilt from
    the running state (rule 22: a failed write resets that). `held`: copies of alerts held in
    memory, judged with the held ledger. `copied`: obligations whose copy was made since the
    last delivery write landed (a failed write resets their `webhook_copied`)."""

    def __init__(
        self,
        port: WebhookPort | None,
        now: Callable[[], datetime],
        record: Callable[[Sequence[tuple[Obligation, str]]], None],
        waits: Callable[[Obligation], bool],
        ledger: Callable[[bool], Ledger],
        audience_written: Callable[[WebhookCopy], bool] = lambda c: True,
    ) -> None:
        self.port, self._now, self._record = port, now, record
        self._waits, self._ledger = waits, ledger
        self._audience_written = audience_written
        if port is not None:
            port.on_reserve = lambda: record(
                ()
            )  # the rests are written before a try's request leaves
        self._parked_down: dict[str, datetime] = {}
        self.copies: dict[str, WebhookCopy] = {}
        self.held: set[str] = set()
        self.copied: set[str] = set()
        self._post_task: asyncio.Task[None] | None = None  # the running webhook pass
        self.phase = QueuePhase.IDLE

    def with_copies(self, st: PersistedState) -> PersistedState:
        """`st` with the owed copies and each URL's rest, as every save writes them."""
        port = self.port
        current_ids = set() if port is None else {destination_id(port, u) for u in port.urls}
        down = {
            identity: until
            for identity, until in self._parked_down.items()
            if identity not in current_ids
        }
        if port is not None:
            down.update(
                {
                    destination_id(port, u): t
                    for u, t in port.down.items()
                    if u in port.destination_ids
                }
            )
        return replace(st, webhook_copies=tuple(self.copies.values()), webhook_down=down)

    def restore(
        self, st: PersistedState, *, preserve: Callable[[Obligation], bool] = lambda o: False
    ) -> None:
        """What a loaded state owed."""
        port = self.port
        parked = {c.id: c for c in st.webhook_copies if preserve(c.obligation)}
        if parked:
            self._parked_down = dict(st.webhook_down)
        if port is not None:
            self.copies = {
                c.id: c if c.target_ids is not None else replace(c, target_ids=frozenset())
                for c in st.webhook_copies
            }
            # a restart keeps each URL's rest and outage: no extra try, no second warning
            saved = st.webhook_down
            port.down.update(
                {
                    u: saved[destination_id(port, u)]
                    for u in port.urls
                    if destination_id(port, u) in saved
                }
            )
        elif n := len(st.webhook_copies):
            self.copies = parked
            # nothing could post them, and a later poster would get stale alerts
            log.info("dropped %d webhook copies: webhooks are off", n - len(parked))

    def drop_warnings(self, keys: Collection[OccurrenceKey]) -> None:
        """The journey warnings owed for these occurrences (a skip, rule 5(a))."""
        for c in list(self.copies.values()):
            if c.obligation.event.kind in JOURNEY_WARNINGS and c.obligation.occurrence in keys:
                self.drop(c.id)

    def drop(self, cid: str) -> None:
        self.copies.pop(cid, None)
        self.held.discard(cid)

    def stage(self, o: Obligation, held: bool) -> bool:
        """The webhook copy of an obligation handed to the phone; True when one was made (not
        with webhooks off, nor twice for one alert). `held`: an alert held in memory, its copy
        judged with the held ledger."""
        if self.port is None or o.webhook_copied or o.id in self.copied:
            return False
        self.copied.add(o.id)
        cid = event_id(o.id)
        if cid in self.copies:
            return False
        # rule 5(b): the copies this message covers end now, so a receiver that comes back never
        # gets them after it, also once its own copy was accepted or a restart came between
        for c in [c for c in self.copies.values() if covers(o, c.obligation)]:
            self.drop(c.id)
        if len(self.copies) >= MAX_COPIES:  # overload: a never-tried copy goes first
            pool = [c for c in self.copies.values() if c.first_try is None]
            old = min(
                pool or self.copies.values(), key=lambda c: to_utc(c.obligation.event.created_at)
            )
            self.drop(old.id)
            log.warning("dropped a webhook copy: more than %d owed", MAX_COPIES)
        targets = frozenset(destination_id(self.port, u) for u in self.port.urls)
        if o.target_ids is not None:
            targets &= o.target_ids
        pinned = alert_facts(o.event).disruption_targets
        if pinned is not None:
            targets &= pinned
        self.copies[cid] = WebhookCopy(cid, o, target_ids=targets)
        if held:
            self.held.add(cid)
        return True

    def marked(self, o: Obligation) -> Obligation:
        """`o` as a write holds it: marked when its copy was made since the last delivery write."""
        return replace(o, webhook_copied=True) if o.id in self.copied else o

    def after_send(self, kept: Obligation) -> Obligation:
        """An alert its send kept: its copy was made before the send, so the retry makes none."""
        return kept if self.port is None else replace(kept, webhook_copied=True)

    def rewrite(self, c: WebhookCopy, o: Obligation) -> None:
        """The same copy (webhook-id, receipts, tries) now owing `o`, judged on what the write
        tried (the held ledger) until it lands: a failed write must not drop it (rule 22)."""
        self.copies[c.id] = replace(c, obligation=o)
        self.held.add(c.id)

    def delivery_written(self, in_memory: Collection[str]) -> None:
        """A delivery write landed: the marks it wrote end, except those of alerts it did not
        hold yet (`in_memory`, still sent from memory when the write was made)."""
        self.copied &= set(in_memory)

    def _merge(self, port: WebhookPort, c: WebhookCopy, r: WebhookCopy | None) -> None:
        """A copy's try result, merged into what is there now: `act`, a poll or the look-ahead
        run can change the copies while the POST awaits."""
        cur = self.copies.get(c.id)
        if cur is None:
            return  # skipped or given up meanwhile: never restored
        if cur is c:
            if r is None:
                self.drop(c.id)
            elif r is not c:
                self.copies[c.id] = r
            return
        if r is None:  # every URL `c` still owed took the text sent at the try
            self.drop(c.id)
            return
        tries = [t for t in (cur.last_try, r.last_try) if t is not None]
        merged = replace(
            cur,
            done=cur.done + tuple(u for u in r.done if u not in cur.done),
            first_try=cur.first_try or r.first_try,
            last_try=max(tries, key=to_utc, default=None),
            target_ids=cur.target_ids if cur.target_ids is not None else r.target_ids,
        )
        if all(destination_id(port, u) in merged.done for u in copy_destinations(merged, port)):
            self.drop(c.id)
        else:
            self.copies[c.id] = merged

    def post_soon(self) -> None:
        """A webhook pass now, or one more round of the running one (copies made meanwhile)."""
        port = self.port
        if port is None or self.phase in (QueuePhase.STOPPING, QueuePhase.STOPPED):
            return
        if self.phase in (QueuePhase.RUNNING, QueuePhase.RERUN_REQUESTED):
            self.phase = QueuePhase.RERUN_REQUESTED
            return
        self.phase = QueuePhase.RUNNING
        self._post_task = asyncio.get_running_loop().create_task(self._post_passes(port))

    async def _post_passes(self, port: WebhookPort) -> None:
        try:
            while True:
                self.phase = QueuePhase.RUNNING
                before = dict(self.copies)
                try:
                    await self._post_copies(port)
                except Exception:
                    log.exception("webhook pass failed")
                finally:
                    if self.copies != before:
                        self._record(())
                if self.phase is not QueuePhase.RERUN_REQUESTED:
                    break
        finally:
            self.phase = (
                QueuePhase.STOPPED if self.phase is QueuePhase.STOPPING else QueuePhase.IDLE
            )

    async def idle(self) -> None:
        """Waits until no webhook pass runs (a clean shutdown, tests)."""
        while (t := self._post_task) is not None and not t.done():
            await asyncio.wait({t})

    async def stop(self) -> None:
        self.phase = QueuePhase.STOPPING
        if (t := self._post_task) is not None and not t.done():
            t.cancel()
            await asyncio.wait({t})
        self.phase = QueuePhase.STOPPED

    async def _post_copies(self, port: WebhookPort) -> None:
        """After the phone sends: each owed copy once. Never tried first, oldest first, then
        least recently tried first, so a failing URL's one try per rest goes to every copy in
        turn. The give-up check comes before every wait."""
        for c in sorted(
            self.copies.values(),
            key=lambda c: (
                c.first_try is not None,
                to_utc(c.last_try or c.obligation.event.created_at),
                to_utc(c.obligation.event.created_at),
            ),
        ):
            if self.copies.get(c.id) is not c:
                continue  # removed or replaced since
            now = self._now()
            if c.first_try is not None and to_utc(now) - to_utc(c.first_try) >= GIVE_UP:
                self.drop(c.id)
                log.warning("gave up a webhook copy after 2 h")
                continue
            if self._waits(c.obligation):
                continue
            v = self._ledger(c.id in self.held)
            cur = c

            def started(x: WebhookCopy) -> None:  # the first try is written before it goes out
                nonlocal cur
                accepted = tuple(
                    (x.obligation, destination)
                    for destination in x.done
                    if destination not in cur.done
                )
                self._merge(port, cur, x)
                cur = x
                self._record(accepted)

            r = await post_copy(c, v, port, now, started, audience_written=self._audience_written)
            self._merge(port, cur, r)
