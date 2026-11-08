"""Queued private alerts stay bound to the original authenticated webhook receiver."""

import json
from dataclasses import replace

import httpx
import pytest
from runtime_rig import DAY, Rig, cancelled_l2, clean_world

from train_commute_companion.model import EventKind
from train_commute_companion.runtime import Runtime
from train_commute_companion.serde import dumps
from train_commute_companion.webhook import url_id
from train_commute_companion.webhook_send import WebhookPoster

URL = "https://receiver.invalid/same-path"
SECRET_A, SECRET_B = "dummy-signing-key-account-a", "dummy-signing-key-account-b"
TOKEN_A, TOKEN_B = "dummy-bearer-account-a", "dummy-bearer-account-b"


@pytest.mark.parametrize("rotation", ["bearer", "signing"])
@pytest.mark.parametrize("failed_cleanup", [False, True])
async def test_same_url_new_principal_never_receives_old_queued_fact(
    tmp_path, rotation, failed_cleanup
):
    rig = Rig(tmp_path)
    rig.provider.world = clean_world()
    requests = []
    status = 200

    def receive(request):
        requests.append((request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:

        def runtime(secret, token):
            port = WebhookPoster(client, (URL,), secret, rig.clock.now, token=token)
            return Runtime(
                profile_path=rig.path,
                store=rig.store,
                provider=rig.provider,
                notifier=rig.notifier,
                clock=rig.clock,
                require_stations=False,
                webhooks=port,
            )

        rig.rt = runtime(SECRET_A, TOKEN_A)
        await rig.rt.start()
        await rig.rt.webhooks_idle()
        status = 503
        rig.provider.world = cancelled_l2(DAY)
        rig.clock.jump(2)
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        assert any(event["kind"] == EventKind.CANCELLED.value for _, event in requests)
        assert rig.store.load().webhook_copies
        serialized = dumps(rig.store.load())
        assert TOKEN_A not in serialized and SECRET_A not in serialized
        requests.clear()
        status = 200
        secret, token = (SECRET_A, TOKEN_B) if rotation == "bearer" else (SECRET_B, TOKEN_A)
        rig.store.fail = failed_cleanup
        rig.rt = runtime(secret, token)
        await rig.rt.start()
        await rig.rt.webhooks_idle()
        rig.store.fail = False
        if failed_cleanup:
            rig.rt = runtime(secret, token)
            await rig.rt.start()
        rig.clock.jump(2)
        await rig.rt.watchdog_once()
        await rig.rt.webhooks_idle()
        assert not any(event["kind"] == EventKind.CANCELLED.value for _, event in requests)
        # A newly observed occurrence of the fact can use the replacement scope.
        rig.provider.world = clean_world()
        rig.clock.jump(2)
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        rig.provider.world = cancelled_l2(DAY)
        rig.clock.jump(2)
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        assert any(
            auth == "Bearer " + token and event["kind"] == EventKind.CANCELLED.value
            for auth, event in requests
        )


@pytest.mark.parametrize("legacy", ["url_only", "unbound"])
async def test_legacy_outbox_does_not_bind_unknown_original_authentication(tmp_path, legacy):
    rig = Rig(tmp_path)
    rig.provider.world = clean_world()
    requests = []

    def receive(request):
        requests.append(json.loads(request.content))
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:

        def runtime():
            return Runtime(
                profile_path=rig.path,
                store=rig.store,
                provider=rig.provider,
                notifier=rig.notifier,
                clock=rig.clock,
                require_stations=False,
                webhooks=WebhookPoster(client, (URL,), SECRET_B, rig.clock.now, token=TOKEN_B),
            )

        rig.rt = runtime()
        await rig.rt.start()
        await rig.rt.webhooks_idle()
        rig.provider.world = cancelled_l2(DAY)
        rig.clock.jump(2)
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        state = rig.store.load()
        targets = frozenset({url_id(URL)}) if legacy == "url_only" else None
        copies = tuple(
            replace(
                copy, target_ids=targets, obligation=replace(copy.obligation, target_ids=targets)
            )
            for copy in state.webhook_copies
        )
        rig.store.save(replace(state, webhook_copies=copies))
        requests.clear()
        rig.rt = runtime()
        await rig.rt.start()
        rig.clock.jump(2)
        await rig.rt.watchdog_once()
        await rig.rt.webhooks_idle()
        assert not any(event["kind"] == EventKind.CANCELLED.value for event in requests)


async def test_old_unpinned_phone_obligation_does_not_create_copy_for_new_credentials(tmp_path):
    rig = Rig(tmp_path)
    rig.provider.world = clean_world()
    requests = []

    def receive(request):
        requests.append(json.loads(request.content))
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:

        def runtime(token):
            return Runtime(
                profile_path=rig.path,
                store=rig.store,
                provider=rig.provider,
                notifier=rig.notifier,
                clock=rig.clock,
                require_stations=False,
                webhooks=WebhookPoster(client, (URL,), SECRET_A, rig.clock.now, token=token),
            )

        rig.rt = runtime(TOKEN_A)
        await rig.rt.start()
        await rig.rt.webhooks_idle()
        rig.notifier.fails = lambda event: event.kind is EventKind.CANCELLED
        rig.provider.world = cancelled_l2(DAY)
        rig.clock.jump(2)
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        state = rig.store.load()
        assert any(o.event.kind is EventKind.CANCELLED for o in state.obligations)
        obligations = tuple(
            replace(o, target_ids=None, primary_target_pending=False, webhook_copied=False)
            if o.event.kind is EventKind.CANCELLED
            else o
            for o in state.obligations
        )
        rig.store.save(replace(state, obligations=obligations, webhook_copies=()))
        requests.clear()
        rig.rt = runtime(TOKEN_B)
        await rig.rt.start()
        rig.clock.jump(2)
        await rig.rt.watchdog_once()
        await rig.rt.webhooks_idle()
        assert not any(event["kind"] == EventKind.CANCELLED.value for event in requests)
        # The original phone obligation still remains eligible for its existing primary path.
        assert any(o.event.kind is EventKind.CANCELLED for o in rig.rt.state.obligations)


async def test_incident_clearance_is_not_forwarded_to_replacement_principal(tmp_path):
    from test_disruption_receipts import feed

    from train_commute_companion.providers.base import ActiveDisruption, ActiveDisruptions

    rig = Rig(tmp_path)
    rig.provider.world = clean_world()
    rig.provider.disruptions = feed()
    requests = []

    def receive(request):
        requests.append((request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:

        def runtime(token):
            return Runtime(
                profile_path=rig.path,
                store=rig.store,
                provider=rig.provider,
                notifier=rig.notifier,
                clock=rig.clock,
                require_stations=False,
                webhooks=WebhookPoster(client, (URL,), SECRET_A, rig.clock.now, token=token),
            )

        rig.rt = runtime(TOKEN_A)
        await rig.rt.start()
        await rig.rt.webhooks_idle()
        assert any(
            auth == "Bearer " + TOKEN_A and e["kind"] == "disruption" for auth, e in requests
        )
        requests.clear()
        rig.rt = runtime(TOKEN_B)
        await rig.rt.start()
        await rig.rt.webhooks_idle()
        rig.provider.disruptions = feed(False)
        rig.clock.jump(2)
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        assert not any(e["kind"] in {"disruption", "disruption_cleared"} for _, e in requests)
        rig.provider.disruptions = ActiveDisruptions(
            (ActiveDisruption("new-incident", "New fault", frozenset({"UT"}), None),), "complete"
        )
        rig.clock.jump(2)
        await rig.rt.poll_once()
        await rig.rt.webhooks_idle()
        assert any(
            auth == "Bearer " + TOKEN_B and e["kind"] == "disruption" for auth, e in requests
        )


def test_identity_uses_normalized_address_actual_signing_key_and_bearer_scope():
    import base64

    from train_commute_companion.webhook import transport_id

    equivalent = "whsec_" + base64.b64encode(SECRET_A.encode()).decode()
    one = transport_id(URL, SECRET_A, TOKEN_A)
    assert one == transport_id(URL, equivalent, TOKEN_A)
    assert one != transport_id(URL, SECRET_A, TOKEN_B)
    assert one != transport_id(URL, SECRET_B, TOKEN_A)
    assert one != transport_id(URL + "/other", SECRET_A, TOKEN_A)
    assert one != url_id(URL) and one.startswith("wh1_")
    assert not any(value in one for value in (URL, SECRET_A, TOKEN_A))
