"""Every prior-art edge-case row is mapped, and every mapped test exists."""

import subprocess
import sys
from functools import cache
from pathlib import Path

from ROWS import PORTED


@cache
def _collected(root: Path) -> frozenset[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return frozenset(line for line in out.splitlines() if "::" in line)


def collected_node_ids(pytestconfig) -> frozenset[str]:
    return _collected(Path(pytestconfig.rootpath))


def test_every_prior_art_row_is_mapped():
    expected = (
        {f"board:{i}" for i in range(1, 85)}
        | {f"lifecycle:{i}" for i in range(1, 61)}
        | {f"lookahead:{i}" for i in range(1, 34)}
    )
    assert set(PORTED) == expected


def test_every_mapped_test_exists(pytestconfig):
    ids = collected_node_ids(pytestconfig)
    for row, target in PORTED.items():
        assert target.startswith(("merged:", "overridden:")) or target in ids, row


def test_merged_rows_point_at_a_mapped_row():
    for row, target in PORTED.items():
        if target.startswith("merged:"):
            other = target.removeprefix("merged:")
            assert other in PORTED and other != row and not PORTED[other].startswith("merged:"), row
