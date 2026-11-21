"""Versioned wire format for a Home Assistant client of the Docker runtime."""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from train_commute_companion.runtime import Runtime
from train_commute_companion.serde import decode, encode

PATH = "/home-assistant/v1"
VERSION = 1

_METHODS: dict[str, Callable[..., Any]] = {
    method.__name__: method
    for method in (
        Runtime.view,
        Runtime.stations,
        Runtime.preview_trip,
        Runtime.choose_at,
        Runtime.leaving,
        Runtime.travelling,
        Runtime.not_travelling,
        Runtime.answer_prompt,
        Runtime.check_trip,
        Runtime.forget_station,
        Runtime.clear_commute_history,
        Runtime.arrival_records,
        Runtime.confirm_arrival,
        Runtime.answer_arrival_prompt,
        Runtime.prepare_recovery,
        Runtime.select_recovery,
        Runtime.confirm_recovery_step,
        Runtime.stop_recovery,
        Runtime.report_location,
        Runtime.confirm_departure_presence,
        Runtime.sync_profile,
    )
}


class InvalidCommand(ValueError):
    """A rejected bridge request, before it reaches the runtime."""


def _result_types(annotation: Any) -> tuple[Any, ...]:
    return get_args(annotation) if get_origin(annotation) in (Union, UnionType) else (annotation,)


def _kind(annotation: Any) -> str:
    origin = get_origin(annotation)
    return "literal" if origin is Literal else str((origin or annotation).__name__)


def _matches(result: object, annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Literal:
        return result in get_args(annotation)
    return isinstance(result, origin or annotation)


def request(method: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"version": VERSION, "method": method, "arguments": encode(arguments)}


def _prepare(
    message: object,
) -> tuple[Callable[..., Any], dict[str, Any], tuple[Any, ...]]:
    if (
        not isinstance(message, dict)
        or set(message) != {"version", "method", "arguments"}
        or type(message["version"]) is not int
        or message["version"] != VERSION
        or not isinstance(name := message["method"], str)
        or (method := _METHODS.get(name)) is None
        or not isinstance(arguments := message["arguments"], dict)
    ):
        raise InvalidCommand("Invalid Home Assistant bridge command.")
    hints = get_type_hints(method)
    try:
        inspect.signature(method).bind(None, **arguments)
        supplied = {key: decode(hints[key], value) for key, value in arguments.items()}
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise InvalidCommand("Invalid Home Assistant bridge arguments.") from error
    return method, supplied, _result_types(hints["return"])


async def dispatch(runtime: Runtime, message: object) -> dict[str, Any]:
    method, arguments, types = _prepare(message)
    result = method(runtime, **arguments)
    if isinstance(result, Awaitable):
        result = await result
    result_type = next(tp for tp in types if _matches(result, tp))
    return {"version": VERSION, "kind": _kind(result_type), "result": encode(result)}


def read_result(method: str, message: object) -> Any:
    if (
        not isinstance(message, dict)
        or type(message.get("version")) is not int
        or message["version"] != VERSION
        or "result" not in message
        or not isinstance(kind := message.get("kind"), str)
    ):
        raise ValueError("Incompatible Home Assistant bridge response.")
    kinds = {_kind(tp): tp for tp in _result_types(get_type_hints(_METHODS[method])["return"])}
    variant = kinds.get(kind)
    if variant is None:
        raise ValueError("Unexpected Home Assistant bridge result type.")
    result = message["result"]
    # The trip check already returns a JSON dictionary.
    if get_origin(variant) is dict and isinstance(result, dict):
        return result
    return decode(variant, result)
