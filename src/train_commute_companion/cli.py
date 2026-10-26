"""`tcc`: check a profiles file, replay a scripted day, or follow the real journey (9, step 1)."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, time
from pathlib import Path

import httpx

from train_commute_companion.calendar import TZ
from train_commute_companion.model import AlertEvent, Direction, Profile
from train_commute_companion.profile import load_config
from train_commute_companion.providers.base import ProviderError
from train_commute_companion.providers.ns import NsProvider, map_stations
from train_commute_companion.replay import ATTRIBUTION, ReplayError, format_event, replay_lines
from train_commute_companion.runtime import Runtime, SystemClock
from train_commute_companion.serde import decode
from train_commute_companion.store import JsonFileStore, PersistedState

_DST_HOUR = 2  # the hour that is repeated or skipped on the clock-change nights (5.2)


def _times(p: Profile) -> list[tuple[str, time]]:
    out: list[tuple[str, time]] = []
    for d in Direction:
        j = p.journey(d)
        out.append((f"{d.value}.first_train", j.first_train))
        out += [(f"{d.value}.window[{i}]", t) for i, t in enumerate(j.window)]
    if p.alerts.latest_acceptable_arrival:
        out.append(("alerts.latest_acceptable_arrival", p.alerts.latest_acceptable_arrival))
    out += [(f"alerts.quiet_hours[{i}]", t) for i, t in enumerate(p.alerts.quiet_hours or ())]
    return out


def _moved(path: Path, profiles: Sequence[Profile]) -> list[str]:
    """Read-only: check-profile never moves a damaged store aside the way a running app does."""
    try:
        state = decode(PersistedState, json.loads(path.read_text(encoding="utf-8"))["state"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"store: cannot read {path.name} ({type(e).__name__}); NS changes not checked"]
    lines = []
    for p in profiles:
        for d in Direction:
            hist = state.baselines.get(f"{p.id}|{d.value}")
            confirmed = [v for v in (hist.versions if hist else ()) if v.confirmed]
            if not confirmed:
                continue
            v = max(confirmed, key=lambda x: (x.effective_from, x.version))
            adopted, mine = v.signature.legs[0].departure, p.journey(d).first_train
            if adopted != mine:
                lines.append(
                    f"{p.name}.{d.value}: NS moved your {mine:%H:%M} to {adopted:%H:%M} "
                    f"from {v.effective_from.isoformat()}; update your profile?"
                )
    return lines


def check_profile(args: argparse.Namespace) -> int:
    try:
        text = Path(args.path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"{args.path}: cannot read ({e})")
        return 1
    known = None
    if args.stations_file:
        try:
            known = map_stations(json.loads(Path(args.stations_file).read_text(encoding="utf-8")))
        except (OSError, ValueError, ProviderError) as err:  # never pass silently
            print(f"{args.stations_file}: cannot read station list ({err})")
            return 1
    cfg, errors = load_config(text, known, None, datetime.now(TZ).date())
    for pe in errors:
        print(f"{pe.path}: {pe.message}" + (f" ({pe.suggestion})" if pe.suggestion else ""))
    profiles = cfg.profiles if cfg else ()
    for p in profiles:
        for where, t in _times(p):
            if t.hour == _DST_HOUR:
                print(
                    f"warning: {p.name}.{where}: {t:%H:%M} is between {_DST_HOUR:02d}:00 and {_DST_HOUR + 1:02d}:00, "
                    "which is repeated or skipped on the clock-change nights"
                )
    if args.store:
        for line in _moved(Path(args.store), profiles):
            print(line)
    return 1 if errors else 0


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
        print(format_event(event), flush=True)
        return True


async def _run(args: argparse.Namespace, key: str) -> None:
    async with httpx.AsyncClient() as client:
        provider = NsProvider(key, client, lambda: datetime.now(TZ))
        try:
            known = await provider.stations()
        except ProviderError:  # without the list codes go unchecked; polling still works
            known = None
        rt = Runtime(
            profile_path=Path(args.path),
            store=JsonFileStore(Path(args.store)),
            provider=provider,
            notifier=_StdoutNotifier(),
            clock=SystemClock(),
            known_stations=known,
        )
        print(ATTRIBUTION, flush=True)
        await rt.start()
        await rt.run_forever()


def run(args: argparse.Namespace) -> int:
    if not os.environ.get("NS_API_KEY", "").strip():
        print("NS_API_KEY is not set; refusing to start")
        return 1
    try:
        asyncio.run(_run(args, os.environ["NS_API_KEY"].strip()))
    except KeyboardInterrupt:
        pass
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
