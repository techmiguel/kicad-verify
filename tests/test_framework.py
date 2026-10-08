"""Unit tests of the requirements framework (no KiCad, no network)."""
import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

import kicadverify
from kicadverify import cli, config, manifest, outputs, report, requirements as rq, review, signoff, waivers
from kicadverify.checks import assertions, board, dfm, fab, happy, kicad_cli
from kicadverify.report import (FAIL, FAILED, NOT_RUN, NOT_VERIFIABLE, PASS, SKIP, VERIFIED, WARN, Result,
                                coverage, not_verifiable)

ROOT = Path(__file__).resolve().parent.parent


def req(rid, **kw):
    return rq.normalize({"id": rid, "text": f"text of {rid}", "source": {"kind": "best_practice", "ref": "x"}, **kw})


def verdict(reqs, results, mode="full"):
    out, _ = rq.evaluate(reqs, results, ".", mode)
    return {v["id"]: v for v in out}


# ------------------------------------------------------------------ normalisation
def test_normalize_defaults():
    a, m, h = req("A"), req("M", method="model"), req("H", method="inspection")
    assert (a["method"], a["gate"], a["verified_by"], a["acceptance"]) == ("auto", "fab", ["A"], "no_fail")
    assert (m["method"], m["gate"], m["verifier"]) == ("model", "release", "REVIEW")
    assert (h["method"], h["gate"], h["verifier"]) == ("human", "release", "SIGNOFF")
    x = req("X", check={"type": "pin_net"})
    assert x["verifier"] == "ASSERT" and x["verified_by"] == ["X"]
    assert rq.normalize({"id": "S", "text": "t", "source": "IPC-2221"})["source"] == {"kind": "unspecified",
                                                                                       "ref": "IPC-2221"}


# ------------------------------------------------------------------ verdicts
def test_status_mapping():
    reqs = [req("P"), req("F"), req("S"), req("W"), req("WS", acceptance="no_findings"), req("N")]
    res = [Result("P", PASS), Result("F", FAIL), Result("S", SKIP), Result("W", WARN, "w"), Result("WS", WARN)]
    v = verdict(reqs, res)
    assert {k: x["status"] for k, x in v.items()} == {
        "P": VERIFIED, "F": FAILED, "S": NOT_VERIFIABLE, "W": VERIFIED, "WS": FAILED, "N": NOT_RUN}
    assert v["F"]["severity"] == "error" and v["WS"]["severity"] == "warning"
    assert "accepted" in v["W"]["reason"]


def test_explicit_outcome_wins():
    v = verdict([req("A")], [Result("A", WARN, "tool crashed", outcome=NOT_VERIFIABLE)])
    assert v["A"]["status"] == NOT_VERIFIABLE


def test_partial_coverage_is_not_verified():
    gap = [{"key": "g1", "text": "D5 voltage unknown"}]
    r = Result("A", PASS, "ok", coverage=coverage("parts", 4, gap))
    v = verdict([req("A")], [r])
    assert v["A"]["status"] == NOT_VERIFIABLE and "3/4" in v["A"]["reason"]
    # a person checks D5 by hand and waives the gap: VERIFIED, with the deviation on record
    r = Result("A", PASS, "ok", coverage=coverage("parts", 4, list(gap)))
    waivers.apply([r], [{"check": "A", "key": "g1", "reason": "measured on the bench", "date": "2026-01-01",
                         "by": "ana"}])
    v = verdict([req("A")], [r])
    assert v["A"]["status"] == VERIFIED and v["A"]["coverage"]["checked"] == 4
    assert v["A"]["deviations"][0]["kind"] == "coverage" and v["A"]["deviations"][0]["by"] == "ana"


def test_precedence_and_patterns():
    reqs = [req("KH-*")]
    v = verdict(reqs, [Result("KH-A", PASS), Result("KH-B", SKIP), Result("KH-C", FAIL)])
    assert v["KH-*"]["status"] == FAILED
    v = verdict(reqs, [Result("KH-A", PASS), Result("KH-B", SKIP, outcome=NOT_RUN), Result("KH-C", SKIP)])
    assert v["KH-*"]["status"] == NOT_VERIFIABLE
    v = verdict([req("A", verified_by=["X", "Y"])], [Result("X", PASS), Result("Y", PASS)])
    assert v["A"]["status"] == VERIFIED and len(v["A"]["evidence"]) == 2


def test_not_run_reasons():
    reqs = [req("PCB-ERC-001"), req("M", method="model"), req("H", method="human")]
    v = verdict(reqs, [], mode="fast")
    assert "full mode" in v["PCB-ERC-001"]["reason"]
    assert "kicadverify review" in v["M"]["reason"] and "signoff H" in v["H"]["reason"]


def test_orphan_results_are_traced():
    reqs = [req("A"), rq.normalize({"id": "GEN-TRACE-001", "text": "t", "verified_by": ["@orphans"], "gate": "dev"})]
    v = verdict(reqs, [Result("A", PASS), Result("REV-EXTRA", FAIL, "x", violations=[{"key": "e", "text": "bad"}]),
                       Result("WAIVERS", WARN, "stale")])
    assert v["GEN-TRACE-001"]["status"] == FAILED and v["GEN-TRACE-001"]["findings"][0]["text"] == "bad"
    v = verdict(reqs, [Result("A", PASS), Result("PCB-IFACE-001", WARN)])
    assert v["GEN-TRACE-001"]["status"] == VERIFIED  # informational verifiers are not orphans


# ------------------------------------------------------------------ gates
def _v(rid, status, gate, severity=None, method="auto"):
    return {"id": rid, "status": status, "gate": gate, "severity": severity, "reason": "r", "method": method,
            "coverage": None}


def test_gates():
    vs = [_v("DEVFAIL", FAILED, "release", "error"),
          _v("WARNFAIL", FAILED, "release", "warning"),
          _v("NV", NOT_VERIFIABLE, "fab"),
          _v("NR", NOT_RUN, "release", method="human"),
          _v("ADVISORY", FAILED, "none", "error")]
    g = rq.gates(vs)
    blockers = {k: {b["id"] for b in x["blockers"]} for k, x in g.items()}
    assert blockers["dev"] == {"DEVFAIL"}
    assert blockers["fab"] == {"DEVFAIL", "NV"}
    assert blockers["release"] == {"DEVFAIL", "WARNFAIL", "NV", "NR"}
    assert "signoff NR" in next(b["action"] for b in g["release"]["blockers"] if b["id"] == "NR")
    g = rq.gates([_v("NV", NOT_VERIFIABLE, "fab")], {"fab": [FAILED]})
    assert g["fab"]["pass"]


# ------------------------------------------------------------------ lint
def test_lint():
    reqs = [req("A"), req("A"), rq.normalize({"id": "B", "text": "t", "method": "magic"}),
            req("C", verified_by=["NO-SUCH-CHECK"]), rq.normalize({"id": "D", "text": "t"}),
            req("E", source={"kind": "fab_capability", "ref": "fab", "confirmed": False}),
            req("F", check={"type": "nope"})]
    items = rq.lint(reqs, [{"id": "X", "reason": None}], assertions.TYPES)
    got = {(x["level"], x["id"], x["text"].split(" ")[0]) for x in items}
    assert ("error", "A", "duplicate") in got and ("error", "B", "unknown") in got
    assert ("error", "C", "verifier") in got and ("warning", "D", "no") in got
    assert ("warning", "E", "source") in got and ("error", "F", "unknown") in got
    assert ("warning", "X", "excluded") in got


def test_base_requirements_lint_clean():
    base = config.load_yaml(config.DATA / "requirements_base.yaml")
    reqs = [rq.normalize(r, "base") for r in base["requirements"]]
    assert not [x for x in rq.lint(reqs, [], assertions.TYPES) if x["level"] == "error"]
    assert all(r["source"] and r["source"].get("ref") for r in reqs)
    assert all(rq.registry_entry(r.get("verifier") or r["verified_by"][0]) or r["verified_by"] == ["@orphans"]
               for r in reqs)


# ------------------------------------------------------------------ waivers
def test_waivers_keep_record_and_detect_stale():
    r = Result("X", FAIL, "d", violations=[{"key": "a", "text": "A"}])
    out = waivers.apply([r], [{"check": "X", "key": "a", "reason": "ok", "date": "2026-01-01"},
                              {"check": "X", "key": "zz", "reason": "old", "date": "2026-01-01"},
                              {"check": "NOT-RUN", "key": "q", "reason": "r", "date": "2026-01-01"},
                              {"check": "X", "key": "b", "reason": "exp", "date": "2025-01-01",
                               "expires": "2025-06-01"}])
    assert r.status == PASS and r.waived[0]["reason"] == "ok"
    notes = " ".join(x.detail for x in out if x.check_id == "WAIVERS")
    assert "X zz" in notes and "NOT-RUN" not in notes and "expired" in notes


# ------------------------------------------------------------------ sign-off
def test_signoff_results(tmp_path):
    (tmp_path / "verification" / "pcb").mkdir(parents=True)
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)")
    reqs = [req("H1", method="human"), req("H2", method="human"), req("H3", method="human")]
    signoff.add(tmp_path, "H1", "ana", "ok")
    signoff.add(tmp_path, "H2", "ana", "pin 1 wrong", result="fail")
    v = verdict(reqs, signoff.results(tmp_path, reqs))
    assert (v["H1"]["status"], v["H2"]["status"], v["H3"]["status"]) == (VERIFIED, FAILED, NOT_RUN)
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb (changed))")
    v = verdict(reqs, signoff.results(tmp_path, reqs))
    assert v["H1"]["status"] == NOT_RUN and "not the current" in v["H1"]["reason"]


# ------------------------------------------------------------------ reviewer
def test_judge_maps_to_verdicts(tmp_path):
    (tmp_path / "d.json").write_text("U1 VO +3V3 regulator output", encoding="utf-8")
    reqs = [{"id": i} for i in "ABCDE"]
    raw = {"verdicts": [
        {"id": "A", "verdict": "PASS", "summary": "ok",
         "evidence": [{"file": "d.json", "quote": "U1 VO +3V3 regulator"}]},
        {"id": "B", "verdict": "PASS", "summary": "ok",
         "evidence": [{"file": "d.json", "quote": "invented quote here"}]},
        {"id": "C", "verdict": "FAIL", "summary": "bad", "evidence": []},
        {"id": "D", "verdict": "FAIL", "summary": "bad", "evidence": [{"file": "d.json", "quote": "regulator output"}]},
    ], "extra_findings": []}
    res, _, _ = review.judge(raw, reqs, tmp_path, tmp_path)
    v = verdict([req(i, method="model") for i in "ABCDE"], res)
    assert {k: x["status"] for k, x in v.items()} == {
        "A": VERIFIED, "B": NOT_VERIFIABLE, "C": NOT_VERIFIABLE, "D": FAILED, "E": NOT_VERIFIABLE}
    quote = v["A"]["evidence"][0]["artifacts"][0]
    assert quote["quote"] == "U1 VO +3V3 regulator" and quote["sha256"]


def test_review_reused_only_for_same_design_and_requirements(tmp_path):
    proj = {"root": tmp_path, "dir": tmp_path / "verification" / "pcb"}
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)")
    review.write_report(proj, {"model": "m"}, [], [], [Result("M", PASS, "ok")], "RH")
    dh = config.design_hash(tmp_path)
    assert review.load_current(proj, dh, "RH")[0][0].check_id == "M"
    assert review.load_current(proj, dh, "OTHER")[0] is None
    assert review.load_current(proj, "x" * 40, "RH")[0] is None
    # a reviewer that failed to run leaves a report with only REV-RUN: the next release must re-run it
    review.write_report(proj, {"model": "m"}, [], [], [Result("REV-RUN", FAIL, "reviewer did not run: timeout")],
                        "RH")
    assert review.load_current(proj, dh, "RH")[0] is None


# ------------------------------------------------------------------ DFM
PCB = """(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (setup (aux_axis_origin 0 0))
 (footprint "Conn:J" (layer "F.Cu") (at 10 10) (property "Reference" "J1")
  (pad "1" thru_hole circle (at 0 0) (size 1.0 1.0) (drill 0.8) (layers "*.Cu") (net "A"))
  (pad "2" thru_hole circle (at 1.1 0) (size 1.6 1.6) (drill 0.6) (layers "*.Cu") (net "B")))
 (footprint "R:R" (layer "F.Cu") (at 30 20 90) (property "Reference" "R1")
  (pad "1" smd rect (at 0 0 90) (size 1.0 0.5) (layers "F.Cu") (net "C")))
 (via (at 20 20) (size 0.4) (drill 0.3) (net "B"))
 (segment (start 5 30) (end 40 30) (width 0.1) (layer "F.Cu") (net "C"))
 (segment (start 5 0.2) (end 20 0.2) (width 0.2) (layer "F.Cu") (net "C"))
 (gr_rect (start 0 0) (end 50 40) (layer "Edge.Cuts")))"""

PROFILE = {"name": "t", "source": {"ref": "t"}, "layers_max": 2, "min_track_mm": 0.127, "min_via_drill_mm": 0.3,
           "min_via_diameter_mm": 0.5, "min_via_annular_mm": 0.1, "min_pth_drill_mm": 0.3, "min_pth_annular_mm": 0.15,
           "max_drill_mm": 6.3, "min_hole_to_hole_mm": 0.5, "min_copper_to_edge_mm": 0.3,
           "board_max_mm": [100, 100], "board_min_mm": [5, 5], "thickness_mm": [0.4, 2.0]}


@pytest.fixture
def synth_board(tmp_path):
    f = tmp_path / "b.kicad_pcb"
    f.write_text(PCB, encoding="utf-8")
    return board.load(f)


def test_dfm_geometry(synth_board):
    r = dfm.geometry(synth_board, "t", PROFILE)[0]
    text = " | ".join(v["text"] for v in r.violations)
    assert r.status == FAIL
    assert "net C: track 0.1 mm < 0.127" in text
    assert "diameter 0.4 mm < 0.5" in text and "annular ring 0.050 mm < 0.1" in text
    assert "J1.1: annular ring 0.100 mm < 0.15" in text
    assert "J1.1 and J1.2" in text and "hole to hole" in text
    assert "track C (F.Cu)" in text and "from the board edge" in text
    assert not r.coverage["unchecked"]


def test_dfm_missing_limit_is_unchecked(synth_board):
    prof = {k: v for k, v in PROFILE.items() if k != "min_copper_to_edge_mm"}
    r = dfm.geometry(synth_board, "t", prof)[0]
    assert [u["key"] for u in r.coverage["unchecked"]] == ["profile-min_copper_to_edge_mm"]
    assert verdict([req("FAB-DFM-001")], [r])["FAB-DFM-001"]["status"] == FAILED  # defects still fail


def test_dfm_rotated_pad_and_arc():
    p = {"size": (2.0, 0.5), "x": 0, "y": 0, "angle": 90, "shape": "rect"}
    pts = dfm.pad_shape(p)[1]
    assert max(abs(y) for _, y in pts) == pytest.approx(1.0) and max(abs(x) for x, _ in pts) == pytest.approx(0.25)
    arc = board._arc_pts((100, 102), (100.585786, 100.585786), (102, 100))
    assert arc[0] == pytest.approx((100, 102)) and arc[-1] == pytest.approx((102, 100))
    assert all(abs(((x - 102) ** 2 + (y - 102) ** 2) ** 0.5 - 2) < 1e-6 for x, y in arc)
    assert all(x <= 102 + 1e-9 and y <= 102 + 1e-9 for x, y in arc)  # the short way round


SLOT_PCB = """(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (footprint "USB:C" (layer "F.Cu") (at 10 20) (property "Reference" "J1")
  (pad "SH" thru_hole oval (at 0 0) (size 1.0 2.1) (drill oval 0.6 1.7) (layers "*.Cu") (net "GND")))
 (footprint "USB:C" (layer "F.Cu") (at 30 20 90) (property "Reference" "J2")
  (pad "SH" thru_hole oval (at 0 0 90) (size 1.0 2.1) (drill oval 0.6 1.7) (layers "*.Cu") (net "GND")))
 (via (at 11.0 20) (size 0.6) (drill 0.3) (net "GND"))
 (via (at 9.1 20) (size 0.6) (drill 0.3) (net "GND"))
 (via (at 30.3 21.0) (size 0.6) (drill 0.3) (net "GND"))
 (gr_rect (start 0 0) (end 50 40) (layer "Edge.Cuts")))"""


def test_dfm_slot_is_a_capsule(tmp_path):
    f = tmp_path / "s.kicad_pcb"
    f.write_text(SLOT_PCB, encoding="utf-8")
    r = dfm.geometry(board.load(f), "t", PROFILE)[0]
    h2h = sorted(v["text"] for v in r.violations if "hole to hole" in v["text"])
    # 0.6 x 1.7 slot: a via 1.0 mm from its centre across the slot is 0.55 mm away (a circle of the
    # slot's length would put it at 0); one 0.9 mm across on the other side is 0.45 mm away; on the 90-degree slot the
    # long axis is horizontal, so a via 1.0 mm below is 0.55 mm away
    assert h2h == ["J1.SH and via GND: 0.450 mm hole to hole < 0.5 mm"]


EPAD_PCB = """(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (footprint "RF_Module:ESP32" (layer "F.Cu") (at 20 20) (property "Reference" "U1") (attr smd)
  (pad "41" smd rect (at 0 0) (size 3.9 3.9) (layers "F.Cu" "F.Mask") (net "GND"))
  (pad "41" thru_hole circle (at -1 -1) (size 0.6 0.6) (drill 0.3) (layers "*.Cu") (net "GND"))
  (pad "41" thru_hole circle (at 1 1) (size 0.45 0.45) (drill 0.3) (layers "*.Cu") (net "GND")))
 (footprint "Conn:J" (layer "F.Cu") (at 30 20) (property "Reference" "J1")
  (pad "1" thru_hole circle (at 0 0) (size 0.6 0.6) (drill 0.3) (layers "*.Cu") (net "A")))
 (gr_rect (start 0 0) (end 50 40) (layer "Edge.Cuts")))"""


def test_dfm_thermal_vias_use_via_limits(tmp_path):
    f = tmp_path / "e.kicad_pcb"
    f.write_text(EPAD_PCB, encoding="utf-8")
    r = dfm.geometry(board.load(f), "t", {**PROFILE, "min_pth_annular_mm": 0.18})[0]
    text = sorted(v["text"] for v in r.violations)
    # the 0.6/0.3 via inside the exposed pad meets the via ring (0.1 mm); the 0.45/0.3 one does not;
    # the 0.6/0.3 lead of J1 is a through-hole lead and keeps the PTH limit
    assert text == ["J1.1: annular ring 0.150 mm < 0.18 mm (pad 0.6x0.6, drill 0.3)",
                    "U1.41: thermal via annular ring 0.075 mm < 0.1 mm"]


ASSY_PCB = """(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
""" + "".join(f""" (footprint "R:R" (layer "F.Cu") (at {x} 20) (property "Reference" "{r}") (attr smd)
  (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "A"))
  (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "B")))
""" for r, x in (("B1", 10), ("B2", 12.54), ("B3", 15.08), ("C1", 25.4))) + """
 (footprint "Conn:J" (layer "F.Cu") (at 40 20) (property "Reference" "J1") (attr through_hole)
  (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net "A")))
 (footprint "Mech:Heatsink" (layer "B.Cu") (at 30 30) (property "Reference" "HS1") (attr through_hole))
 (footprint "Fiducial:Fiducial_1mm" (layer "F.Cu") (at 5 5) (property "Reference" "FID1")
  (attr smd exclude_from_bom)
  (pad "" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))
 (footprint "R:R" (layer "F.Cu") (at 45 30) (property "Reference" "R9") (attr smd dnp)
  (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net "A"))))"""


@pytest.fixture
def assy_board(tmp_path):
    f = tmp_path / "a.kicad_pcb"
    f.write_text(ASSY_PCB, encoding="utf-8")
    return board.load(f)


def test_bom_expands_designator_ranges(assy_board, tmp_path):
    f = tmp_path / "BOM.csv"
    f.write_text('"Reference","Value","Footprint"\n"B1-B3","1k","R:R"\n"C1","100n","R:R"\n"J1","hdr","Conn:J"\n'
                 '"HS1","Heatsink","Mech:Heatsink"\n', encoding="utf-8")
    r = fab.bom(assy_board, [f], "t")[0]
    assert r.status == PASS, [v["text"] for v in r.violations]  # HS1 has no pads but is on the PCB
    f.write_text(f.read_text() + '"R9","10k","R:R"\n', encoding="utf-8")
    assert [v["text"] for v in fab.bom(assy_board, [f], "t")[0].violations] == [
        "R9: in the BOM but not assembled on the PCB (or excluded from BOM)"]  # DNP on the PCB
    assert fab._designators("R1-3, R7;U2") == ["R1", "R2", "R3", "R7", "U2"]
    assert fab._designators("U1-A") == ["U1-A"]  # not a range


def test_bom_dnp_rows_part_numbers_and_several_files(assy_board, tmp_path):
    # KiCad's BOM with a DNP column, as on air-quality-sensor; MPN filled, no supplier column
    kicad = tmp_path / "board_BOM.csv"
    kicad.write_text('"Reference","Qty","DNP","Footprint","Value","Manufacturer Part Number (MPN)"\n'
                     '"B1,B2,B3","3","","R:R","1k","RC0603"\n"C1","1","","R:R","100n","CL10"\n'
                     '"J1","1","","Conn:J","hdr","PH1"\n"HS1","1","","Mech:Heatsink","HS","H1"\n'
                     '"R9","1","DNP","R:R","10k","RC0603"\n', encoding="utf-8")
    r = fab.bom(assy_board, [kicad], "t")[0]
    assert r.status == PASS, [v["text"] for v in r.violations]  # R9 is DNP on both sides
    # a supplier BOM with an empty LCSC column but a Digi-Key code is not missing a part number
    dk = tmp_path / "dk-BOM.csv"
    dk.write_text("Qty,Reference,Value,PARTNO,DK,LCSC\n3,B1-B3,1k,RC0603,311-1.00KHRCT-ND,\n1,C1,100n,,,\n"
                  "1,J1,hdr,PH1,,\n1,HS1,HS,H1,,\n", encoding="utf-8")
    r = fab.bom(assy_board, [dk], "t")[0]
    assert [v["text"] for v in r.violations] == ["C1: no supplier or manufacturer part number"]
    # a BOM marking a fitted part DNP is a mismatch
    bad = tmp_path / "bad_BOM.csv"
    bad.write_text(kicad.read_text().replace('"C1","1","",', '"C1","1","DNP",'), encoding="utf-8")
    assert "C1: marked DNP in the BOM but fitted on the PCB" in [
        v["text"] for v in fab.bom(assy_board, [bad], "t")[0].violations]
    # several BOMs: the one the board agrees with is checked, the others are named
    r = fab.bom(assy_board, [bad, kicad], "t")[0]
    assert r.status == PASS and "board_BOM.csv checked, also found bad_BOM.csv" in r.detail
    # rows with a trailing comma (more cells than the header): csv keeps the extra cells in a list
    trailing = tmp_path / "trailing_BOM.csv"
    trailing.write_text("".join(ln + ",\n" if i else ln + "\n"
                                for i, ln in enumerate(kicad.read_text().splitlines())), encoding="utf-8")
    assert fab.bom(assy_board, [trailing], "t")[0].status == PASS


def test_cpl_reads_kicad_ascii_pos_in_inches(assy_board, tmp_path):
    rows = "".join(f"{r:<9} 1k        R_0603   {x / 25.4:.4f}   {-y / 25.4:.4f}   0.0000  top\n"
                   for r, x, y in (("B1", 10, 20), ("B2", 12.54, 20), ("B3", 15.08, 20), ("C1", 25.4, 20),
                                   ("FID1", 5, 5)))
    f = tmp_path / "position.csv"  # KiCad's ASCII .pos saved with a .csv name, as in real projects
    f.write_text("### Footprint positions - created on 2026-09-29 ###\n### Printed by KiCad version 10.0.6\n"
                 "## Unit = inches, Angle = deg.\n## Side : All\n"
                 "# Ref     Val       Package  PosX       PosY       Rot  Side\n" + rows + "## End\n",
                 encoding="utf-8")
    r = fab.cpl(assy_board, [f], "t", 0.5)[0]
    # an SMD-only placement file: the through-hole J1 is a warning; the fiducial in it is fine
    assert r.status == WARN and [v["text"] for v in r.violations] == [
        "J1: through-hole, not in the SMD-only CPL (fitted by hand?)"]
    f.write_text(f.read_text().replace(f"{25.4 / 25.4:.4f}", "1.1000"), encoding="utf-8")  # C1 moved 2.5 mm
    r = fab.cpl(assy_board, [f], "t", 0.5)[0]
    assert [v["text"].split(":")[0] for v in r.violations] == ["C1", "J1"]


def test_smd_only_cpl_still_catches_an_smd_part_with_a_through_hole_attribute(assy_board, tmp_path):
    # C1 has SMD pads but the through_hole attribute: an --smd-only export drops it. B1 is an SMD
    # part with a plated peg (a USB connector shell): placed by machine, it keeps the file SMD-only
    for fp in assy_board["footprints"]:
        if fp["ref"] == "C1":
            fp["attr"] = ["through_hole"]
        if fp["ref"] == "B1":
            fp["pads"].append({**fp["pads"][0], "number": "SH", "type": "thru_hole", "drill": 0.6})
    f = tmp_path / "pos.csv"
    f.write_text("Designator,Mid X,Mid Y,Layer,Rotation\nB1,10,20,top,0\nB2,12.54,20,top,0\nB3,15.08,20,top,0\n",
                 encoding="utf-8")
    texts = [v["text"] for v in fab.cpl(assy_board, [f], "t", 0.5)[0].violations]
    assert "C1: missing from the CPL (wrong SMD/THT or 'exclude from position files' attribute?)" in texts
    assert "J1: through-hole, not in the SMD-only CPL (fitted by hand?)" in texts


def test_cpl_with_through_hole_parts_requires_all(assy_board, tmp_path):
    f = tmp_path / "cpl.csv"
    f.write_text("Designator,Mid X,Mid Y,Layer,Rotation\nB1,10,20,top,0\nB2,12.54,20,top,0\nC1,25.4,20,top,0\n"
                 "J1,40,20,top,0\nR9,45,30,top,0\n", encoding="utf-8")
    r = fab.cpl(assy_board, [f], "t", 0.5)[0]
    assert r.status == FAIL
    assert sorted(v["text"].split(":")[0] for v in r.violations) == ["B3", "R9"]  # missing; DNP placed


WIDTH_PCB = """(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (footprint "R:R" (layer "F.Cu") (at 10 10) (property "Reference" "R5")
  (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net "+BATT"))
  (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net "DIV")))
 (segment (start 9.5 10) (end 5 10) (width 0.2) (layer "F.Cu") (net "+BATT"))
 (segment (start 5 10) (end 5 20) (width 1.0) (layer "F.Cu") (net "+BATT"))
 (segment (start 20 10) (end 30 10) (width 0.2) (layer "F.Cu") (net "VBUS_EN"))
 (segment (start 20 12) (end 30 12) (width 0.2) (layer "F.Cu") (net "VBUS_FAULT"))
 (segment (start 20 14) (end 30 14) (width 0.2) (layer "F.Cu") (net "/Sense/+5V"))
 (footprint "C:C" (layer "F.Cu") (at 40 10) (property "Reference" "C7")
  (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net "GND")))
 (segment (start 39.5 10) (end 35 10) (width 0.15) (layer "F.Cu") (net "GND"))
 (segment (start 35 10) (end 35 20) (width 1.0) (layer "F.Cu") (net "GND")))"""


def _width(tmp_path, params):
    f = tmp_path / "w.kicad_pcb"
    f.write_text(WIDTH_PCB, encoding="utf-8")
    proj = {"root": tmp_path, "params": params, "pins": {}}
    return next(x for x in board.run(board.load(f), "t", proj) if x.check_id == "PCB-WIDTH-001")


def test_power_width_needs_a_current_and_skips_stubs_and_control_signals(tmp_path):
    # VBUS_EN / VBUS_FAULT are a load-switch enable and a fault flag, not the rail; a sheet named
    # /Sense/ does not hide its rail; the 0.15 mm GND stub only reaches a decoupling capacitor
    r = _width(tmp_path, {})
    assert r.status == PASS and not r.violations
    assert sorted(u["text"] for u in r.coverage["unchecked"]) == [
        "+BATT: tracks down to 0.2 mm (thin tracks reach R5.1); declare the net's current in params.net_currents "
        "to check them against IPC-2221",
        "/Sense/+5V: tracks down to 0.2 mm; declare the net's current in params.net_currents to check them "
        "against IPC-2221"]
    assert r.coverage["total"] == 3  # +BATT, /Sense/+5V, GND
    assert verdict([req("PCB-WIDTH-001")], [r])["PCB-WIDTH-001"]["status"] == NOT_VERIFIABLE
    # with the currents declared: 0.2 mm of 35 µm copper carries ~0.9 A at +10 °C (IPC-2221)
    r = _width(tmp_path, {"net_currents": {"+BATT": 0.5, "~.*5V": 2}})
    assert not r.coverage["unchecked"] and [v["text"] for v in r.violations] == [
        "/Sense/+5V: 0.2 mm on F.Cu for 2 A; IPC-2221 needs 0.78 mm (+10 °C, 35 µm copper)"]
    # a fixed project limit keeps the former behaviour
    r = _width(tmp_path, {"min_power_width_mm": 0.25})
    assert sorted(v["text"] for v in r.violations) == ["+BATT: 0.2 mm (thin tracks reach R5.1)", "/Sense/+5V: 0.2 mm"]


def test_power_width_inline_capacitors_and_declared_non_power_nets(tmp_path):
    f = tmp_path / "w2.kicad_pcb"
    f.write_text("""(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (footprint "C:C" (layer "F.Cu") (at 10 10) (property "Reference" "C1")
  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "+5V")))
 (footprint "C:C" (layer "F.Cu") (at 20 10) (property "Reference" "C2")
  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "+5V")))
 (segment (start 10 10) (end 20 10) (width 0.15) (layer "F.Cu") (net "+5V"))
 (segment (start 10 20) (end 30 20) (width 0.2) (layer "F.Cu") (net "/MOTOR_A")))""")
    r = board.power_width(board.load(f), "t", {"net_currents": {"+5V": 2, "~/MOTOR.*": 3.5}})
    # bulk C1 -> 0.15 mm -> C2 may carry the load: judged; /MOTOR_A is no power name but has a current
    assert sorted(v["text"].split(":")[0] for v in r.violations) == ["+5V", "/MOTOR_A"]


def test_ipc2221_width():
    # IPC-2221 chart values: 1 A, +10 °C, 1 oz outer copper ~ 0.30 mm; inner layers need about 2.6x
    assert board.ipc2221_width_mm(1.0, 0.035) == pytest.approx(0.30, abs=0.01)
    assert board.ipc2221_width_mm(1.0, 0.035, internal=True) / board.ipc2221_width_mm(1.0, 0.035) == pytest.approx(
        2 ** (1 / 0.725), rel=1e-6)


def test_dnp_alternative_placement_is_a_warning():
    overlap = {"type": "courtyards_overlap", "severity": "error", "description": "Courtyards overlap",
               "items": [{"description": "Footprint R9"}, {"description": "Footprint U5"}]}
    pth = {"type": "pth_inside_courtyard", "severity": "error", "description": "PTH inside courtyard",
           "items": [{"description": "PTH pad 1 [/MR+] of J106"}, {"description": "Footprint J8"}]}
    holes = {"type": "hole_to_hole", "severity": "error", "description": "Drilled hole too close",
             "items": [{"description": "PTH pad 1 [/MR+] of J106"}, {"description": "PTH pad 2 [/MR+] of J104"}]}
    dnp = {"U5", "J104", "J106"}
    assert kicad_cli._dnp_downgrade(overlap, dnp)["severity"] == "warning"
    assert kicad_cli._dnp_downgrade(overlap, dnp)["description"].endswith("[DNP: U5]")
    assert kicad_cli._dnp_downgrade(pth, dnp)["severity"] == "warning"
    assert kicad_cli._dnp_downgrade(holes, dnp) is holes  # the holes are drilled anyway
    assert kicad_cli._dnp_downgrade(overlap, set()) is overlap

    kh = {"pcb": {"findings": [
        {"rule_id": "PM-001", "severity": "error", "summary": "Courtyard overlap between C3 and U5",
         "components": ["C3", "U5"]},
        {"rule_id": "PM-001", "severity": "error", "summary": "Courtyard overlap between R10 and J1",
         "components": ["J1", "R10"]}]}, "_version": "t"}
    r = next(x for x in happy.to_results(kh, "t", {}, dnp) if x.check_id == "KH-PM-001")
    assert r.status == FAIL  # R10/J1 are both fitted
    assert any("[DNP: U5]" in v["text"] for v in r.violations)
    r = next(x for x in happy.to_results({"pcb": {"findings": kh["pcb"]["findings"][:1]}, "_version": "t"},
                                         "t", {}, dnp) if x.check_id == "KH-PM-001")
    assert r.status == WARN


def test_drill_positions_match_within_tolerance(tmp_path):
    f = tmp_path / "d.kicad_pcb"
    f.write_text("""(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (footprint "Conn:J" (layer "F.Cu") (at 87.005 50.292) (property "Reference" "J3")
  (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net "A"))
  (pad "2" thru_hole circle (at 10 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net "A")))
 (via (at 122.1013 70.905) (size 0.5) (drill 0.3) (net "B")))""", encoding="utf-8")
    drl = tmp_path / "x-PTH.drl"
    # KiCad 8 inch output, 4 decimals: J3.1 and the via sit on half-steps of 0.01 mm once converted
    # (87.0052, 70.9041), which a rounded lookup missed; J3.2 was moved 0.05 mm after the export
    drl.write_text("M48\nINCH\nT1C0.0118\nT2C0.0394\n%\nG90\nT1\nX4.8071Y-2.7916\nT2\nX3.4254Y-1.98\n"
                   "X3.8211Y-1.98\nM30\n", encoding="utf-8")
    r = fab.drills(board.load(f), [drl], "t")[0]
    assert [v["text"] for v in r.violations] == [
        "hole Ø1.0 mm at (97.00, 50.29) on the PCB has no match in the .drl files"]


def _gbr(path, func, body, fs="%FSLAX46Y46*%", unit="%MOMM*%"):
    path.write_text(f"%TF.FileFunction,{func}*%\n{fs}\n{unit}\n" + body + "M02*\n", encoding="utf-8")
    return path


def test_gerber_sets_normalise_format_and_skip_plot_artefacts(tmp_path):
    pad = "%TA.AperFunction,SMDPad,CuDef*%\n%ADD10R,1X1*%\n%TD*%\n"
    prof = "%TA.AperFunction,Profile*%\n%ADD11C,0.1*%\n%TD*%\n"
    a = _gbr(tmp_path / "a.gbr", "Copper,L1,Top",
             pad + prof + "D10*\nX12345600Y-5000000D03*\nD11*\nX0Y0D02*\nX1000000Y0D01*\n")
    # the same pad written by another exporter: inches, 4.5 format (12.3456 mm = 0.48605 in)
    b = _gbr(tmp_path / "b.gbr", "Copper,L1,Top", pad + "D10*\nX48605Y-19685D03*\n", "%FSLAX45Y45*%", "%MOIN*%")
    mask = _gbr(tmp_path / "m.gbr", "Soldermask,Top", "%ADD10R,1X1*%\nD10*\nX12345600Y-5000000D03*\n")
    sa, sb, sm = (fab._gerber_sets([f]) for f in (a, b, mask))
    assert sa["Copper,L1,Top"] == {(12.3456, -5.0)}  # the profile drawn on the copper layer is ignored
    assert fab._unmatched(sa["Copper,L1,Top"], sb["Copper,L1,Top"]) == 0
    assert fab._unmatched({(12.4, -5.0)}, sb["Copper,L1,Top"]) == 1
    assert sm["Soldermask,Top"] == {(12.3456, -5.0)}  # mask openings carry no aperture attributes
    funcs = ("Copper,L1,Top", "Copper,L2,Inr", "Copper,L4,Bot", "Soldermask,Bot", "Paste,Top", "Legend,Top",
             "Profile,NP", "Other,User")
    assert [fab._plot_layer(f) for f in funcs] == [
        "F.Cu", "In1.Cu", "B.Cu", "B.Mask", "F.Paste", "F.Silkscreen", "Edge.Cuts", None]


def test_kicad_happy_defaults_and_duplicates():
    def f(rule, summary, conf="heuristic", sev="error"):
        return {"rule_id": rule, "severity": sev, "summary": summary, "confidence": conf, "components": ["U5"]}
    kh = {"schematic": {"findings": [
        f("PP-001", "IC power pin U3.1 (CB) has no DC path to a power rail"),
        f("VD-001", "C1 (10uF 4V) exceeds voltage derating on +5V"),  # heuristic, yet a real defect
        f("FD-001", "No fiducials on F.Cu", "deterministic")]},
        "pcb": {"findings": [f("TV-001", "Thermal vias: U5 insufficient", "deterministic", "warning")] * 3},
        "_version": "t"}
    res = {r.check_id: r for r in happy.to_results(kh, "t", {})}
    assert res["KH-PP-001"].status == WARN and res["KH-FD-001"].status == WARN
    assert res["KH-VD-001"].status == FAIL  # still blocks: the seeded 4 V capacitor on 5 V
    assert len(res["KH-TV-001"].violations) == 1
    forced = {r.check_id: r for r in happy.to_results(kh, "t", {"kicad_happy": {"severity": {"PP-001": "fail"}}})}
    assert forced["KH-PP-001"].status == FAIL


def test_drill_sets_are_checked_one_by_one_and_zips_extract_safely(tmp_path):
    import zipfile
    f = tmp_path / "d.kicad_pcb"
    f.write_text("""(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (via (at 10 10) (size 0.6) (drill 0.3) (net "A")) (via (at 20 10) (size 0.6) (drill 0.3) (net "A")))""")
    fresh = "M48\nMETRIC\nT1C0.300\n%\nG90\nT1\nX10.0Y-10.0\nX20.0Y-10.0\nM30\n"
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "b.drl").write_text(fresh)
    # the uploaded archive was exported before the second via moved; members sit in a sub-folder and one
    # tries to escape the extraction directory
    with zipfile.ZipFile(tmp_path / "out" / "order.zip", "w") as z:
        z.writestr("gerbers/b.drl", fresh.replace("X20.0Y-10.0", "X21.0Y-10.0"))
        z.writestr("../../evil.drl", fresh)
    b = board.load(f)
    _, drl, _, _ = fab.find_outputs(tmp_path, {}, unzip_to=tmp_path / "unz")
    assert not (tmp_path / "evil.drl").exists() and not (tmp_path.parent / "evil.drl").exists()
    assert sorted(p.relative_to(tmp_path).as_posix() for p in drl) == [
        "out/b.drl", "unz/out/order.zip/b.drl", "unz/out/order.zip/evil.drl"]
    r = fab.drills(b, drl[:2], "t")[0]
    # merged, every hole counted twice; one set at a time, only the stale archive fails
    assert r.status == FAIL and [v["text"] for v in r.violations] == [
        "[order.zip] hole Ø0.3 mm at (20.00, 10.00) on the PCB has no match in the .drl files"]
    assert fab.archives(tmp_path, {}) == [tmp_path / "out" / "order.zip"]
    # an archive with the same name in another folder is extracted beside it, not over it
    (tmp_path / "rev1").mkdir()
    with zipfile.ZipFile(tmp_path / "rev1" / "order.zip", "w") as z:
        z.writestr("b.drl", fresh)
    _, drl, _, _ = fab.find_outputs(tmp_path, {}, unzip_to=tmp_path / "unz")
    sets = fab.output_sets([d for d in drl if d.name == "b.drl"])
    assert len(sets) == 3 and all(Path(f).exists() for fs in sets.values() for f in fs)


def test_init_finds_outputs_outside_the_project_folder(tmp_path):
    import zipfile
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    kicad = repo / "hardware" / "kicad"
    kicad.mkdir(parents=True)
    (kicad / "x.kicad_pro").write_text("{}")
    (kicad / "x.kicad_pcb").write_text("(kicad_pcb)")
    # another board of the same repository, with more outputs: not this board's
    (repo / "other" / "manufacturing").mkdir(parents=True)
    for layer in ("F_Cu", "B_Cu", "F_Mask", "B_Mask"):
        (repo / "other" / "manufacturing" / f"other-{layer}.gbr").write_text("%TF.FileFunction,Other*%\n")
    (repo / "hardware" / "fab" / "gerber").mkdir(parents=True)
    (repo / "hardware" / "fab" / "gerber" / "x-F_Cu.gbr").write_text("%TF.FileFunction,Copper,L1,Top*%\n")
    with zipfile.ZipFile(repo / "hardware" / "fab" / "x-gerbers.zip", "w") as z:
        z.writestr("x-F_Cu.gbr", "%TF.FileFunction,Copper,L1,Top*%\n")
    (repo / "hardware" / "bom").mkdir()
    (repo / "hardware" / "bom" / "bom.csv").write_text("Designator\n")
    (repo / "hardware" / "bom" / "cpl.csv").write_text("Designator\n")
    (repo / "tools" / "test").mkdir(parents=True)
    (repo / "tools" / "test" / "jlc_cpl_rotations.csv").write_text("x\n")
    config.init_project(kicad)
    params = config.load_yaml(kicad / config.DIRNAME / "requirements.yaml", {})["params"]
    assert params["fab"] == {"profile": None, "dir": "../fab", "bom": "../bom/bom.csv", "cpl": "../bom/cpl.csv"}
    g, _, b, c = fab.find_outputs(kicad, params, unzip_to=tmp_path / "unz")
    assert len(g) == 2 and b == [kicad / "../bom/bom.csv"] and c == [kicad / "../bom/cpl.csv"]


def test_init_detects_outputs_for_the_chosen_board_whatever_the_template_order(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for b in ("a", "b"):
        (repo / "hw" / b).mkdir(parents=True)
        (repo / "hw" / b / f"{b}.kicad_pro").write_text("{}")
        (repo / "hw" / b / f"{b}.kicad_pcb").write_text("(kicad_pcb)")
    (repo / "fab").mkdir()
    (repo / "fab" / "a-F_Cu.gbr").write_text("%TF.FileFunction,Copper,L1,Top*%\n")
    real = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(sorted(
        real(self), key=lambda f: f.name != "requirements.yaml")))
    config.init_project(repo / "hw", board=repo / "hw" / "a" / "a.kicad_pro")
    params = config.load_yaml(repo / "hw" / config.DIRNAME / "requirements.yaml", {})["params"]
    assert params["fab"]["dir"] == "../fab"


def test_one_board_per_verification(tmp_path, capsys):
    repo = tmp_path / "hw"
    for b in ("main", "daughter"):
        (repo / b).mkdir(parents=True)
        for ext in (".kicad_pro", ".kicad_pcb", ".kicad_sch"):
            (repo / b / (b + ext)).write_text("{}" if ext == ".kicad_pro" else f"({b})")
    (repo / "main" / "power.kicad_sch").write_text("(sub-sheet)")
    # two boards and none chosen: refuse rather than mix them
    assert cli.main(["init", str(repo)]) == 2
    assert "kicad-verify reviews one board at a time" in capsys.readouterr().err
    assert not (repo / config.DIRNAME).exists()
    # --board as the hint lists it: relative to the project root, whatever the current directory
    assert cli.main(["init", str(repo), "--board", "main/main.kicad_pro"]) == 0
    assert [k["label"] for k in config.discover(repo)] == ["main"]
    capsys.readouterr()
    # verify given the other board's .kicad_pro: refused, not silently the recorded board
    assert cli.main(["verify", str(repo / "daughter" / "daughter.kicad_pro"), "--fast"]) == 2
    assert "is about main.kicad_pro, not daughter.kicad_pro" in capsys.readouterr().err
    # run from the other board's folder: refused too
    assert cli.main(["verify", str(repo / "daughter"), "--fast"]) == 2
    assert "holds daughter.kicad_pro" in capsys.readouterr().err
    # init --board on an initialised project changes the board
    assert cli.main(["init", str(repo), "--board", "daughter/daughter.kicad_pro"]) == 0
    assert [k["label"] for k in config.discover(repo)] == ["daughter"]
    assert cli.main(["init", str(repo), "--board", "main/main.kicad_pro"]) == 0
    # each board's outputs: only main's are checked for main
    for b in ("main", "daughter"):
        (repo / b / "gerbers").mkdir()
        (repo / b / "gerbers" / f"{b}-F_Cu.gbr").write_text("%TF.FileFunction,Copper,L1,Top*%\n")
    g, _, _, _ = fab.find_outputs(repo, {})
    assert [x.name for x in g] == ["main-F_Cu.gbr"]
    # the board at the top of a folder with another board in a sub-folder: that board's outputs are not this one's
    top = tmp_path / "top"
    (top / "daughter" / "gerbers").mkdir(parents=True)
    for d, b in ((top, "main"), (top / "daughter", "daughter")):
        (d / f"{b}.kicad_pro").write_text("{}")
        (d / f"{b}.kicad_pcb").write_text("(x)")
    (top / "daughter" / "gerbers" / "daughter-F_Cu.gbr").write_text("%TF.FileFunction,Copper,L1,Top*%\n")
    (top / "daughter" / "daughter.drl").write_text("M48\n")
    (top / "main-F_Cu.gbr").write_text("%TF.FileFunction,Copper,L1,Top*%\n")
    g, d, _, _ = fab.find_outputs(top, {})
    assert [x.name for x in g] == ["main-F_Cu.gbr"] and d == []
    assert sorted(f.name for f in config.design_files(repo)) == [
        "main.kicad_pcb", "main.kicad_pro", "main.kicad_sch", "power.kicad_sch"]
    h = config.design_hash(repo)
    (repo / "daughter" / "daughter.kicad_pcb").write_text("(changed)")  # the other board changes
    assert config.design_hash(repo) == h
    (repo / "main" / "power.kicad_sch").write_text("(changed)")  # a sheet of this board changes
    assert config.design_hash(repo) != h
    # a folder whose own board sits at its top keeps it even with boards in sub-folders
    (repo / "main" / "sub").mkdir()
    (repo / "main" / "sub" / "x.kicad_pro").write_text("{}")
    assert [k["label"] for k in config.discover(repo / "main")] == ["main"]


def _pro(tmp_path, min_clearance, classes, dru=None):
    pro = tmp_path / "x.kicad_pro"
    pro.write_text(json.dumps({"board": {"design_settings": {"rules": {"min_clearance": min_clearance}}},
                               "net_settings": {"classes": [{"name": n, "clearance": c} for n, c in classes]}}))
    dru_file = pro.with_suffix(".kicad_dru")
    dru_file.unlink(missing_ok=True)
    if dru:
        dru_file.write_text(dru)
    return pro


def test_fab_rules(tmp_path):
    prof = {"name": "t", "min_spacing_mm": 0.127}
    assert dfm.rules(_pro(tmp_path, 0.2, [("Default", 0.1)]), "t", prof)[0].status == PASS  # board floor holds
    r = dfm.rules(_pro(tmp_path, 0.0, [("Default", 0.2), ("HV", 0.1)]), "t", prof)[0]
    assert r.status == FAIL and "HV" in r.violations[0]["text"]
    dru = '(version 1)\n(rule "tight"\n  (constraint clearance (min 0.1mm))\n  (condition "A.NetClass == \'X\'"))\n'
    r = dfm.rules(_pro(tmp_path, 0.0, [("Default", 0.2)], dru), "t", prof)[0]
    assert r.status == FAIL and "tight" in r.violations[0]["text"]
    assert dfm.rules(_pro(tmp_path, 0.0, [("Default", 0.2)]), "t", prof)[0].status == PASS


def test_fab_profile_resolution():
    assert dfm.resolve_profile({})[0] is None
    assert "unknown" in dfm.resolve_profile({"fab": {"profile": "nope"}})[1]
    p, _ = dfm.resolve_profile({"fab": {"profile": {"base": "jlcpcb-1-2-layer-standard", "min_track_mm": 0.2,
                                                    "source": {"confirmed": True, "by": "ana"}}}})
    assert p["min_track_mm"] == 0.2 and p["min_via_drill_mm"] == 0.3 and p["source"]["confirmed"] is True
    built = dfm.builtin_profiles()["jlcpcb-1-2-layer-standard"]
    assert built["source"]["confirmed"] is False  # values not confirmed against the fab's own page
    res = dfm.run({"copper_layers": []}, "x.kicad_pro", "t", {})
    assert [r.outcome for r in res] == [NOT_VERIFIABLE, NOT_VERIFIABLE]


# ------------------------------------------------------------------ assertions
def _nl():
    return {"components": {"U1": {"value": "AMS1117-3.3", "footprint": "SOT-223", "dnp": False,
                                  "fields": {"MPN": "AMS1117-3.3"}, "pins": {}},
                           "R1": {"value": "10k", "footprint": "R0805", "dnp": False, "fields": {}, "pins": {}}},
            "nets": {"+3V3": [], "GND": []}, "pin_net": {("U1", "2"): "+3V3"}}


def test_assertions(synth_board):
    def run(spec):
        return assertions.evaluate({"id": "P", "check": spec}, _nl(), synth_board, "t")
    assert run({"type": "pin_net", "ref": "U1", "pin": 2, "net": "+3V3"}).status == PASS
    assert run({"type": "pin_net", "ref": "J1", "pin": 1, "net": "B"}).status == FAIL
    # the schematic has J1 pin 1 on B but the PCB pad is on A: both sides are checked
    nl = _nl()
    nl["components"]["J1"] = {"value": "", "footprint": "Conn:J", "dnp": False, "fields": {}, "pins": {}}
    nl["pin_net"][("J1", "1")] = "B"
    r = assertions.evaluate({"id": "P", "check": {"type": "pin_net", "ref": "J1", "pin": 1, "net": "B"}}, nl,
                            synth_board, "t")
    assert r.status == FAIL and [v["text"] for v in r.violations] == ["J1.1: net A in the PCB, required B"]
    assert run({"type": "value", "ref": "U1", "matches": "~AMS1117-3\\.3"}).status == PASS
    r = run({"type": "field", "field": "MPN"})
    assert r.status == FAIL and "R1" in r.violations[0]["text"] and r.coverage["total"] == 2
    assert run({"type": "track_width", "net": "C", "min": 0.15}).status == FAIL
    assert run({"type": "track_width", "net": "NOPE", "min": 0.15}).outcome == NOT_VERIFIABLE
    assert run({"type": "layer_count", "equals": 2}).status == PASS
    assert run({"type": "board_size", "max": [40, 40]}).status == FAIL
    assert run({"type": "net_exists", "net": "GND"}).status == PASS
    assert run({"type": "weird"}).status == FAIL


# ------------------------------------------------------------------ project config
def _project(tmp_path, reqs_yaml, extra=None):
    vd = tmp_path / "verification" / "pcb"
    vd.mkdir(parents=True)
    (tmp_path / "p.kicad_pro").write_text("{}")
    (vd / "requirements.yaml").write_text(yaml.safe_dump(reqs_yaml))
    for name, data in (extra or {}).items():
        (vd / name).write_text(data)
    return config.load_project(tmp_path)


def test_project_overrides_exclusions_and_assembly(tmp_path):
    proj = _project(tmp_path, {
        "params": {"fab": {"assembly": False}},
        "requirements": [{"id": "PCB-WIDTH-001", "gate": "none"},
                         {"id": "PRJ-1", "text": "t", "source": {"kind": "customer", "ref": "spec"},
                          "check": {"type": "layer_count", "equals": 2}}],
        "disable": [{"id": "PCB-HOLE-001", "reason": "no enclosure"}, "PCB-PINS-001"]})
    ids = {r["id"]: r for r in proj["requirements"]}
    assert ids["PCB-WIDTH-001"]["gate"] == "none" and ids["PCB-WIDTH-001"]["origin"] == "base+project"
    assert ids["PCB-WIDTH-001"]["source"]["kind"] == "standard"  # inherited from the base
    assert ids["PRJ-1"]["verifier"] == "ASSERT"
    ex = {e["id"]: e["reason"] for e in proj["excluded"]}
    assert ex["PCB-HOLE-001"] == "no enclosure" and ex["PCB-PINS-001"] is None
    assert "FAB-BOM-001" in ex and "FAB-CPL-001" not in ids
    assert any(x["id"] == "PCB-PINS-001" for x in cli._lint(proj))


def test_design_hash_includes_rules(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)")
    h1 = config.design_hash(tmp_path)
    (tmp_path / "b.kicad_dru").write_text("(version 1)")
    h2 = config.design_hash(tmp_path)
    (tmp_path / "b.kicad_prl").write_text("ui state")
    assert h1 != h2 == config.design_hash(tmp_path)


def test_cli_version_and_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == f"kicad-verify {kicadverify.__version__}"
    sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    assert len(sub._choices_actions) == len(sub.choices) >= 18
    assert all(a.help for a in sub._choices_actions), [a.dest for a in sub._choices_actions if not a.help]


def test_init_ci_workflow(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hw").mkdir()
    (tmp_path / "hw" / "b.kicad_pro").write_text("{}")
    created = config.init_project(tmp_path / "hw", ci="github")
    wf = tmp_path / ".github" / "workflows" / "hw-verify.yml"
    assert wf in created
    text = wf.read_text()
    assert "kicadverify verify hw --gate fab" in text and "{{path}}" not in text
    assert f"kicad-verify@v{kicadverify.__version__}" in text and "{{version}}" not in text
    assert yaml.safe_load(text)["jobs"]["verify"]["steps"]
    assert (tmp_path / "hw" / "verification" / "pcb" / "project.yaml").read_text().startswith('name: "hw"')


# ------------------------------------------------------------------ outputs, manifest
def _rep(tmp_path):
    reqs = [req("A"), req("B"), req("C", gate="release", method="human")]
    res = [Result("A", PASS, "ok", evidence=[tmp_path / "b.kicad_pcb"]),
           Result("B", FAIL, "bad", violations=[{"key": "k", "text": "broken | pipe"}])]
    ver = rq.verification(reqs, [{"id": "Z", "reason": "n/a"}], res, tmp_path, "full", {})
    return report.build("proj", "full", res, {"kicad-verify": "t", "kicad-cli": "t"},
                        {"design_hash": "ab" * 32, "verification": ver})


def test_outputs(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)")
    rep = _rep(tmp_path)
    md = outputs.markdown(rep)
    assert "## Traceability matrix" in md and "broken \\| pipe" in md and "| Z | n/a |" in md
    assert "`b.kicad_pcb`" in md
    x = ET.fromstring(outputs.junit(rep).split("?>", 1)[1])
    auto = next(s for s in x if s.get("name") == "requirements.auto")
    assert (auto.get("tests"), auto.get("failures")) == ("2", "1")
    assert next(s for s in x if s.get("name") == "requirements.human").get("skipped") == "1"
    txt = report.text(rep, gate="dev")
    assert "Gate dev: BLOCKED by 1" in txt and "B " in txt


def test_manifest_and_audit(tmp_path):
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)")
    (tmp_path / "fab").mkdir()
    (tmp_path / "fab" / "b-F_Cu.gbr").write_text("G04*")
    config.init_project(tmp_path)
    proj = config.load_project(tmp_path)
    man = manifest.build(tmp_path, proj, _rep(tmp_path))
    manifest.write(tmp_path, proj, man)
    assert set(man["files"]["design"]) == {"b.kicad_pcb", "b.kicad_pro"}
    assert manifest.audit(tmp_path, proj)[0] == []
    (tmp_path / "fab" / "b-F_Cu.gbr").write_text("G04 changed*")
    (tmp_path / "fab" / "b-B_Cu.gbr").write_text("G04*")
    diffs = {(g, p, d) for g, p, d in manifest.audit(tmp_path, proj)[0]}
    assert ("fabrication", "fab/b-F_Cu.gbr", "changed") in diffs
    assert ("fabrication", "fab/b-B_Cu.gbr", "not in the manifest") in diffs


def test_coverage_doc_is_generated():
    assert (ROOT / "docs" / "COVERAGE.md").read_text(encoding="utf-8") == cli.checks_markdown()


def test_registry_covers_every_base_check():
    reg = rq.registry()
    for c in reg.values():
        assert c.get("title") and c.get("covers") and c.get("limits") and c.get("modes")
    assert {"FAB-RULES-001", "FAB-DFM-001", "REVIEW", "SIGNOFF", "ASSERT"} <= set(reg)
    assert not_verifiable("X", "d").outcome == NOT_VERIFIABLE


# ------------------------------------------------------------------ provenance and attestations
import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402

from kicadverify import attest, provenance  # noqa: E402


def _prov_project(tmp_path):
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)")
    (tmp_path / "fab").mkdir()
    (tmp_path / "fab" / "b-F_Cu.gbr").write_text("G04*")
    config.init_project(tmp_path)
    return config.load_project(tmp_path)


def test_policy_digest_is_deterministic_and_attributes_changes(tmp_path):
    proj = _prov_project(tmp_path)
    p1, p2 = provenance.policy(proj), provenance.policy(config.load_project(tmp_path))
    assert p1["digest"] == p2["digest"] and p1["digest"].startswith("sha256:")
    (tmp_path / "verification" / "pcb" / "waivers.yaml").write_text(yaml.safe_dump(
        {"waivers": [{"check": "X", "key": "k", "reason": "r", "date": "2026-01-01"}]}))
    p3 = provenance.policy(config.load_project(tmp_path))
    assert p3["digest"] != p1["digest"]
    assert [k for k in p1["components"] if p1["components"][k] != p3["components"][k]] == ["waivers"]


def test_intent_parsing(tmp_path):
    proj = _prov_project(tmp_path)
    i = provenance.intent(tmp_path, proj)
    assert i["present"] and not i["declared"] and len(i["missing"]) == 7 and not i["complete"]
    (tmp_path / "verification" / "pcb" / "design_intent.md").write_text(
        "# Intent\n- Purpose of the board: relay\n- Supply input (voltage, source): 5 V USB\n- Loads:\n")
    j = provenance.intent(tmp_path, proj)
    assert j["declared"] == {"Purpose of the board": "relay", "Supply input (voltage, source)": "5 V USB"}
    assert j["missing"] == ["Loads"] and j["digest"] != i["digest"]
    r = cli.intent_check({"intent": j}, tmp_path)
    assert r.coverage["checked"] == 2 and r.coverage["total"] == 3
    assert verdict([req("GEN-INTENT-001")], [r])["GEN-INTENT-001"]["status"] == NOT_VERIFIABLE


def test_review_is_invalidated_by_intent_change(tmp_path):
    proj = _prov_project(tmp_path)
    prov = provenance.collect(tmp_path, proj)
    key = cli.review_key(prov, "RH")
    review.write_report(proj, {"model": "m"}, [], [], [Result("M", PASS, "ok")], key, {"bundle_digest": "x"})
    dh = config.design_hash(tmp_path)
    assert review.load_current(proj, dh, key)[0] is not None
    (tmp_path / "verification" / "pcb" / "design_intent.md").write_text("- Purpose of the board: something else\n")
    key2 = cli.review_key(provenance.collect(tmp_path, proj), "RH")
    res, why, _ = review.load_current(proj, dh, key2)
    assert res is None and "design intent" in why


def _rep_with_prov(tmp_path, proj):
    rep = _rep(tmp_path)
    rep["provenance"] = provenance.collect(tmp_path, proj)
    rep["design_hash"] = rep["provenance"]["design_hash"]
    return rep


def test_attestation_statement_and_check(tmp_path):
    proj = _prov_project(tmp_path)
    rep = _rep_with_prov(tmp_path, proj)
    stmt = attest.statement(rep)
    assert stmt["_type"] == attest.STATEMENT_TYPE and stmt["predicateType"] == attest.PREDICATE_TYPE
    names = {s["name"]: s["annotations"]["group"] for s in stmt["subject"]}
    assert names["b.kicad_pcb"] == "design" and names["fab/b-F_Cu.gbr"] == "fabrication"
    pred = stmt["predicate"]
    assert pred["summary"]["by_requirement"]["FAILED"] == ["B"] and pred["excluded"][0]["id"] == "Z"
    assert pred["policy"]["digest"] and pred["intent"]["digest"] and pred["tools"]["kicad-verify"]["code_digest"]
    f = attest.write(stmt, tmp_path / "att")
    assert attest.check(f, tmp_path)["holds"]
    (tmp_path / "fab" / "b-F_Cu.gbr").write_text("G04 changed*")
    (tmp_path / "fab" / "b-B_Cu.gbr").write_text("G04*")
    res = attest.check(f, tmp_path)
    assert not res["holds"] and res["subject"]["changed"] == ["fab/b-F_Cu.gbr"] and res["new"] == ["fab/b-B_Cu.gbr"]


@pytest.mark.skipif(not _shutil.which("ssh-keygen"), reason="ssh-keygen not available")
def test_signed_attestation(tmp_path):
    proj = _prov_project(tmp_path)
    key = tmp_path / "k"
    _subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ana", "-f", str(key)], check=True)
    allowed = tmp_path / "allowed_signers"
    allowed.write_text("ana@example.org " + " ".join(key.with_suffix(".pub").read_text().split()[:2]) + "\n")
    f = attest.write(attest.statement(_rep_with_prov(tmp_path, proj)), tmp_path / "att")
    attest.sign(f, key)
    res = attest.check(f, tmp_path, allowed)
    assert res["signature"] == {"valid": True, "signer": "ana@example.org"} and res["holds"]
    f.write_text(f.read_text().replace('"FAILED": 1', '"FAILED": 0'))
    res = attest.check(f, tmp_path, allowed)
    assert res["signature"]["valid"] is False and not res["holds"]


def test_signoff_bound_to_policy(tmp_path):
    proj = _prov_project(tmp_path)
    reqs = [r for r in proj["requirements"] if r["id"] == "HUM-FIT-001"]
    signoff.add(tmp_path, "HUM-FIT-001", "ana", "ok")
    pol = provenance.policy(config.load_project(tmp_path))
    assert verdict(reqs, signoff.results(tmp_path, reqs, policy=pol))["HUM-FIT-001"]["status"] == VERIFIED
    assert signoff.pending(tmp_path, reqs, pol) == []
    (tmp_path / "verification" / "pcb" / "waivers.yaml").write_text(yaml.safe_dump(
        {"waivers": [{"check": "X", "key": "k", "reason": "r", "date": "2026-01-01"}]}))
    pol2 = provenance.policy(config.load_project(tmp_path))
    v = verdict(reqs, signoff.results(tmp_path, reqs, policy=pol2))["HUM-FIT-001"]
    assert v["status"] == NOT_RUN and "changed: waivers" in v["reason"]
    assert signoff.pending(tmp_path, reqs, pol2) == ["HUM-FIT-001"]
    legacy = tmp_path / "verification" / "pcb" / "signoff.yaml"
    data = yaml.safe_load(legacy.read_text())
    data["signoffs"][0].pop("policy_digest")
    legacy.write_text(yaml.safe_dump(data))
    v = verdict(reqs, signoff.results(tmp_path, reqs, policy=pol2))["HUM-FIT-001"]
    assert v["status"] == NOT_RUN and "no policy binding" in v["reason"]


# ------------------------------------------------------------------ isolation barriers
from kicadverify.checks import isolation  # noqa: E402

ISO_PCB = """(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (segment (start 10 10) (end 30 10) (width 1.0) (layer "F.Cu") (net "/L"))
 (segment (start 10 13) (end 30 13) (width 0.5) (layer "F.Cu") (net "GND"))
 (segment (start 10 20) (end 30 20) (width 1.0) (layer "B.Cu") (net "/N"))
 (footprint "C:C" (layer "F.Cu") (at 40 10) (property "Reference" "J1")
  (pad "1" thru_hole circle (at 0 0) (size 2 2) (drill 1) (layers "*.Cu") (net "/L"))
  (pad "2" thru_hole circle (at 0 6) (size 2 2) (drill 1) (layers "*.Cu") (net "+5V")))
 (gr_rect (start 0 0) (end 50 30) (layer "Edge.Cuts")))"""


@pytest.fixture
def iso_board(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(ISO_PCB)
    (tmp_path / "b.kicad_pro").write_text(json.dumps({"net_settings": {"netclass_patterns": [
        {"netclass": "MAINS", "pattern": "/L"}, {"netclass": "MAINS", "pattern": "N"}]}}))
    return board.load(tmp_path / "b.kicad_pcb"), tmp_path / "b.kicad_pro"


def _iso(b, pro, barriers):
    return isolation.run(b, pro, "t", {"isolation": {"barriers": barriers}})[0]


def test_isolation_netclass_selection(iso_board):
    b, pro = iso_board
    ex, pat = isolation.netclasses(pro)
    assert isolation.netclass_of("/N", ex, pat) == "MAINS" and isolation.netclass_of("GND", ex, pat) == "Default"
    # /L track edge at 10.5, GND track edge at 12.75: 2.25 mm on F.Cu; J1 pads 6 - 2 = 4 mm apart
    r = _iso(b, pro, [{"name": "mains-LV", "a": {"netclass": "MAINS"}, "required_mm": 3.0}])
    assert r.status == FAIL and "2.25 mm" in r.violations[0]["text"] and "F.Cu" in r.violations[0]["text"]
    r = _iso(b, pro, [{"name": "mains-LV", "a": {"netclass": "MAINS"}, "required_mm": 2.0}])
    assert r.status == PASS and r.coverage["checked"] == 2  # F.Cu and B.Cu
    r = _iso(b, pro, [{"name": "L-N", "a": {"nets": ["/L"]}, "b": {"nets": ["~/?N"]}, "required_mm": 5}])
    assert r.status == PASS  # /L and /N only meet on B.Cu through J1.1: 20 - 10 - 1 - 0.5 = 8.5 mm


def test_isolation_on_a_kicad9_board(iso_board, tmp_path):
    """KiCad 9 writes track nets as numbers resolved through the net table: the same board must give
    the same separation, or the mains tracks are not selected at all."""
    _, pro = iso_board
    k9 = ISO_PCB.replace('(layers (0 "F.Cu" signal)', '(net 0 "") (net 1 "/L") (net 2 "GND") (net 3 "/N") '
                                                      '(net 4 "+5V")\n (layers (0 "F.Cu" signal)')
    for i, n in enumerate(("/L", "GND", "/N"), 1):
        k9 = k9.replace(f'(layer "{"F.Cu" if n != "/N" else "B.Cu"}") (net "{n}"))',
                        f'(layer "{"F.Cu" if n != "/N" else "B.Cu"}") (net {i}))')
    k9 = k9.replace('(net "/L"))\n  (pad "2"', '(net 1 "/L"))\n  (pad "2"').replace('(net "+5V"))', '(net 4 "+5V"))')
    assert '(net "' not in k9
    (tmp_path / "k9.kicad_pcb").write_text(k9)
    r = _iso(board.load(tmp_path / "k9.kicad_pcb"), pro,
             [{"name": "mains-LV", "a": {"netclass": "MAINS"}, "required_mm": 3.0}])
    assert r.status == FAIL and "2.25 mm" in r.violations[0]["text"]


def test_isolation_measures_arcs_not_their_chord(iso_board, tmp_path):
    """A mains arc bulging 5 mm toward a low-voltage track: its chord is 7 mm away, the arc 2 mm."""
    _, pro = iso_board
    (tmp_path / "arc.kicad_pcb").write_text("""(kicad_pcb (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (arc (start 10 10) (mid 15 15) (end 20 10) (width 0.2) (layer "F.Cu") (net "/L"))
 (segment (start 10 17) (end 20 17) (width 0.2) (layer "F.Cu") (net "GND"))
 (gr_rect (start 0 0) (end 50 30) (layer "Edge.Cuts")))""")
    b = board.load(tmp_path / "arc.kicad_pcb")
    assert len([tr for tr in b["tracks"] if tr["net"] == "/L"]) == 16
    r = _iso(b, pro, [{"name": "mains-LV", "a": {"netclass": "MAINS"}, "required_mm": 4.0}])
    assert r.status == FAIL and "1.80 mm" in r.violations[0]["text"]


def test_isolation_cutout_turns_shortfall_into_gap(iso_board, tmp_path):
    b, pro = iso_board
    slot = ISO_PCB.replace('(gr_rect (start 0 0) (end 50 30) (layer "Edge.Cuts"))',
                           '(gr_rect (start 0 0) (end 50 30) (layer "Edge.Cuts")) '
                           '(gr_rect (start 12 11.2) (end 28 11.8) (layer "Edge.Cuts"))')
    (tmp_path / "s.kicad_pcb").write_text(slot)
    bs = board.load(tmp_path / "s.kicad_pcb")
    assert isolation.has_cutouts(bs) and not isolation.has_cutouts(b)
    r = _iso(bs, pro, [{"name": "mains-LV", "a": {"netclass": "MAINS"}, "required_mm": 3.0}])
    assert r.status == PASS and r.coverage["unchecked"] and "cutouts" in r.coverage["unchecked"][0]["text"]
    assert verdict([req("ISO-SEP-001")], [r])["ISO-SEP-001"]["status"] == NOT_VERIFIABLE


def test_isolation_declared_or_not_verifiable(iso_board):
    b, pro = iso_board
    assert isolation.run(b, pro, "t", {})[0].outcome == NOT_VERIFIABLE
    r = _iso(b, pro, [{"name": "x", "a": {"nets": ["NOPE"]}, "required_mm": 1}])
    assert "selects no net" in r.coverage["unchecked"][0]["text"]
    reqs = [req("ISO-SEP-001")]
    isolation.apply_sources(reqs, {"isolation": {"barriers": [
        {"name": "m", "required_mm": 4,
         "source": {"kind": "regulatory", "ref": "IEC 62368-1 T.x", "confirmed": False}}]}})
    assert reqs[0]["source"]["confirmed"] is False and "IEC 62368-1" in reqs[0]["source"]["ref"]
