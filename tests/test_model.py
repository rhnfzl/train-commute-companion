from datetime import date

from builders import make_connection, make_leg


def test_connection_transfer_stations():
    c = make_connection(
        [make_leg("AAA", "UT", "08:12", "08:40"), make_leg("UT", "RTD", "08:46", "09:24")]
    )
    assert c.transfer_stations == ("UT",)
    assert (c.origin, c.destination) == ("AAA", "RTD")


def test_train_id_equality_is_by_value():
    a = make_leg("AAA", "UT", "08:12", "08:40", number="3150").train
    assert a == make_leg("AAA", "UT", "08:12", "08:40", number="3150").train


def test_time_before_4am_rolls_to_next_day():
    leg = make_leg("UT", "RTD", "23:40", "00:05")
    assert leg.alight.planned_arrival.date() == date(2026, 10, 28)


def test_train_ids_in_the_repeated_autumn_hour_are_distinct():
    from datetime import datetime

    from train_commute_companion.calendar import TZ
    from train_commute_companion.model import TrainId

    day = date(2026, 10, 25)
    first = TrainId(day, "3150", datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=0))
    second = TrainId(day, "3150", datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=1))
    assert first != second and len({first, second}) == 2
    same = TrainId(day, "3150", first.planned_departure.astimezone(TZ))
    assert same == first and hash(same) == hash(first)
