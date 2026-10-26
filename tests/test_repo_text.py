import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".txt"}


def tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / f for f in out.stdout.split("\n") if f and (ROOT / f).suffix in TEXT]


def test_no_em_dash_in_repo():
    hits = [p for p in tracked_text_files() if chr(0x2014) in p.read_text(encoding="utf-8")]
    assert hits == []


def test_no_station_literals_in_src():
    # station codes appear only in tests and docs
    for p in [*(ROOT / "src").rglob("*.py"), *(ROOT / "custom_components").rglob("*.py")]:
        assert not re.search(r'"(UT|RTD|AAA|BBB)"', p.read_text()), p
