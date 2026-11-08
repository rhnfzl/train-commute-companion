from datetime import timedelta

import pytest

from train_commute_companion.serde import decode, dumps, encode, loads


@pytest.mark.parametrize(
    "duration",
    [
        timedelta(),
        timedelta(microseconds=1),
        timedelta(microseconds=-1),
        timedelta(days=999999999, microseconds=999999),
    ],
)
def test_timedelta_roundtrip_uses_exact_integer_microseconds(duration):
    value = encode(duration)
    assert type(value) is int
    assert loads(timedelta, dumps(duration)) == duration


@pytest.mark.parametrize("value", [True, 1.5, "1000", None, {}, 10**100])
def test_timedelta_rejects_noninteger_and_overflow_values(value):
    with pytest.raises(ValueError):
        decode(timedelta, value)
