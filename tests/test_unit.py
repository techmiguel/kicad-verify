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
    # a QFN whose exposed pad (pin 33, GND) is missing from the footprint: pin 1 is GND too, on one pad
    qpins = {"1": {"name": "GND", "type": "power_in"}, "2": {"name": "IO", "type": "bidirectional"},
             "33": {"name": "GND", "type": "power_in"}}
    qnl = {"components": {"U1": {"value": "MCU", "footprint": "Q:QFN-32", "pins": qpins}},
           "pin_net": {("U1", "1"): "GND", ("U1", "2"): "/IO", ("U1", "33"): "GND"}}
    qfn = {"footprints": [_fp("U1", "Q:QFN-32", [("1", "GND", "smd"), ("2", "/IO", "smd")], value="MCU")]}
    r = parity.run(qfn, qnl, "t")[1]
    assert r.status == FAIL and "U1: symbol pin 33 (GND) has no pad" in r.violations[0]["text"]
    # a pin unconnected in the schematic without a pad (one header row of a module not used): a
    # warning to confirm, not a lost connection
    qnl["pin_net"][("U1", "33")] = "unconnected-(U1-GND-Pad33)"
    qfn["footprints"][0]["pads"][0]["net"] = "GND"
    r = parity.run(qfn, qnl, "t")[1]
    assert r.status == WARN and r.violations[0]["text"].endswith("(the pin is unconnected in the schematic)")


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
    res = {r.check_id: r for r in board.run(b, "t", {"root": tmp_path, "params": {"min_power_width_mm": 0.25},
                                                       "pins": {}})}
    # before: tracks were on nets "2" and "1", so no power net was found and the width check passed
    assert [v["text"] for v in res["PCB-WIDTH-001"].violations] == ["+5V: 0.15 mm (thin tracks reach R1.1)"]
    # the In1.Cu track under the screw head is not in the keep-out; the B.Cu track and the via are
    assert sorted(v["text"] for v in res["PCB-KEEPOUT-001"].violations) == [
        "H1: track +5V (B.Cu) inside the Ø6.0 mm keep-out", "H1: via GND inside the Ø6.0 mm keep-out"]


def test_padnet_leaves_pads_without_a_symbol_pin_to_pinmap():
    from kicadverify.checks import board
    # SCD41: the symbol has the used pins only; DNC pads 1-5 have no pin and no net
    fp = _fp("U5", "Sensor:SCD4x", [(str(n), None, "smd") for n in range(1, 6)] + [("6", "GND", "smd"),
                                                                                    ("7", None, "smd")])
    nl = {"components": {"U5": {"pins": {"6": {}, "7": {}}}}, "pin_net": {}}
    b = {"footprints": [fp], "tracks": [], "vias": []}
    res = {r.check_id: r for r in board.run(b, "t", {"root": ".", "params": {}, "pins": {}}, nl=nl)}
    assert [v["text"] for v in res["PCB-PADNET-001"].violations] == ["U5.7"]  # a pin of the symbol, no net
    res = {r.check_id: r for r in board.run(b, "t", {"root": ".", "params": {}, "pins": {}})}
    assert len(res["PCB-PADNET-001"].violations) == 6  # without the schematic every open pad is reported


def test_evidence_paths_are_relative_also_beside_the_project(tmp_path):
    from kicadverify import evidence
    (tmp_path / "hardware" / "kicad").mkdir(parents=True)
    (tmp_path / "hardware" / "fab").mkdir()
    f = tmp_path / "hardware" / "fab" / "x.zip"
    f.write_bytes(b"x")
    root = tmp_path / "hardware" / "kicad"
    assert evidence.rel(f, root) == "../fab/x.zip"  # the same key on every checkout
    assert evidence.rel(root / "a.kicad_pcb", root) == "a.kicad_pcb"


def test_profile_from_fab_kicad_rules(tmp_path, capsys):
    from kicadverify import cli, fabimport
    # a KiCad 5 fab template (as the OSH Park 4-layer one): renamed copper layers, setup minima and the
    # fab minimum in a net class
    pcb = tmp_path / "Fab-4Layer.kicad_pcb"
    pcb.write_text("""(kicad_pcb (version 20171130)
 (layers (0 Front signal) (1 In1.Cu signal) (2 In2.Cu signal) (31 Back signal) (44 Edge.Cuts user))
 (setup (trace_min 0.127) (via_min_size 0.4572) (via_min_drill 0.254) (edge_clearance 0))
 (net_class Default "default" (clearance 0.2) (trace_width 0.25))
 (net_class Min "fab minimum" (clearance 0.127) (trace_width 0.127)))""")
    prof, notes = fabimport.profile_from(pcb, "fab-4")
    assert {k: v for k, v in prof.items() if k != "source"} == {
        "name": "fab-4", "layers_max": 4, "min_spacing_mm": 0.127, "min_track_mm": 0.127,
        "min_via_annular_mm": 0.1016, "min_via_diameter_mm": 0.4572, "min_via_drill_mm": 0.254}
    assert notes["min_via_annular_mm"].startswith("derived") and prof["source"]["confirmed"] is False
    # custom rules: only those without a condition are fab-wide minima; mil converted
    dru = tmp_path / "fab.kicad_dru"
    dru.write_text("""(version 1)
(rule "Fab min track" (constraint track_width (min 5mil)))
(rule "Fab holes" (constraint hole_size (min 0.3mm)) (constraint hole_to_hole (min 0.5mm)))
(rule "Power only" (condition "A.NetClass == 'PWR'") (constraint track_width (min 1mm)))""")
    prof, _ = fabimport.profile_from(dru)
    assert prof["min_track_mm"] == 0.127 and prof["min_via_drill_mm"] == 0.3 == prof["min_pth_drill_mm"]
    assert prof["min_hole_to_hole_mm"] == 0.5
    # a KiCad 6+ project: zeros mean "not set"
    pro = tmp_path / "fab.kicad_pro"
    pro.write_text('{"board": {"design_settings": {"rules": {"min_clearance": 0.1, "min_track_width": 0.09,'
                   ' "min_copper_edge_clearance": 0.0, "min_through_hole_diameter": 0.2}}}}')
    prof, _ = fabimport.profile_from(pro)
    assert "min_copper_to_edge_mm" not in prof and prof["min_spacing_mm"] == 0.1
    assert cli.main(["profiles", "--from", str(pcb), "--name", "fab-4"]) == 0
    out = capsys.readouterr().out
    assert "name: fab-4" in out and "not in the file" in out and "min_copper_to_edge_mm" in out


def test_dfm_tolerance_of_a_tenth_of_a_mil():
    from kicadverify.checks import dfm
    prof = {"name": "t", "source": {"ref": "t"}, "min_via_annular_mm": 0.1016}  # 4 mil
    b = {"copper_layers": ["F.Cu", "B.Cu"], "thickness": 1.6, "outline_bbox": None, "tracks": [],
         "footprints": [], "edge_segments": [],
         "vias": [{"net": "A", "pos": (0, 0), "drill": 0.3, "size": 0.5},     # 0.1 mm ring: 1.6 µm short
                  {"net": "A", "pos": (5, 0), "drill": 0.3, "size": 0.49}]}   # 0.095 mm: really short
    r = dfm.geometry(b, "t", prof)[0]
    assert [v["text"].split(":")[1].strip() for v in r.violations] == ["annular ring 0.095 mm < 0.1016 mm"]


def _link_nl(conn, pins, extra):
    """Netlist of a board: connector `conn` with {pin: net}, plus (ref, pin, net, type) pins of other parts."""
    def comp(value, pins_):
        return {"ref": "", "value": value, "footprint": "", "lib": "", "part": value, "description": "",
                "datasheet": "", "fields": {}, "dnp": False, "pins": pins_}
    comps = {conn: comp("Conn", {p: {"name": p, "type": "passive"} for p in pins})}
    nets = {}
    for p, n in pins.items():
        if n:
            nets.setdefault(n, []).append({"ref": conn, "pin": p, "type": "passive"})
    for ref, p, n, ty in extra:
        comps.setdefault(ref, comp("IC", {}))["pins"][p] = {"name": p, "type": ty}
        nets.setdefault(n, []).append({"ref": ref, "pin": p, "type": ty})
    pin_net = {(x["ref"], x["pin"]): n for n, xs in nets.items() for x in xs}
    return {"components": comps, "nets": nets, "pin_net": pin_net}


def test_board_to_board_link(tmp_path, monkeypatch):
    from kicadverify.checks import interconnect
    (tmp_path / "sensor").mkdir()
    for ext in (".kicad_pro", ".kicad_sch"):
        (tmp_path / "sensor" / ("sensor" + ext)).write_text("{}")
    main = _link_nl("J3", {"1": "+5V", "2": "GND", "3": "/SDA", "4": "/TX", "5": "/INT", "6": None},
                    [("U1", "1", "+5V", "power_in"), ("U1", "2", "GND", "power_in"),
                     ("U1", "3", "/SDA", "bidirectional"), ("U1", "4", "/TX", "output"), ("U1", "5", "/INT", "input")])
    mate = _link_nl("P1", {"1": "+3V3", "2": "GND", "3": "/I2C_SCL", "4": "/TX", "5": None, "6": None},
                    [("U9", "1", "+3V3", "power_in"), ("U9", "2", "GND", "power_in"),
                     ("U9", "3", "/I2C_SCL", "bidirectional"), ("U9", "4", "/TX", "output")])
    monkeypatch.setattr(interconnect.netlist, "load", lambda sch, cache=None: mate)
    link = {"name": "sensor", "connector": "J3", "mate": {"board": "sensor/sensor.kicad_pro", "connector": "P1"}}
    r = interconnect.run(main, tmp_path, "t", {"interconnects": [link]}, tmp_path)[0]
    texts = [v["text"].split(": ", 2)[-1] for v in r.violations]
    assert r.status == FAIL and texts == [
        "5 V against 3.3 V",                                       # +5V into the sensor's 3.3 V rail
        "an output pin drives the net on each board",              # TX to TX: not crossed
        "net names differ",                                        # SDA against I2C_SCL
        "the mating pin is not connected"]                         # /INT ends on nothing
    assert r.coverage["total"] == 6
    # reversed connector: pin 1 meets pin 6 and so on
    link["mapping"] = "reverse"
    r = interconnect.run(main, tmp_path, "t", {"interconnects": [link]}, tmp_path)[0]
    assert any("J3.1 (+5V) mates sensor:P1.6" in v["text"] for v in r.violations)
    # different pin counts cannot be mapped straight
    mate["components"]["P1"]["pins"].pop("6")
    link["mapping"] = "straight"
    r = interconnect.run(main, tmp_path, "t", {"interconnects": [link]}, tmp_path)[0]
    assert "6 electrical pins against 5 on the mate" in r.violations[0]["text"]
    # a missing mate is a coverage gap, not a pass
    link["mate"]["board"] = "nope/nope.kicad_pro"
    r = interconnect.run(main, tmp_path, "t", {"interconnects": [link]}, tmp_path)[0]
    assert r.coverage["unchecked"] and "not an existing .kicad_pro" in r.coverage["unchecked"][0]["text"]
    # nothing declared: no result, and the requirement is excluded with its reason
    assert interconnect.run(main, tmp_path, "t", {}, tmp_path) == []


def test_board_to_board_link_on_carrier_boards(tmp_path, monkeypatch):
    """Carrier boards for modules: only connector (passive) pins on the nets, so roles come from the
    names. A reversed header puts ground on the I2C lines (HiveHub scale module and power module)."""
    from kicadverify.checks import interconnect
    (tmp_path / "pm").mkdir()
    for ext in (".kicad_pro", ".kicad_sch"):
        (tmp_path / "pm" / ("pm" + ext)).write_text("{}")
    scale = _link_nl("J19", {"1": "GND", "2": "GND", "3": "/3.3V", "4": "/3.3V", "5": "/D4", "6": "/D5"},
                     [("J15", "1", "/3.3V", "passive"), ("J15", "2", "GND", "passive"), ("J15", "3", "/D4", "passive"),
                      ("J15", "4", "/D5", "passive")])
    power = _link_nl("J20", {"1": "/BATneg", "2": "/BATneg", "3": "/3.3V", "4": "/3.3V", "5": "/SDA", "6": "/SCL"},
                     [("J21", "1", "/3.3V", "passive"), ("J21", "2", "/BATneg", "passive"),
                      ("J21", "3", "/SCL", "passive"), ("J21", "4", "/SDA", "passive")])
    monkeypatch.setattr(interconnect.netlist, "load", lambda sch, cache=None: power)
    link = {"name": "pm", "connector": "J19", "mate": {"board": "pm/pm.kicad_pro", "connector": "J20"}}
    r = interconnect.run(scale, tmp_path, "t", {"interconnects": [link]}, tmp_path)[0]
    # GND against /BATneg is ground on both sides; D4/SDA only differ by name (a warning to confirm)
    assert r.status == WARN and [v["text"].rsplit(": ", 1)[-1] for v in r.violations] == ["net names differ"] * 2
    link["mapping"] = "reverse"
    r = interconnect.run(scale, tmp_path, "t", {"interconnects": [link]}, tmp_path)[0]
    assert r.status == FAIL and [v["text"].rsplit(": ", 1)[-1] for v in r.violations] == [
        "a ground against a signal", "a ground against a signal", "a signal against a ground",
        "a signal against a ground"]
