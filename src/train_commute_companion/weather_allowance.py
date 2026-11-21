"""Suggest extra time to reach the station without changing the followed journey."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from train_commute_companion.calendar import hhmm, to_utc
from train_commute_companion.catalogue import MessageLanguage, message
from train_commute_companion.providers.weather import FORECAST_TTL, WeatherView


@dataclass(frozen=True, slots=True)
class WeatherAllowance:
    extra_min: int
    hazard: Literal["rain", "fog", "snow", "ice"]
    suggested_leave_by: datetime

    @property
    def text(self) -> str:
        return self.format("en")

    def format(self, language: MessageLanguage) -> str:
        return message(
            "leave_by.weather",
            language,
            leave=message("leave_by", language, time=hhmm(self.suggested_leave_by)),
            minutes=self.extra_min,
            hazard=message(f"weather.{self.hazard}", language),
        )


def weather_allowance(
    base: datetime,
    buffer_min: int,
    weather: WeatherView,
    now: datetime,
    *,
    elapsed_grace: timedelta = timedelta(),
) -> WeatherAllowance | None:
    """The strongest forecast hazard wins. This table is a planning heuristic, not calibration."""
    hour = weather.forecast
    if (
        buffer_min <= 0
        or hour is None
        or weather.fetched_at is None
        or not timedelta(0) <= to_utc(now) - to_utc(weather.fetched_at) < FORECAST_TTL
        or abs(to_utc(hour.at) - to_utc(weather.when)) > timedelta(minutes=30)
    ):
        return None
    band = 0 if buffer_min <= 10 else 1 if buffer_min <= 20 else 2
    hazard: Literal["rain", "fog", "snow", "ice"]
    if hour.weather_code in (56, 57, 66, 67) or (
        hour.temperature_c <= 1 and hour.precipitation_mm >= 0.2
    ):
        hazard, minutes = "ice", (5, 10, 15)
    elif hour.snowfall_cm > 0 or hour.weather_code in (71, 73, 75, 77, 85, 86):
        hazard, minutes = "snow", (5, 8, 10)
    elif hour.precipitation_mm >= 0.2:
        hazard, minutes = "rain", (2, 3, 5)
    elif hour.weather_code in (45, 48):
        hazard, minutes = "fog", (2, 3, 5)
    else:
        return None
    extra = minutes[band]
    suggested = to_utc(base) - timedelta(minutes=extra)
    return (
        None
        if suggested + elapsed_grace < to_utc(now)
        else WeatherAllowance(extra, hazard, suggested)
    )
