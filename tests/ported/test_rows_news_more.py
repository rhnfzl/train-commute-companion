"""Board row 23: a delay that grows by a full step after being announced is news again."""

from test_news import key, keyed, late

from train_commute_companion.news import announced


def test_plus_5_to_plus_8_is_news_and_plus_5_to_plus_6_is_not():
    k5, said5 = keyed(late(5))
    assert announced(5, 8) == 8
    assert key(late(8), last=said5) != k5
    assert key(late(6), last=said5) == k5
