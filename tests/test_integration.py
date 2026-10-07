"""End-to-end on a real board. Needs kicad-cli and network (first run fetches the fixture).

KICAD_CLI may point to a wrapper that runs kicad-cli from the official image (see the CI template
written by `kicadverify init --ci github`)."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import fixtures
from kicadverify import cli, config
from kicadverify.cli import analyse


def _kicad_ok():
    exe = config.KICAD_CLI
    if not (shutil.which(exe) or os.path.isfile(exe)):
        return False
    try:
        return subprocess.run([exe, "--version"], capture_output=True, timeout=120).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _kicad_ok(), reason="kicad-cli not available")
REFERENCE = Path(__file__).parent / "reference" / "rele" / "verification"
sys.path.insert(0, str(Path(__file__).parent / "seeded"))
import run_seeded  # noqa: E402


def _configured(dst):
    d = fixtures.copy_of(fixtures.rele_board(), dst)
    shutil.copytree(REFERENCE, d / "verification")
    return d


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    return _configured(tmp_path_factory.mktemp("rele") / "rele")


def _status(rep):
    return {v["id"]: v["status"] for v in rep["verification"]["requirements"]}


def test_unconfigured_board_is_not_ready(tmp_path):
    """Without project data the framework says what it could not verify instead of passing."""
    d = fixtures.copy_of(fixtures.rele_board(), tmp_path / "raw")
    config.init_project(d)
    rep, _ = analyse(d, "fast")
    st = _status(rep)
    assert st["PCB-PINS-001"] == st["FAB-DFM-001"] == "NOT_VERIFIABLE"
    assert st["PCB-ERC-001"] == "NOT_RUN"  # fast mode
    assert not rep["verification"]["gates"]["fab"]["pass"]


def test_reference_board_passes_fab_gate(board):
    rep, _ = analyse(board, "full")
    st = _status(rep)
    assert rep["verification"]["gates"]["fab"]["pass"], rep["verification"]["gates"]["fab"]["blockers"]
    for rid in ("PCB-DRC-001", "PCB-PARITY-001", "CIR-POL-001", "FAB-RULES-001", "FAB-DFM-001", "FAB-STALE-001",
                "FAB-DRILL-001", "FAB-CPL-001", "PRJ-PWR-001", "PRJ-MAINS-001"):
        assert st[rid] == "VERIFIED", (rid, st[rid])
    assert st["MOD-PINOUT-001"] == "NOT_RUN" and not rep["verification"]["gates"]["release"]["pass"]
    # library drift and off-grid ends are reported apart and need no waiver; ERC itself is clean
    assert st["PCB-ERC-001"] == "VERIFIED"
    hyg = next(r for r in rep["results"] if r["check"] == "PCB-HYGIENE-001" and "ERC" in r["detail"])
    assert hyg["status"] == "WARN" and hyg["violations"]
    assert not any(r["check"] == "WAIVERS" for r in rep["results"])  # no stale waiver left
    stale = next(r for r in rep["results"] if r["check"] == "FAB-STALE-001")
    assert "2 Gerber sets compared" in stale["detail"]  # fabrication/gerbers and gerbers_JLCPCB.zip
    assert (board / "verification" / "pcb" / "reports" / "evidence" / "rele-esp12f.drc.json").exists()
    assert cli.main(["verify", str(board), "--gate", "fab", "--fast"]) == 1  # fast mode cannot clear fab


@pytest.mark.parametrize("mutation,req", [("led_D2_reversed", "CIR-POL-001"),
                                          ("rules_clearance_relaxed", "FAB-RULES-001"),
                                          ("gnd_near_mains_no_dru_rule", "ISO-SEP-001")])
def test_seeded_defect_blocks_the_gate(board, tmp_path, mutation, req):
    d = fixtures.copy_of(board, tmp_path / mutation)
    shutil.copytree(REFERENCE, d / "verification")
    assert run_seeded.apply(d, run_seeded.MUTATIONS[mutation][0])
    rep, _ = analyse(d, "full")
    assert _status(rep)[req] == "FAILED"
    assert not rep["verification"]["gates"]["fab"]["pass"]


def test_stale_check_ignores_the_last_plot_settings(board, tmp_path):
    """The board's stored plot settings are those of the last plot. After a PDF plot of the copper
    layers only, re-plotting with them wrote PDF files under .gbr names and nothing was compared."""
    from kicadverify.checks import board as board_mod, fab
    d = fixtures.copy_of(board, tmp_path / "pdf_last")
    pcb = d / "rele-esp12f.kicad_pcb"
    t = pcb.read_text(encoding="utf-8")
    t = t.replace("(outputformat 1)", "(outputformat 4)", 1)
    t = re.sub(r"\(layerselection [^)]*\)", "(layerselection 0x00000000_00000000_00000000_00000001)", t, count=1)
    pcb.write_text(t, encoding="utf-8")
    b = board_mod.load(pcb)
    files, _, _, _ = fab.find_outputs(d, {})
    stale = {r.check_id: r for r in fab.gerbers(pcb, b, files, "t")}["FAB-STALE-001"]
    assert stale.status == "PASS", stale.detail
    assert stale.coverage["total"] >= 5 and not stale.coverage["unchecked"]


def test_stale_archive_next_to_fresh_gerbers(board, tmp_path):
    """The loose Gerbers were re-exported after a change; the zip uploaded to the fab was not."""
    d = fixtures.copy_of(board, tmp_path / "stale_zip")
    shutil.copytree(REFERENCE, d / "verification", dirs_exist_ok=True)
    assert run_seeded.apply(d, run_seeded.MUTATIONS["gerbers_stale"][0])
    out = d / "fabrication" / "gerbers"
    for f in out.iterdir():
        if f.suffix.lower() in (".gbr", ".gbrjob") or f.suffix.lower().startswith(".g"):
            f.unlink()
    subprocess.run([config.KICAD_CLI, "pcb", "export", "gerbers", "-o", str(out), str(d / "rele-esp12f.kicad_pcb")],
                   check=True, capture_output=True, timeout=300)
    rep, _ = analyse(d, "full")
    stale = next(r for r in rep["results"] if r["check"] == "FAB-STALE-001")
    assert stale["status"] == "FAIL" and stale["violations"]
    assert all(v["text"].startswith("[gerbers_JLCPCB.zip]") for v in stale["violations"]), stale["violations"]


def test_attestation_on_real_board(board):
    from kicadverify import attest
    rep, _ = analyse(board, "full")
    prov = rep["provenance"]
    assert prov["tools"]["kicad-cli"]["version"] and prov["tools"]["kicad-happy"]["pinned"]
    assert prov["intent"]["complete"] and prov["policy"]["fab_profile"] == "jlcpcb-1-2-layer-standard"
    st = {v["id"]: v["status"] for v in rep["verification"]["requirements"]}
    assert st["GEN-INTENT-001"] == "VERIFIED"
    f = attest.write(attest.statement(rep), board / "verification" / "pcb" / "attestations")
    res = attest.check(f, board)
    assert res["holds"], res
    groups = {s["annotations"]["group"] for s in json.loads(f.read_text())["subject"]}
    assert {"design", "fabrication", "datasheets", "config"} <= groups
