"""Declarative assertions: project requirements verified by a deterministic check written in YAML.

    - id: PRJ-PWR-001
      text: "The ESP module is powered from +3V3"
      source: {kind: datasheet, ref: "ESP-12F datasheet, pin 8 VCC"}
      check: {type: pin_net, ref: U2, pin: 8, net: "+3V3"}

Types: pin_net, value, footprint, field, net_exists, board_size, layer_count, track_width.
String expectations starting with `~` are regular expressions (full match, case-insensitive).
"""
import re

from ..report import FAIL, PASS, Result, coverage, not_verifiable
from ..waivers import vkey

TYPES = ("pin_net", "value", "footprint", "field", "net_exists", "board_size", "layer_count", "track_width")


def _match(expected, got):
    if got is None:
        return False
    exp = str(expected)
    if exp.startswith("~"):
        return re.fullmatch(exp[1:], str(got), re.I) is not None
    return str(got).lstrip("/").upper() == exp.lstrip("/").upper()


def _comp(nl, board, ref):
    c = (nl or {}).get("components", {}).get(ref)
    fp = next((f for f in (board or {}).get("footprints", []) if f["ref"] == ref), None)
    return c, fp


def evaluate(req, nl, board, label):
    cid, spec = req["id"], req.get("check") or {}
    t = spec.get("type")
    bad, n, ev = [], 0, []

    def need(*keys):
        missing = [k for k in keys if spec.get(k) in (None, "")]
        return missing

    if t not in TYPES:
        return Result(cid, FAIL, f"{label}: unknown assertion type {t!r}")
    if t in ("pin_net", "value", "footprint"):
        if need("ref"):
            return Result(cid, FAIL, f"{label}: assertion {t} needs `ref`")
        c, fp = _comp(nl, board, spec["ref"])
        if c is None and fp is None:
            return Result(cid, FAIL, f"{label}: {spec['ref']} is not in the design",
                          violations=[{"key": vkey(cid, "missing"), "text": f"{spec['ref']} not found"}])
        n = 1
        if t == "pin_net":
            pin = str(spec.get("pin"))
            # both sides are checked: a pad left without a net on the PCB fails even when the
            # schematic has the pin on the required net (the board was not updated)
            sides = []
            if fp:
                pads = [p for p in fp["pads"] if p["number"] == pin]
                sides.append(("PCB", pads[0]["net"] if pads else None, bool(pads)))
            if nl and c is not None:
                sides.append(("schematic", nl["pin_net"].get((spec["ref"], pin)), True))
            for side, got, present in sides:
                if not present:
                    bad.append(f"{spec['ref']}.{pin}: no such pad on the PCB, required on {spec.get('net')}")
                elif not _match(spec.get("net"), got):
                    bad.append(f"{spec['ref']}.{pin}: net {got} in the {side}, required {spec.get('net')}")
        elif t == "value":
            got = (c or {}).get("value") or (fp or {}).get("value")
            if not _match(spec.get("equals") or spec.get("matches"), got):
                bad.append(f"{spec['ref']}: value {got!r}, required {spec.get('equals') or spec.get('matches')!r}")
        else:
            got = (fp or {}).get("name") or (c or {}).get("footprint")
            if not _match(spec.get("equals") or spec.get("matches"), got):
                bad.append(f"{spec['ref']}: footprint {got!r}, required {spec.get('equals') or spec.get('matches')!r}")
    elif t == "field":
        if nl is None:
            return not_verifiable(cid, f"{label}: no schematic netlist")
        if need("field"):
            return Result(cid, FAIL, f"{label}: assertion field needs `field`")
        pat = re.compile(spec.get("refs") or ".*")
        for ref, c in sorted(nl["components"].items()):
            if ref.startswith("#") or c.get("dnp") or not pat.fullmatch(ref):
                continue
            n += 1
            val = (c["fields"].get(spec["field"]) or "").strip()
            if not val or (spec.get("matches") and not _match(spec["matches"], val)):
                bad.append(f"{ref}: field {spec['field']} = {val!r}")
    elif t == "net_exists":
        n = 1
        nets = set((nl or {}).get("nets", {})) | {p["net"] for f in (board or {}).get("footprints", [])
                                                  for p in f["pads"] if p["net"]}
        if not any(_match(spec.get("net"), x) for x in nets):
            bad.append(f"net {spec.get('net')} not found")
    elif t in ("board_size", "layer_count", "track_width"):
        if board is None:
            return not_verifiable(cid, f"{label}: no PCB")
        if t == "board_size":
            bb, n = board["outline_bbox"], 1
            if not bb:
                return not_verifiable(cid, f"{label}: no board outline")
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            mx = spec.get("max")
            if mx and (w > mx[0] + 1e-6 or h > mx[1] + 1e-6):
                bad.append(f"board {w:.2f} x {h:.2f} mm > {mx[0]} x {mx[1]} mm")
        elif t == "layer_count":
            got, n = len(board["copper_layers"]), 1
            if (spec.get("equals") is not None and got != spec["equals"]) or \
                    (spec.get("max") is not None and got > spec["max"]):
                bad.append(f"{got} copper layers")
        else:
            spec_net = str(spec.get("net", "~.*"))
            pat = re.compile(spec_net[1:] if spec_net.startswith("~") else re.escape(spec_net.lstrip("/")), re.I)
            mn = float(spec.get("min", 0))
            thin = {}
            for tr in board["tracks"]:
                net = tr["net"] or ""
                if net and (pat.fullmatch(net) or pat.fullmatch(net.lstrip("/"))):
                    n += 1
                    if tr["width"] < mn - 1e-9:
                        thin[tr["net"]] = min(tr["width"], thin.get(tr["net"], 99))
            if n == 0:
                return not_verifiable(cid, f"{label}: no track on nets matching {spec.get('net')}")
            bad += [f"{net}: {w} mm < {mn} mm" for net, w in thin.items()]
    return Result(cid, FAIL if bad else PASS, f"{label}: assertion {t}: " + ("; ".join(bad[:3]) if bad else "holds"),
                  violations=[{"key": vkey(cid, b), "text": b} for b in bad], evidence=ev,
                  coverage=coverage(f"{t} items", n))


def run(reqs, nl, board, label):
    return [evaluate(r, nl, board, label) for r in reqs if r.get("check") and r["method"] == "auto"]
