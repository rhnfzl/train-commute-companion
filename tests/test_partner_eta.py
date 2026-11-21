import asyncio
from datetime import timedelta

import pytest
import runtime_seam as seam
from builders import make_connection, make_leg
from lifecycle_fixtures import journey, occ
from runtime_rig import DAY, PROFILE, Rig, at

from train_commute_companion.model import Direction, LegState, OccurrenceKey, okey
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.runtime import Runtime
from train_commute_companion.state import PollRecord

HOME = Direction.RETURN


@pytest.mark.parametrize(
    "enabled, phone, delay, paused, expected",
    [
        (False, "partner", 12, False, False),
        (True, None, 12, False, False),
        (True, "partner", 9, False, False),
        (True, "partner", 12, False, True),
        (True, "partner", 12, True, False),
    ],
)
async def test_private_return_estimate_requires_opt_in_target_and_delay(
    tmp_path, enabled, phone, delay, paused, expected
):
    rig = Rig(tmp_path, "17:30")
    profile = PROFILE.replace(
        "    name:",
        (
            f"    alerts: {{partner_eta: {str(enabled).lower()}, partner_phone: {phone or 'null'}}}"
            "\n    name:"
        ),
        1,
    )
    rig.write(text=profile)
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="4242")])
    ]
    delivered = []

    async def send(target, message, allowed):
        if allowed():
            delivered.append((target, message))

    async def start():
        rig.rt = Runtime(
            profile_path=rig.path,
            store=rig.store,
            provider=rig.provider,
            notifier=rig.notifier,
            clock=rig.clock,
            require_stations=False,
            send_partner_eta=send,
        )
        await rig.rt.start()

    await start()
    await rig.rt.leaving("p1", DAY, HOME)
    await rig.run_until("17:54")
    if paused:
        rig.write(
            text=profile.replace(
                "    name:", f'    pauses: [{{start: "{DAY}", end: "{DAY}"}}]\n    name:', 1
            )
        )
        await rig.rt.reload()
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="4242", delay_arr=delay)])
    ]
    await rig.run_until("17:58")
    await rig.rt._partner_refresh.idle()
    desired = [("partner", "Train 12 min late. Expected arrival around 18:42.")] if expected else []
    assert delivered == desired
    assert all("Expected arrival around" not in e.text for e in rig.notifier.attempts)
    await start()
    await rig.run_until("18:02")
    await rig.rt._partner_refresh.idle()
    assert delivered == desired


@pytest.mark.parametrize(
    "update, expected",
    [
        ("expired", False),
        ("opt_out", False),
        ("new_phone", False),
        ("removed_profile", False),
        ("wake", True),
        ("checked_alternatives", True),
    ],
)
async def test_pending_estimate_checks_only_current_consent_and_train_evidence(
    tmp_path, update, expected
):
    rig = Rig(tmp_path, "17:30")
    profile = PROFILE.replace(
        "    name:", "    alerts: {partner_eta: true, partner_phone: partner}\n    name:", 1
    )
    rig.write(text=profile)
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="4242", delay_arr=12)])
    ]
    began, resume = asyncio.Event(), asyncio.Event()
    delivered = []

    async def send(target, message, allowed):
        began.set()
        await resume.wait()
        if allowed():
            delivered.append(message)

    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        send_partner_eta=send,
    )
    await rig.rt.start()
    await rig.rt.leaving("p1", DAY, HOME)
    await rig.run_until("17:54")
    await asyncio.wait_for(began.wait(), 2)
    if update == "expired":
        rig.clock.set("18:01")
    elif update == "removed_profile":
        rig.rt._excluded_profiles = lambda: frozenset({"p1"})
    else:
        current = {
            "opt_out": "partner_eta: false, partner_phone: partner",
            "new_phone": "partner_eta: true, partner_phone: other",
            "wake": 'partner_eta: true, partner_phone: partner, wake_at: "06:50"',
            "checked_alternatives": "partner_eta: true, partner_phone: partner, checked_alternatives: false",
        }[update]
        rig.write(text=profile.replace("partner_eta: true, partner_phone: partner", current))
        await rig.rt.reload()
    resume.set()
    await rig.rt._partner_refresh.idle()
    assert delivered == (["Train 12 min late. Expected arrival around 18:42."] if expected else [])


@pytest.mark.parametrize("pause", [None, "commute", "return"])
async def test_partner_receipts_and_pauses_use_the_return_service_date(tmp_path, pause):
    rig = Rig(tmp_path)
    profile = PROFILE.replace(
        "    name:", "    alerts: {partner_eta: true, partner_phone: partner}\n    name:", 1
    )
    today = DAY + timedelta(days=1)
    if pause:
        paused = DAY if pause == "commute" else today
        profile = profile.replace(
            "    name:", f'    pauses: [{{start: "{paused}", end: "{paused}"}}]\n    name:', 1
        ).replace(
            'return: {first_train: "17:52"', 'return: {day_offset: 1, first_train: "00:15"', 1
        )
        profile = profile.split('  - id: "p2"')[0]
    rig.write(text=profile)
    stamp = at("00:23", today)
    rig.clock.t = stamp
    received = []

    async def send(target, message, allowed):
        if allowed():
            received.append(message)

    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        send_partner_eta=send,
    )
    await rig.rt.start()
    assert rig.rt.config.profiles[0].return_.day_offset == (1 if pause else 0)
    states = {}
    for day, dep, arr, delay, number in (
        (DAY, "23:52", "23:55", 56, "771"),
        (today, "00:15", "00:50", 12, "772"),
    ):
        if pause and day != today:
            continue
        leg = make_leg("RTD", "AAA", dep, arr, number=number, delay_arr=delay, day=day)
        key = OccurrenceKey("p1", HOME, day)
        states[okey(key)] = occ(
            journey([leg], [LegState.RIDING]),
            key=key,
            travelling=True,
            poll_records=(PollRecord(stamp, True, None),),
        )
        query = Query("RTD", "AAA", stamp)
        rig.rt._memory.boards[okey(key)] = (
            ProviderAnswer(query, (make_connection([leg]),), stamp),
        )
    seam.seed_state(rig.rt, occurrences=states)

    await rig.rt.partner_eta_once()
    await rig.rt.partner_eta_once()

    expected = [] if pause == "return" else ["Train 12 min late. Expected arrival around 01:02."]
    if pause is None:
        expected.insert(0, "Train 56 min late. Expected arrival around 00:51.")
    assert received == expected
    assert set(rig.store.load().partner_eta_days) == (set(states) if expected else set())
