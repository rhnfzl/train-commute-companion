"""Dataclass <-> JSON for every persisted type. Decoding is driven by the type hints, so a field
added with a default loads from a file written before it existed (a missing key takes the default)."""

import json
from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from types import NoneType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from train_commute_companion.calendar import TZ


def encode(obj: object) -> Any:
    if obj is None or isinstance(obj, bool | int | float | str) and not isinstance(obj, Enum):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, date | time):  # datetime is a date; isoformat keeps the offset (and fold)
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: encode(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): encode(v) for k, v in obj.items()}
    if isinstance(obj, frozenset | set):
        return sorted((encode(x) for x in obj), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(obj, tuple | list):
        return [encode(x) for x in obj]
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def _bad(tp: object, v: object) -> ValueError:
    return ValueError(f"expected {tp}, got {v!r}")


def decode(tp: Any, v: Any) -> Any:
    origin, args = get_origin(tp), get_args(tp)
    if tp is NoneType:
        if v is not None:
            raise _bad(tp, v)
        return None
    if origin is Union or origin is UnionType:
        if v is None and NoneType in args:
            return None
        rest = [a for a in args if a is not NoneType]
        if len(rest) != 1:
            raise TypeError(f"ambiguous union {tp}")
        return decode(rest[0], v)
    if origin is Literal:
        if v not in args:
            raise _bad(tp, v)
        return v
    if origin is tuple:
        if not isinstance(v, list):
            raise _bad(tp, v)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(decode(args[0], x) for x in v)
        if len(args) != len(v):
            raise _bad(tp, v)
        return tuple(decode(a, x) for a, x in zip(args, v, strict=True))
    if origin is frozenset:
        if not isinstance(v, list):
            raise _bad(tp, v)
        return frozenset(decode(args[0], x) for x in v)
    if origin is not None and isinstance(origin, type) and issubclass(origin, Mapping):
        if not isinstance(v, dict):
            raise _bad(tp, v)
        key = int if args[0] is int else (lambda k: decode(args[0], k))
        return {key(k): decode(args[1], x) for k, x in v.items()}
    if isinstance(tp, type):
        if issubclass(tp, Enum):
            return tp(v)
        if tp is datetime:
            if not isinstance(v, str):
                raise _bad(tp, v)
            d = datetime.fromisoformat(v)
            if d.tzinfo is None:
                raise ValueError(f"naive datetime {v!r}")
            return d.astimezone(TZ)
        if tp is date:
            return date.fromisoformat(v)
        if tp is time:
            return time.fromisoformat(v)
        if is_dataclass(tp):
            return _decode_dc(tp, v)
        if tp is int:
            if isinstance(v, bool) or not isinstance(v, int):
                raise _bad(tp, v)
            return v
        if tp is float:
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise _bad(tp, v)
            return float(v)
        if tp in (str, bool):
            if not isinstance(v, tp):
                raise _bad(tp, v)
            return v
    raise TypeError(f"cannot decode {tp}")


def _decode_dc(cls: type, v: Any) -> Any:
    if not isinstance(v, dict):
        raise _bad(cls, v)
    hints = get_type_hints(cls)
    known = {f.name: f for f in fields(cls)}
    if extra := v.keys() - known.keys():
        raise ValueError(f"{cls.__name__}: unknown field {min(extra)!r}")
    kw = {}
    for name, f in known.items():
        if name in v:
            kw[name] = decode(hints[name], v[name])
        elif f.default is MISSING and f.default_factory is MISSING:
            raise ValueError(f"{cls.__name__}: missing field {name!r}")
    return cls(**kw)


def dumps(obj: object) -> str:
    return json.dumps(encode(obj), sort_keys=True)


def loads[T](cls: type[T], text: str) -> T:
    return decode(cls, json.loads(text))  # type: ignore[no-any-return]
