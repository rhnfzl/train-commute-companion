"""An app with location following on, and the OwnTracks requests sent to it."""

import asyncio
import base64
from pathlib import Path

from app_rig import CHAT, CODES, TOKEN, StationsProvider, TickClock
from builders import ONE_TRANSFER
from runtime_rig import PROFILES_DIR, Provider, at
from tcc_app.main import App, Settings, build

SECRET = "test-owntracks-secret-0001"


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


async def post(port: int, body: bytes, auth: str | None = None,
               length: int | None = None) -> tuple[int, bytes, bytes]:  # fmt: skip
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = (
        f"POST /owntracks HTTP/1.1\r\nContent-Length: {len(body) if length is None else length}\r\n"
    )
    if auth is not None:
        head += f"Authorization: {auth}\r\n"
    writer.write(head.encode() + b"\r\n" + body)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    top, _, payload = answer.partition(b"\r\n\r\n")
    return int(top.split(b" ")[1]), top, payload


POSITIONS = {"AAA": (52.3, 4.9), "UT": (52.1, 5.1), "RTD": (51.0, 4.0)}


FAR = (53.0, 6.0)


UT = (52.1, 5.1)


async def located_app(
    tmp_path: Path, tg, name: str, positions=POSITIONS, now: str = "08:00"
) -> App:
    clock = TickClock(at(now))
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text((PROFILES_DIR / f"{name}.yaml").read_text())
    s = Settings("k", TOKEN, CHAT, None, profiles, tmp_path)
    provider = StationsProvider(Provider(clock, list(ONE_TRANSFER)), CODES, positions)
    app = build(s, clock=clock, provider=provider, client=tg.client())
    app.clock = clock  # type: ignore[attr-defined]
    await app.rt.start()
    return app
