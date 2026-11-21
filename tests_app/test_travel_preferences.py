from datetime import UTC, datetime, timedelta

from tcc_app.travel_preferences import TravelPreferences

NOW = datetime(2026, 11, 21, tzinfo=UTC)
PREF = {
    "name": "Utrecht office",
    "origin": "GVC",
    "destination": "UT",
    "arrival_by": "09:00",
    "return_on_leave": True,
}
SAVE = {"action": "save", "preference": PREF}


def test_preferences_are_saved_only_after_review_and_survive_restart(tmp_path):
    path = tmp_path / "travel-preferences.json"
    prefs = TravelPreferences(path, lambda: NOW)
    review = prefs.preview(SAVE)
    assert review["status"] == "ready"
    assert "09:00" in review["text"] and "UT" in review["text"]
    assert prefs.list()["preferences"] == []
    assert not path.exists()

    reply = prefs.save(SAVE, review["review_token"], "request-1")
    assert reply["status"] == "saved"
    again = TravelPreferences(path, lambda: NOW)
    assert again.list()["preferences"] == [PREF]
    assert again.receipt("request-1")["status"] == "saved"
    assert again.save(SAVE, "expired", "request-1")["status"] == "saved"

    forget = {"action": "forget", "name": "utrecht office"}
    review = again.preview(forget)
    assert again.save(forget, review["review_token"], "request-2")["status"] == "saved"
    assert again.list()["preferences"] == []
    assert "Utrecht office" not in path.read_text()


def test_preference_review_is_bound_to_exact_change_and_revision(tmp_path):
    prefs = TravelPreferences(tmp_path / "travel-preferences.json", lambda: NOW)
    later = {"action": "save", "preference": {**PREF, "arrival_by": "10:00"}}
    original = prefs.preview(SAVE)
    replacement = prefs.preview(later)
    assert prefs.save(later, original["review_token"], "request-1")["status"] != "saved"
    assert prefs.save(later, replacement["review_token"], "request-2")["status"] == "saved"
    assert prefs.save(SAVE, original["review_token"], "request-3")["status"] != "saved"
    assert prefs.save(SAVE, replacement["review_token"], "request-2")["status"] != "saved"
    assert prefs.list()["preferences"] == [later["preference"]]


def test_expired_and_unwritten_preferences_do_not_take_effect(tmp_path, monkeypatch):
    clock = [NOW]
    path = tmp_path / "travel-preferences.json"
    prefs = TravelPreferences(path, lambda: clock[0])
    old = prefs.preview(SAVE)
    clock[0] += timedelta(days=1)
    assert prefs.save(SAVE, old["review_token"], "expired")["status"] != "saved"

    monkeypatch.setattr("tcc_app.travel_preferences.save_private_json", lambda *_: False)
    current = prefs.preview(SAVE)
    assert prefs.save(SAVE, current["review_token"], "request")["status"] == "retry"
    assert prefs.list()["preferences"] == [] and prefs.receipt("request") is None
    assert not path.exists()


def test_preferences_fill_only_missing_fields_for_matching_trips(tmp_path):
    prefs = TravelPreferences(tmp_path / "travel-preferences.json", lambda: NOW)
    review = prefs.preview(SAVE)
    assert prefs.save(SAVE, review["review_token"], "save")["status"] == "saved"
    change = {
        "action": "create",
        "plan": {
            "origin": "GVC",
            "destination": "UT",
            "outbound": {"mode": "window", "earliest": "07:00", "latest": "08:30"},
            "return": {"earliest": "16:00", "latest": "23:00"},
        },
    }
    planned = prefs.with_defaults(change)["plan"]
    assert planned["outbound"]["arrive_by"] == "09:00"
    assert planned["return"]["mode"] == "on_leave"
    assert "arrive_by" not in change["plan"]["outbound"]

    named = prefs.with_defaults(
        {"action": "create", "preference_name": "UTRECHT OFFICE", "plan": {"return": {}}},
        profile_origin="RTD",
    )
    assert "preference_name" not in named
    assert named["plan"] == {
        "origin": "GVC",
        "destination": "UT",
        "outbound": {"mode": "window", "arrive_by": "09:00"},
        "return": {"mode": "on_leave"},
    }

    explicit = {
        "action": "edit",
        "plan": {
            **change["plan"],
            "outbound": {"mode": "window", "arrive_by": "10:00"},
            "return": {"mode": "exact", "departure": "18:10"},
        },
    }
    assert prefs.with_defaults(explicit) == explicit
    exact = {
        "action": "create",
        "plan": {
            "origin": "GVC",
            "destination": "UT",
            "outbound": {"mode": "exact", "departure": "08:12"},
            "return": None,
        },
    }
    assert prefs.with_defaults(exact) == exact
    other_origin = {"action": "create", "plan": {"origin": "RTD", "destination": "UT"}}
    assert prefs.with_defaults({**other_origin, "preference_name": PREF["name"]}) == other_origin

    ambiguous = {"action": "save", "preference": {**PREF, "name": "Utrecht visit"}}
    review = prefs.preview(ambiguous)
    assert prefs.save(ambiguous, review["review_token"], "second")["status"] == "saved"
    assert prefs.with_defaults(change) == change


def test_repeated_choices_yield_a_dismissible_offer_without_automatic_memory(tmp_path):
    path = tmp_path / "travel-preferences.json"
    prefs = TravelPreferences(path, lambda: NOW)
    plans = [
        {
            "plan_id": f"travel-{i}",
            "start_date": f"2026-11-{day:02}",
            "origin": "GVC",
            "destination": "UT",
            "outbound": {"mode": "window", "arrive_by": "09:00"},
            "return": {"mode": "on_leave"},
        }
        for i, day in enumerate((3, 10, 17))
    ]
    assert prefs.suggest([plans[0]] * 3) is None
    assert prefs.suggest([*plans[:2], {**plans[2], "start_date": plans[1]["start_date"]}]) is None
    suggestion = prefs.suggest(plans)
    assert suggestion is not None and "09:00" in suggestion["text"]
    offered = suggestion["change"]
    assert offered["preference"]["return_on_leave"] is True
    assert prefs.list()["preferences"] == []
    assert prefs.dismiss(offered)
    assert prefs.suggest(plans) is None

    again = TravelPreferences(path, lambda: NOW)
    assert again.suggest(plans) is None and again.list()["preferences"] == []
    review = again.preview(offered)
    assert again.save(offered, review["review_token"], "affirmative")["status"] == "saved"
    assert again.list()["preferences"] == [offered["preference"]]


def test_unreadable_preferences_are_preserved(tmp_path):
    path = tmp_path / "travel-preferences.json"
    path.write_text("broken")
    prefs = TravelPreferences(path, lambda: NOW)
    assert prefs.preview(SAVE)["status"] == "retry"
    assert prefs.save(SAVE, "token", "request")["status"] == "retry"
    assert path.read_text() == "broken"
