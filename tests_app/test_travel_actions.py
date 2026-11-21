from datetime import UTC, datetime, timedelta
from typing import Any

TOKEN = "client-one-token-0123456789abcdefgh"
OTHER = "client-two-token-0123456789abcdefgh"


class Plans:
    def __init__(self):
        self.at = datetime(2026, 11, 21, tzinfo=UTC)
        self.receipts: dict[str, dict[str, Any]] = {}
        self.changes = 0
        self.stale = False

    def now(self):
        return self.at

    def preview_travel_plan(self, change):
        return {"status": "ready", "text": "Utrecht on Tuesday.", "review_token": "known"}

    def save_travel_plan(self, change, review_token, request_id):
        if request_id in self.receipts:
            if change == {"action": "create"}:
                return self.receipts[request_id]
            return {"status": "invalid", "text": "That request ID names a different change."}
        assert review_token == "known"
        if self.stale:
            return {"status": "stale", "text": "The plan changed."}
        self.changes += 1
        self.receipts[request_id] = {"status": "saved", "text": "Saved.", "undo_id": "undo"}
        return self.receipts[request_id]

    def travel_change_receipt(self, request_id):
        return self.receipts.get(request_id)

    def list_travel_plans(self):
        return {"plans": []}


def change(request_id):
    return {
        "tool": "change_travel_plan",
        "request_id": request_id,
        "change": {"action": "create"},
    }


async def test_ask_only_applies_the_exact_approved_request(tmp_path):
    from tcc_app.travel_actions import TravelActions

    prompts = []

    async def prompt(text, approval_id):
        prompts.append((text, approval_id))
        return True

    plans = Plans()
    actions = TravelActions(plans, {"office": TOKEN, "other": OTHER}, tmp_path / "access.json")
    pending = await actions.dispatch(change("one"), TOKEN, prompt)
    assert pending["status"] == "pending"
    assert plans.changes == 0
    assert prompts[0][0] == "office asks to change a travel plan.\nUtrecht on Tuesday."
    assert await actions.dispatch(change("one"), TOKEN, prompt) == pending
    assert len(prompts) == 1

    assert actions.decide(prompts[0][1], "once")["status"] == "saved"
    assert await actions.dispatch(change("one"), TOKEN, prompt) == next(
        iter(plans.receipts.values())
    )
    different = {**change("one"), "change": {"action": "cancel"}}
    assert (await actions.dispatch(different, TOKEN, prompt))["status"] == "invalid"
    assert plans.changes == 1
    assert (await actions.dispatch(change("two"), TOKEN, prompt))["status"] == "pending"
    assert (await actions.dispatch(change("two"), OTHER, prompt))["status"] == "pending"
    assert len(prompts) == 3


async def test_always_is_durable_and_deny_and_expiry_do_not_change_the_plan(tmp_path):
    from tcc_app.travel_actions import TravelActions

    prompts = []

    async def prompt(text, approval_id):
        prompts.append(approval_id)
        return True

    plans = Plans()
    path = tmp_path / "access.json"
    agents = {"office": TOKEN, "other": OTHER}
    actions = TravelActions(plans, agents, path)
    assert (await actions.dispatch(change("zero"), None, prompt))["status"] == "denied"
    await actions.dispatch(change("one"), TOKEN, prompt)
    assert actions.decide(prompts[-1], "always")["status"] == "saved"
    restored = TravelActions(plans, agents, path)
    assert (await restored.dispatch(change("two"), TOKEN, prompt))["status"] == "saved"
    assert len(prompts) == 1

    await restored.dispatch(change("one"), OTHER, prompt)
    assert restored.decide(prompts[-1], "deny")["status"] == "denied"
    assert plans.changes == 2
    await restored.dispatch(change("two"), OTHER, prompt)
    plans.at += timedelta(hours=1)
    assert restored.decide(prompts[-1], "once")["status"] == "expired"
    assert plans.changes == 2


async def test_stale_review_cannot_set_always_allow(tmp_path):
    from tcc_app.travel_actions import TravelActions

    prompts = []

    async def prompt(text, approval_id):
        prompts.append(approval_id)
        return True

    plans = Plans()
    actions = TravelActions(plans, {"office": TOKEN}, tmp_path / "access.json")
    await actions.dispatch(change("one"), TOKEN, prompt)
    plans.stale = True
    assert actions.decide(prompts[-1], "always")["status"] == "stale"
    assert (await actions.dispatch(change("two"), TOKEN, prompt))["status"] == "pending"


async def test_always_plan_access_cannot_auto_approve_overlap_or_memory(tmp_path):
    from tcc_app.travel_actions import TravelActions
    from tcc_app.travel_preferences import TravelPreferences

    class ConflictPlans(Plans):
        def preview_travel_plan(self, change):
            if "conflict_policy" not in change:
                return {"status": "conflict", "text": "Tuesday overlaps."}
            return super().preview_travel_plan(change)

    plans = ConflictPlans()
    preferences = TravelPreferences(tmp_path / "preferences.json", plans.now)
    actions = TravelActions(plans, {"office": TOKEN}, tmp_path / "access.json", preferences)
    assert actions.set_policy("office", "always")["status"] == "saved"
    prompts = []

    async def prompt(text, approval_id):
        prompts.append((text, approval_id))
        return True

    ask = change("overlap")
    assert (await actions.dispatch(ask, TOKEN, prompt))["status"] == "conflict"
    ask["change"]["conflict_policy"] = "replace"
    assert (await actions.dispatch(ask, TOKEN, prompt))["status"] == "pending"
    assert plans.changes == 0 and "Tuesday overlaps." in prompts[-1][0]
    assert actions.decide(prompts[-1][1], "once")["status"] == "saved"

    remember = {
        "tool": "change_travel_preference",
        "request_id": "remember",
        "change": {
            "action": "save",
            "preference": {
                "name": "Office",
                "origin": "RTD",
                "destination": "UT",
                "arrival_by": "09:00",
            },
        },
    }
    assert (await actions.dispatch(remember, TOKEN, prompt))["status"] == "pending"
    assert not actions.can_always(prompts[-1][1])
    assert preferences.list()["preferences"] == []
    assert actions.decide(prompts[-1][1], "once")["status"] == "saved"
    assert preferences.list()["preferences"][0]["arrival_by"] == "09:00"
