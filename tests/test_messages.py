from builders import make_leg

from train_commute_companion.messages import all_good_text, data_unclear_text


def test_change_journey_is_never_direct_and_ok_says_per_ns():
    l1, l2 = make_leg("AAA", "UT", "08:12", "08:40"), make_leg("UT", "RTD", "08:50", "09:24")
    t = all_good_text([l1, l2])
    assert "on time per NS" in t and "change at UT" in t and "direct" not in t


def test_every_transfer_station_is_named_in_order():
    l1 = make_leg("AAA", "UT", "08:12", "08:40")
    l2 = make_leg("UT", "BBB", "08:50", "09:05", number="2")
    l3 = make_leg("BBB", "RTD", "09:15", "09:30", number="3")
    assert "change at UT then BBB" in all_good_text([l1, l2, l3])


def test_data_unclear_lists_each_copy():
    t = data_unclear_text(
        "IC 1234", "Utrecht Centraal", ("arrives 08:40 (planned only)", "arrives 08:46 (live)")
    )
    assert t == (
        "NS data unclear for IC 1234 at Utrecht Centraal. Copy 1: arrives 08:40"
        " (planned only). Copy 2: arrives 08:46 (live). Check the NS app or the station boards."
    )


def test_data_unclear_without_facts_still_reads():
    assert data_unclear_text("", "", ()) == (
        "NS data unclear for your train. Check the NS app or the station boards."
    )
