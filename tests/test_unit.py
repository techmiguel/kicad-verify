"""Unit tests that need neither KiCad nor network."""

from kicadverify import netlist, review, sexp, waivers
from kicadverify.checks import circuit, fab, parity
from kicadverify.report import FAIL, PASS, WARN, Result


def test_sexp_parses_strings_and_escapes():
    t = sexp.parse('(a "x y" (b 1 2) (c "q\\"r"))')
    assert t[0] == "a" and t[1] == "x y" and sexp.child(t, "b") == ["b", "1", "2"]
    assert sexp.child(t, "c")[1] == 'q"r'


def test_parse_value():
    assert netlist.parse_value("4k7") == 4700
    assert abs(netlist.parse_value("100nF") - 1e-7) < 1e-15
    assert netlist.parse_value("220") == 220
    assert netlist.parse_value("10M") == 10e6
    assert netlist.parse_value("R47") is None or abs(netlist.parse_value("R47") - 0.47) < 1e-9


def test_same_value():
    f = netlist.same_value
    assert f("4k7", "4.7k") and f("4700", "4k7") and f("100n", "100nF") and f("220", "220Ω") and f("10K", "10k")
    assert f("470uF 10V", "470uF  10v") and f("AMS1117-3.3", "AMS1117-3.3") and f("", "")
    assert f("10k 1%", "10k 1 %") and f("100nF/50V", "100n 50V") and f("2k2", "2.2k") and f("1µF", "1uF")
    assert not f("220", "330") and not f("1M", "1m") and not f("470uF 10V", "470uF 4V")
    assert not f("AMS1117-3.3", "AMS1117-1.8") and not f("10k", "")


def test_parity_compares_values():
    pins = {"1": {"name": "", "type": "passive"}, "2": {"name": "", "type": "passive"}}
    nl = {"components": {"R9": {"value": "220", "footprint": "R:R_0603", "pins": pins},
                         "R1": {"value": "10k", "footprint": "R:R_0603", "pins": pins}},
          "pin_net": {("R9", "1"): "A", ("R9", "2"): "B", ("R1", "1"): "A", ("R1", "2"): "C"}}

    def board(r9, r1):
        def fp(ref, val, n2):
            return {"ref": ref, "name": "R:R_0603", "value": val, "attr": [],
                    "pads": [{"number": "1", "net": "A", "type": "smd"}, {"number": "2", "net": n2, "type": "smd"}]}
        return {"footprints": [fp("R9", r9, "B"), fp("R1", r1, "C")]}

    ok = parity.run(board("220Ω", "10K"), nl, "t")[0]
    assert ok.status == PASS, ok.violations
    bad = parity.run(board("330", "10k"), nl, "t")[0]
    assert bad.status == FAIL and [v["text"] for v in bad.violations] == ["R9: value 330 on PCB vs 220 in schematic"]


def test_rated_voltage():
    assert netlist.rated_voltage({"value": "22uF 10V", "fields": {}}) == 10
    assert netlist.rated_voltage({"value": "100nF", "fields": {"Voltage": "50V"}}) == 50


def test_net_voltage_from_name():
    f = circuit.net_voltage_from_name
    assert f("+3V3") == 3.3 and f("/+5V") == 5 and f("GND") == 0 and f("AGND") == 0
    assert f("VBUS") == 5 and f("+1V8_RF") == 1.8 and f("/RELAY") is None and f("Net-(D2-K)") is None


def test_fixed_regulator_vout():
    f = circuit.fixed_regulator_vout
    assert f("AMS1117-3.3") == 3.3 and f("AMS1117-1.8") == 1.8 and f("LM7805") == 5
    assert f("XC6206P332MR") == 3.3 and f("HT7333") == 3.3 and f("ESP-12F") is None


def _nl(comps, nets):
    pin_net = {(n["ref"], n["pin"]): name for name, nodes in nets.items() for n in nodes}
    for r, c in comps.items():
        c.setdefault("ref", r)
        c.setdefault("fields", {})
        c.setdefault("description", "")
        c.setdefault("datasheet", "")
        c.setdefault("lib", "Device")
    return {"components": comps, "nets": nets, "pin_net": pin_net}


def _led_board(anode_net, cathode_net, r_value="1k"):
    comps = {
        "D1": {"value": "LED red", "part": "LED", "pins": {"1": {"name": "K", "type": "passive"},
                                                           "2": {"name": "A", "type": "passive"}}},
        "R1": {"value": r_value, "part": "R", "pins": {"1": {"name": "", "type": "passive"},
                                                       "2": {"name": "", "type": "passive"}}},
    }
    nets = {"+3V3": [{"ref": "R1", "pin": "1", "name": "", "type": "passive"}],
            "Net-(D1-A)": [{"ref": "R1", "pin": "2", "name": "", "type": "passive"}],
            "GND": []}
    nets[anode_net].append({"ref": "D1", "pin": "2", "name": "A", "type": "passive"})
    nets[cathode_net].append({"ref": "D1", "pin": "1", "name": "K", "type": "passive"})
    return _nl(comps, nets)


def _status(results, cid):
    return next(r.status for r in results if r.check_id == cid)


def test_led_polarity_and_current():
    ok, _ = circuit.run(_led_board("Net-(D1-A)", "GND"), "t", {})
    assert _status(ok, "CIR-POL-001") == PASS and _status(ok, "CIR-LED-001") == PASS
    rev, _ = circuit.run(_led_board("GND", "Net-(D1-A)"), "t", {})
    assert _status(rev, "CIR-POL-001") == FAIL
    hot, _ = circuit.run(_led_board("Net-(D1-A)", "GND", "10"), "t", {})
    assert _status(hot, "CIR-LED-001") == FAIL


def _rgb_board(r_value="1k"):
    """Common-anode RGB LED on +3V3, one resistor per cathode to a GPIO; a 10k pull-up also on +3V3."""
    pas = {"name": "", "type": "passive"}
    comps = {"D1": {"value": "LED_RGB_CA", "part": "LED_RGB", "description": "RGB LED, red/green/blue/anode",
                    "pins": {"1": {"name": "RK", "type": "passive"}, "2": {"name": "GK", "type": "passive"},
                             "3": {"name": "BK", "type": "passive"}, "4": {"name": "A", "type": "passive"}}},
             "R9": {"value": "10k", "part": "R", "pins": {"1": dict(pas), "2": dict(pas)}},
             "U1": {"value": "MCU", "part": "MCU", "pins": {str(i): {"name": f"IO{i}", "type": "bidirectional"}
                                                            for i in range(1, 5)}}}
    nets = {"+3V3": [{"ref": "D1", "pin": "4", "name": "A", "type": "passive"},
                     {"ref": "R9", "pin": "1", **pas}],
            "/EN": [{"ref": "R9", "pin": "2", **pas},
                    {"ref": "U1", "pin": "4", "name": "IO4", "type": "bidirectional"}]}
    for i, (col, ref) in enumerate((("R", "R10"), ("G", "R11"), ("B", "R12")), start=1):
        comps[ref] = {"value": r_value, "part": "R", "pins": {"1": dict(pas), "2": dict(pas)}}
        nets[f"/LED_{col}K"] = [{"ref": "D1", "pin": str(i), "name": f"{col}K", "type": "passive"},
                                {"ref": ref, "pin": "2", **pas}]
        nets[f"/LED_{col}"] = [{"ref": ref, "pin": "1", **pas},
                               {"ref": "U1", "pin": str(i), "name": f"IO{i}", "type": "bidirectional"}]
    return _nl(comps, nets)


def test_rgb_led_one_channel_per_cathode():
    res, _ = circuit.run(_rgb_board(), "t", {})
    led = next(r for r in res if r.check_id == "CIR-LED-001")
    assert led.coverage["total"] == 3 and not led.coverage["unchecked"]
    assert _status(res, "CIR-POL-001") == PASS and led.status in (PASS, WARN)
    assert not any("R9" in v["text"] for v in led.violations)  # the pull-up on the anode rail is not in series
    hot, _ = circuit.run(_rgb_board("10"), "t", {})
    assert _status(hot, "CIR-LED-001") == FAIL


def test_routed_slots_are_not_round_holes(tmp_path):
    drl = tmp_path / "x-NPTH.drl"
    drl.write_text("M48\nMETRIC\nT1C0.580\nT2C0.660\n%\nG90\nG05\nT2\nX96.25Y-116.0\n"
                   "T1\nX103.54Y-116.0G85X103.96Y-116.0\nG05\nM30\n")
    holes, decimal = fab._drill_holes([drl])
    assert holes == [(0.66, 96.25, -116.0)] and decimal


def test_regulator_mismatch():
    comps = {"U1": {"value": "AMS1117-1.8", "part": "AMS1117", "pins": {
        "1": {"name": "GND", "type": "power_in"}, "2": {"name": "VO", "type": "power_out"},
        "3": {"name": "VI", "type": "power_in"}}}}
    nets = {"GND": [{"ref": "U1", "pin": "1", "name": "GND", "type": "power_in"}],
            "+3V3": [{"ref": "U1", "pin": "2", "name": "VO", "type": "power_out"}],
            "+5V": [{"ref": "U1", "pin": "3", "name": "VI", "type": "power_in"}]}
    res, _ = circuit.run(_nl(comps, nets), "t", {})
    assert _status(res, "CIR-REG-001") == FAIL


def test_waiver_silences_one_finding_only():
    r = Result("X", FAIL, "d", violations=[{"key": "a", "text": "A"}, {"key": "b", "text": "B"}])
    waivers.apply([r], [{"check": "X", "key": "a", "reason": "ok", "date": "2026-01-01"}])
    assert r.status == FAIL and [v["key"] for v in r.violations] == ["b"]
    waivers.apply([r], [{"check": "X", "key": "b", "reason": "ok", "date": "2026-01-01"}])
    assert r.status == PASS


def test_evidence_must_be_verbatim(tmp_path):
    (tmp_path / "f.txt").write_text('pin "EN" connected to\n  R1 pull-up', encoding="utf-8")
    ok, _ = review.check_evidence({"file": "f.txt", "quote": "EN connected to R1 pull-up"}, tmp_path, tmp_path)
    assert ok
    bad, why = review.check_evidence({"file": "f.txt", "quote": "EN connected to R2 pull-up"}, tmp_path, tmp_path)
    assert not bad and "not found" in why
    short, _ = review.check_evidence({"file": "f.txt", "quote": "EN"}, tmp_path, tmp_path)
    assert not short


def _fp(ref, name, pads, attr=(), value=""):
    return {"ref": ref, "name": name, "value": value, "attr": list(attr), "dnp": False, "models": [],
            "pads": [{"number": n, "net": net, "type": ty, "drill": 1.0 if "hole" in ty else None, "drill2": None,
                      "x": 0.0, "y": 0.0, "size": (1.0, 1.0), "layers": ["F.Cu"]} for n, net, ty in pads]}


def test_pinmap_accepts_symbol_pins_drawn_on_a_shared_pad():
    # a 4-pin push-button symbol (1/4 and 2/3 are the two contacts) on the 2-contact KMR2 footprint
    pins = {n: {"name": n, "type": "passive"} for n in "1234"}
    nl = {"components": {"SW1": {"value": "SW", "footprint": "B:KMR2", "pins": pins}},
          "pin_net": {("SW1", "1"): "GND", ("SW1", "4"): "GND", ("SW1", "2"): "/EN", ("SW1", "3"): "/EN"}}
    ok = {"footprints": [_fp("SW1", "B:KMR2", [("1", "GND", "smd"), ("1", "GND", "smd"),
                                                ("2", "/EN", "smd"), ("2", "/EN", "smd")], value="SW")]}
    r = parity.run(ok, nl, "t")[1]
    assert r.status == PASS, r.violations
    # a renumbered pad keeps its net but is no symbol pin: still a missing pad
    bad = {"footprints": [_fp("SW1", "B:KMR2", [("1", "GND", "smd"), ("2", "/EN", "smd"), ("9", "/EN", "smd")],
                              value="SW")]}
    nl["pin_net"] = {("SW1", "1"): "GND", ("SW1", "2"): "GND", ("SW1", "3"): "/EN", ("SW1", "4"): "/EN"}
    nl["components"]["SW1"]["pins"] = pins
    r = parity.run(bad, nl, "t")[1]
    assert r.status == FAIL


def test_parity_compares_kicad_derived_net_names_by_members():
    pins = {p: {"name": "VBUS", "type": "passive"} for p in ("A4", "A9", "B4", "B9")}
    pins["A1"] = {"name": "GND", "type": "passive"}
    nl = {"components": {"J3": {"value": "USB_C", "footprint": "C:USB", "pins": pins}},
          "pin_net": {**{("J3", p): "unconnected-(J3-VBUS-PadA4)" for p in ("A4", "A9", "B4", "B9")},
                      ("J3", "A1"): "GND"}}
    pads = [(p, "Net-(J3-VBUS-PadA4)", "smd") for p in ("A4", "A9", "B4", "B9")] + [("A1", "GND", "smd")]
    r = parity.run({"footprints": [_fp("J3", "C:USB", pads, value="USB_C")]}, nl, "t")[0]
    assert r.status == PASS, r.violations
    # the same name change with one pin moved to another net is a real difference
    pads[0] = ("A4", "GND", "smd")
    r = parity.run({"footprints": [_fp("J3", "C:USB", pads, value="USB_C")]}, nl, "t")[0]
    assert r.status == FAIL and len(r.violations) == 4


def test_model_and_padnet_skip_bodyless_and_mounting_footprints():
    from kicadverify.checks import board
    fps = [_fp("LOGO2", "LibreSolar:LIBRESOLAR_LOGO", [], ["through_hole"]),
           _fp("U1", "bitaxe:polarity", [], ["smd"]),
           _fp("J2", "bitaxe:Tag-Connect_TC2030-IDC-NL", [("1", "/EN", "connect")]),
           _fp("H1", "MountingHole:MountingHole_3.2mm_M3_Pad", [("1", None, "thru_hole")]),
           _fp("R1", "R:R_0603", [("1", "A", "smd"), ("2", None, "smd")])]
    b = {"footprints": fps, "tracks": [], "vias": []}
    res = {r.check_id: r for r in board.run(b, "t", {"root": ".", "params": {}, "pins": {}})}
    assert [v["text"] for v in res["PCB-MODEL-001"].violations] == ["R1 (R:R_0603)"]
    assert [v["text"] for v in res["PCB-PADNET-001"].violations] == ["R1.2"]


KICAD9_PCB = """(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (net 0 "")
 (net 1 "GND")
 (net 2 "+5V")
 (footprint "MountingHole:MountingHole_3.2mm_M3" (layer "F.Cu") (at 10 10) (property "Reference" "H1")
  (pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu" "*.Mask")))
 (footprint "R:R_0603" (layer "F.Cu") (at 30 10) (property "Reference" "R1")
  (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net 2 "+5V")))
 (segment (start 30 10) (end 40 10) (width 0.15) (layer "F.Cu") (net 2))
 (segment (start 8 12) (end 14 12) (width 0.3) (layer "In1.Cu") (net 1))
 (segment (start 8 11.5) (end 14 11.5) (width 0.3) (layer "B.Cu") (net 2))
 (via (at 10 13) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)))"""


def test_kicad9_track_nets_resolve_through_the_net_table(tmp_path):
    from kicadverify.checks import board
    f = tmp_path / "k9.kicad_pcb"
    f.write_text(KICAD9_PCB, encoding="utf-8")
    b = board.load(f)
    assert [t["net"] for t in b["tracks"]] == ["+5V", "GND", "+5V"] and b["vias"][0]["net"] == "GND"
    res = {r.check_id: r for r in board.run(b, "t", {"root": tmp_path, "params": {}, "pins": {}})}
    # before: tracks were on nets "2" and "1", so no power net was found and the width check passed
    assert [v["text"] for v in res["PCB-WIDTH-001"].violations] == ["+5V: 0.15 mm (thin tracks reach R1.1)"]
    # the In1.Cu track under the screw head is not in the keep-out; the B.Cu track and the via are
    assert sorted(v["text"] for v in res["PCB-KEEPOUT-001"].violations) == [
        "H1: track +5V (B.Cu) inside the Ø6.0 mm keep-out", "H1: via GND inside the Ø6.0 mm keep-out"]
