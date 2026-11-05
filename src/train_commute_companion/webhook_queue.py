"""The owed webhook copies (7.12 rules 5, 16, 22): one durable record per alert handed to the phone,
posted in a task of their own so a slow receiver never holds up a phone send."""

import asyncio
import logging
from collections.abc import Callable, Collection
from dataclasses import replace
from datetime import datetime, timedelta

from train_commute_companion.calendar import to_utc
from train_commute_companion.delivery import JOURNEY_WARNINGS, LedgerView, post_copy
from train_commute_companion.model import Obligation, OccurrenceKey, WebhookCopy
from train_commute_companion.notify import WebhookPort
from train_commute_companion.revalidation import covers
from train_commute_companion.store import PersistedState
from train_commute_companion.webhook import event_id, url_id

log = logging.getLogger(__name__)

# webhooks: a copy is given up GIVE_UP after its first try; at most
# MAX_COPIES are owed (one try a minute cannot keep up with faster alerts)
GIVE_UP = timedelta(hours=2)
MAX_COPIES = 200


class CopyQueue:
    """The one live record of owed webhook copies, written with every save and never rebuilt from
    the running state (rule 22: a failed write resets that). `held`: copies of alerts held in
    memory, judged with the held view. `copied`: obligations whose copy was made since the last
    delivery write landed (a failed write resets their `webhook_copied`)."""

    def __init__(
        self,
        port: WebhookPort | None,
        now: Callable[[], datetime],
        record: Callable[[], None],
        waits: Callable[[Obligation], bool],
        ledger: Callable[[bool], LedgerView],
    ) -> None:
        self.port, self._now, self._record = port, now, record
        self._waits, self._ledger = waits, ledger
        if port is not None:
            port.on_reserve = record  # the rests are written before a try's request leaves
        self.copies: dict[str, WebhookCopy] = {}
        self.held: set[str] = set()
        self.copied: set[str] = set()
        self._post_task: asyncio.Task[None] | None = None  # the running webhook pass
        self._post_again = False

    def with_copies(self, st: PersistedState) -> PersistedState:
        """`st` with the owed copies and each URL's rest, as every save writes them."""
        port = self.port
        down = {} if port is None else {url_id(u): t for u, t in port.down.items()}
        return replace(st, webhook_copies=tuple(self.copies.values()), webhook_down=down)

    def restore(self, st: PersistedState) -> None:
        """What a loaded state owed."""
        port = self.port
        if port is not None:
            self.copies = {c.id: c for c in st.webhook_copies}
            # a restart keeps each URL's rest and outage: no extra try, no second warning
            saved = st.webhook_down
            port.down.update({u: saved[url_id(u)] for u in port.urls if url_id(u) in saved})
        elif n := len(st.webhook_copies):
            # nothing could post them, and a later poster would get stale alerts
            log.info("dropped %d webhook copies: webhooks are off", n)

    def drop_warnings(self, keys: Collection[OccurrenceKey]) -> None:
        """The journey warnings owed for these occurrences (a skip, rule 5(a))."""
        for c in list(self.copies.values()):
            if c.obligation.event.kind in JOURNEY_WARNINGS and c.obligation.occurrence in keys:
                self.drop(c.id)

    def drop(self, cid: str) -> None:
        self.copies.pop(cid, None)
        self.held.discard(cid)

    def stage(self, o: Obligation, held: bool) -> bool:
        """The webhook copy of an obligation handed to the phone; True when one was made.
        `held`: an alert held in memory, its copy judged with the held view."""
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
        self.copies[cid] = WebhookCopy(cid, o)
        if held:
            self.held.add(cid)
        return True

    def rewrite(self, c: WebhookCopy, o: Obligation) -> None:
        """The same copy (webhook-id, receipts, tries) now owing `o`, judged on what the write
        tried (the held view) until it lands: a failed write must not drop it (rule 22)."""
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
        )
        if all(url_id(u) in merged.done for u in port.urls):
            self.drop(c.id)
        else:
            self.copies[c.id] = merged

    def post_soon(self) -> None:
        """A webhook pass now, or one more round of the running one (copies made meanwhile)."""
        port = self.port
        if port is None:
            return
        if self._post_task is not None and not self._post_task.done():
            self._post_again = True
            return
        self._post_task = asyncio.get_running_loop().create_task(self._post_passes(port))

    async def _post_passes(self, port: WebhookPort) -> None:
        again = True
        while again:
            self._post_again = False
            before = dict(self.copies)
            try:
                await self._post_copies(port)
            except Exception:  # a broken pass must not stop the next one
                log.exception("webhook pass failed")
            finally:  # a copy that ended or was tried is recorded, also when shutdown cancels
                if self.copies != before:
                    self._record()
            again = self._post_again

    async def idle(self) -> None:
        """Waits until no webhook pass runs (a clean shutdown, tests)."""
        while (t := self._post_task) is not None and not t.done():
            await asyncio.wait({t})

    async def stop(self) -> None:
        if (t := self._post_task) is not None and not t.done():
            t.cancel()
            await asyncio.wait({t})

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
                cur = self.copies[x.id] = x
                self._record()

            r = await post_copy(c, v, port, now, started)
            self._merge(port, cur, r)
