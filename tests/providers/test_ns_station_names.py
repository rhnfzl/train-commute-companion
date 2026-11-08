import pytest

from train_commute_companion.providers.base import MalformedAnswer
from train_commute_companion.providers.ns import map_station_names


def test_station_catalogue_uses_long_names_with_code_fallback():
    assert map_station_names(
        {
            "payload": [
                {"code": "UT", "namen": {"lang": "Utrecht Centraal"}},
                {"stationCode": "RTD", "namen": {}},
            ]
        }
    ) == {"UT": "Utrecht Centraal", "RTD": "RTD"}


@pytest.mark.parametrize("payload", [{}, {"payload": []}])
def test_station_catalogue_refuses_missing_or_empty_station_list(payload):
    with pytest.raises(MalformedAnswer):
        map_station_names(payload)
