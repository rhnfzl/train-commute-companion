"""`tcc`: check a profiles file, replay a scripted day, or follow the real journey (9, step 1)."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import httpx

from train_commute_companion.calendar import TZ
from train_commute_companion.model import AlertEvent, Profile
from train_commute_companion.profile import check_lines
from train_commute_companion.providers.base import ProviderError
from train_commute_companion.providers.ns import NsProvider, map_stations
from train_commute_companion.replay import ATTRIBUTION, ReplayError, format_event, replay_lines
from train_commute_companion.runtime import Runtime, SystemClock
from train_commute_companion.serde import decode
from train_commute_companion.store import JsonFileStore, PersistedState
from train_commute_companion.view import moved


def _moved(path: Path, profiles: Sequence[Profile]) -> list[str]:
    """Read-only: check-profile never moves a damaged store aside the way a running app does."""
    try:
        state = decode(PersistedState, json.loads(path.read_text(encoding="utf-8"))["state"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"store: cannot read {path.name} ({type(e).__name__}); NS changes not checked"]
    return [m.text for m in moved(profiles, state.baselines)]


def check_profile(args: argparse.Namespace) -> int:
    try:
        text = Path(args.path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"{args.path}: cannot read ({e})")
        return 1
    got: frozenset[str] | str = ""
    known: frozenset[str] | None
    if args.stations_file:
        try:
            known = map_stations(json.loads(Path(args.stations_file).read_text(encoding="utf-8")))
        except (OSError, ValueError, ProviderError) as err:  # never pass silently
            print(f"{args.stations_file}: cannot read station list ({err})")
            return 1
    else:
        got = asyncio.run(_ns_stations())
        known = None if isinstance(got, str) else got
    cfg, lines = check_lines(text, known, datetime.now(TZ).date())
    for line in lines:
        print(line)
    profiles = cfg.profiles if cfg else ()
    if args.store:
        for line in _moved(Path(args.store), profiles):
            print(line)
    if known is None:  # 5.3: a file whose codes were not checked is never reported valid
        print(f"station codes not checked: {got}")
        return 1
    # error paths start with "file" or "profiles", never "warning: "
    return 1 if any(not x.startswith("warning: ") for x in lines) else 0


async def _ns_stations() -> frozenset[str] | str:
    """The NS station list with NS_API_KEY, or why it is not available."""
    key = os.environ.get("NS_API_KEY", "").strip()
    if not key:
        return "pass --stations-file or set NS_API_KEY"
    async with httpx.AsyncClient() as client:
        try:
            return await NsProvider(key, client, lambda: datetime.now(TZ)).stations()
        except ProviderError as e:
            return f"the NS station list is unavailable ({e}); pass --stations-file"


def _station_list(cache: Path, fresh: frozenset[str] | None) -> frozenset[str] | None:
    """The fresh NS list, saved as the validated copy; else the last saved copy (5.3)."""
    if fresh is not None:
        try:
            cache.write_text(json.dumps(sorted(fresh)), encoding="utf-8")
        except OSError as e:
            print(f"warning: cannot save the station list to {cache} ({e})", flush=True)
        return fresh
    try:
        codes = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(codes, list) or not codes or not all(isinstance(c, str) for c in codes):
        return None
    return frozenset(codes)


def replay(args: argparse.Namespace) -> int:
    try:
        lines = asyncio.run(replay_lines(Path(args.file)))
    except ReplayError as e:
        print(f"replay: {e}")
        return 1
    print("\n".join(lines))
    return 0


class _StdoutNotifier:
    async def send(self, event: AlertEvent) -> bool:
        print(
            f"{format_event(event)}\n{ATTRIBUTION}", flush=True
        )  # NS attribution on every status output
        return True


async def _run(args: argparse.Namespace, key: str) -> int:
    async with httpx.AsyncClient() as client:
        provider = NsProvider(key, client, lambda: datetime.now(TZ))
        rt = Runtime(
            profile_path=Path(args.path),
            store=JsonFileStore(Path(args.store)),
            provider=provider,
            notifier=_StdoutNotifier(),
            clock=SystemClock(),
            known_stations=None,
        )
        # through the runtime's request budget (rule 14); never accept codes unchecked (5.3)
        cache = Path(args.store).with_suffix(".stations.json")
        known = _station_list(cache, await rt.stations())
        rt.require_stations = True
        rt.on_stations = lambda codes: _station_list(cache, codes)  # saves the fresh list
        if known is None:  # 5.2: the saved configuration still runs; edits wait for the list
            print(
                f"warning: the NS station list is unavailable and {cache} holds no saved copy; "
                "running the last valid configuration, profile changes wait until station "
                "codes can be checked",
                flush=True,
            )
        rt.known_stations = known
        print(ATTRIBUTION, flush=True)
        await rt.run_forever()
    return 0


def run(args: argparse.Namespace) -> int:
    if not os.environ.get("NS_API_KEY", "").strip():
        print("NS_API_KEY is not set; refusing to start")
        return 1
    try:
        return asyncio.run(_run(args, os.environ["NS_API_KEY"].strip()))
    except KeyboardInterrupt:
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tcc")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check-profile", help="validate a profiles file")
    c.add_argument("path")
    c.add_argument("--stations-file")
    c.add_argument("--store")
    c.set_defaults(fn=check_profile)
    r = sub.add_parser("replay", help="run a scripted day on a fake clock")
    r.add_argument("file")
    r.set_defaults(fn=replay)
    n = sub.add_parser("run", help="follow the real journey (needs NS_API_KEY)")
    n.add_argument("path")
    n.add_argument("--store", default="./tcc-state.json")
    n.set_defaults(fn=run)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:  # argparse exits; main returns
        return e.code if isinstance(e.code, int) else 2
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
