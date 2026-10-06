"""Unit tests that need neither KiCad nor network."""
from pathlib import Path

from kicadverify import netlist, review, sexp, waivers
from kicadverify.checks import circuit
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


def test_judge_rules(tmp_path):
    (tmp_path / "d.json").write_text("U1 VO +3V3 regulator output", encoding="utf-8")
    reqs = [{"id": "A"}, {"id": "B"}, {"id": "C"}, {"id": "D"}]
    raw = {"verdicts": [
        {"id": "A", "verdict": "PASS", "summary": "ok", "evidence": [{"file": "d.json", "quote": "U1 VO +3V3 regulator"}]},
        {"id": "B", "verdict": "PASS", "summary": "ok", "evidence": [{"file": "d.json", "quote": "invented quote here"}]},
        {"id": "C", "verdict": "FAIL", "summary": "bad", "evidence": []},
    ], "extra_findings": []}
    res, _, _ = review.judge(raw, reqs, tmp_path, tmp_path)
    st = {r.check_id: r.status for r in res}
    assert st == {"A": PASS, "B": FAIL, "C": WARN, "D": FAIL}
