"""End-to-end on a real board. Needs kicad-cli and network (first run fetches the fixture)."""
import shutil
import subprocess

import pytest

import fixtures
from kicadverify import config
from kicadverify.cli import analyse

pytestmark = pytest.mark.skipif(
    subprocess.run([config.KICAD_CLI, "--version"], capture_output=True).returncode != 0
    if shutil.which(config.KICAD_CLI) or config.KICAD_CLI.endswith(".exe") else True,
    reason="kicad-cli not available")


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    d = fixtures.copy_of(fixtures.rele_board(), tmp_path_factory.mktemp("rele") / "rele")
    config.init_project(d)
    return d


def test_known_good_board_has_no_fail(board):
    rep, _ = analyse(board, "fast")
    fails = [r for r in rep["results"] if r["status"] == "FAIL"]
    assert not fails, fails


def test_reversed_led_is_caught(board, tmp_path):
    d = fixtures.copy_of(board, tmp_path / "led")
    pcb = d / "rele-esp12f.kicad_pcb"
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent / "seeded"))
    import run_seeded
    assert run_seeded.apply(d, run_seeded.MUTATIONS["led_D2_reversed"][0])
    rep, _ = analyse(d, "fast")
    st = {r["check"]: r["status"] for r in rep["results"]}
    assert st["CIR-POL-001"] == "FAIL"
    assert pcb.exists()
