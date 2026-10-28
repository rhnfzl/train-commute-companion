"""Release acceptance, spec 10.4 MCP row ("MCP reads agree with `/status`."): every replay runs
through the runtime, the Bot answering Telegram `/status`, `mcp_answer`, the Unix socket and the
SDK client, and after every 30 s step each tool's answer agrees with `/status`. It imports the SDK
directly, so a run without the `app` extra fails instead of skipping this gate."""

import pytest
from conftest import build_from_replay, drive_replay
from mcp import Client
from tcc_app.commands import NO_WORKS, REFUSAL_HIDDEN
from tcc_app.main import mcp_answer
from tcc_app.mcp_server import build as build_server
from tcc_app.web import start_readout
from test_acceptance import REPLAYS, msg

ALL_REPLAYS = sorted(p.stem for p in REPLAYS.glob("*.yaml"))  # the single- and two-profile replays

WARNINGS = ("  NS data: not fresh", "  NS data: fresh, last poll failed", "Planned-works check:",
            "Profile file refused: ", "Your NS key was rejected.", "State cannot be saved",
            "Delivery not confirmed yet")  # fmt: skip


def _is_warning(line: str) -> bool:
    return (
        line.startswith(WARNINGS)
        or line.endswith(" alerts not delivered yet.")
        or ("NS moved your" in line)
    )


@pytest.mark.parametrize("name", ALL_REPLAYS)
async def test_mcp_reads_agree_with_status(tmp_path, tg, sock, name):  # 10.4
    app, rp = build_from_replay(tmp_path, tg, REPLAYS / f"{name}.yaml")
    server = await start_readout(sock, mcp_answer(app))
    checks = 0
    hidden = f"Profile file refused: {REFUSAL_HIDDEN}"  # MCP never echoes the raw refusal (Task 6)
    async with Client(build_server(sock)) as c:

        async def check() -> None:
            nonlocal checks
            before = len(tg.sent)
            await app.bot.handle(msg(app, "/status", 1000 + checks))
            status = "\n".join(m["text"] for m in tg.sent[before:])  # every part split_text sent
            shown = [
                hidden if x.startswith("Profile file refused: ") else x for x in status.splitlines()
            ]
            lines = set(shown)
            pids = [p.id for p in app.rt.view().profiles]
            for pid in pids:
                st = (await c.call_tool("get_status", {"profile": pid})).structured_content
                warns = {x for x in st["text"].splitlines() if _is_warning(x)}
                if len(pids) == 1:
                    assert st["text"].splitlines() == shown
                else:  # one profile's status is a part of the whole
                    assert set(st["text"].splitlines()) <= lines | {
                        "No journey is being followed now."
                    }
                for d in ("outbound", "return"):
                    nc = (
                        await c.call_tool("get_next_connections", {"profile": pid, "direction": d})
                    ).structured_content
                    assert warns <= set(nc["text"].splitlines())  # every warning, not a subset
                    assert set(nc["text"].splitlines()) <= lines | {
                        "No journey is being followed now.",
                        hidden,
                    }
                    for j in nc["journeys"]:
                        if (g := j["next_good"]) is not None:
                            assert (
                                f"  next good connection: {g['departure']} from {g['from']}"
                                in lines
                            )
                wa = (await c.call_tool("get_works_ahead", {"profile": pid})).structured_content
                assert warns <= set(wa["text"].splitlines())
                assert set(wa["text"].splitlines()) <= lines | {
                    NO_WORKS,
                    "Planned-works check: not run yet",
                    hidden,
                }
            checks += 1

        await drive_replay(app, rp, each=check)
    server.close()
    assert checks > 0
