import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from app_rig import FakeTelegram, TickClock, make_app
from owntracks_rig import located_app
from runtime_rig import FakeClock, at
from tcc_app.main import App, mcp_answer
from tcc_app.web import start_readout


@pytest.fixture
def sock() -> Iterator[Path]:
    """A short socket path: macOS caps AF_UNIX paths near 104 bytes, too short for tmp_path."""
    d = Path(tempfile.mkdtemp(None, "tcc", "/tmp"))  # suffix, prefix, parent
    yield d / "mcp.sock"
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def served(tmp_path: Path, tg: FakeTelegram, sock: Path) -> AsyncIterator[App]:
    """A started app on the fake NS, its read-out on `sock`."""
    app = make_app(tmp_path, tg, TickClock(at("07:50")))
    await app.rt.start()
    server = await start_readout(sock, mcp_answer(app))
    assert server is not None
    yield app
    server.close()
    await server.wait_closed()


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    fake = FakeTelegram()
    yield fake
    await fake.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(at("07:50"))


@pytest.fixture
async def app_owntracks(tmp_path, tg):
    a = await located_app(tmp_path, tg, "located_via_ut_owntracks", now="07:40")
    yield a
    await a.follower.close()
