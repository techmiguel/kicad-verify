"""Schematic <-> PCB parity and symbol-pin <-> footprint-pad mapping.

Own implementation because `kicad-cli pcb drc --schematic-parity` hangs (>10 min, growing memory)
with KiCad 10.0.5 on at least one real project, while the netlist export takes ~1 s.
"""
from ..netlist import norm_net, same_value
from ..report import FAIL, PASS, WARN, Result, count, coverage, not_verifiable
from ..waivers import vkey

PCB_ONLY = ("MountingHole", "Fiducial", "TestPoint", "NetTie", "Logo")
NON_ELECTRICAL_PAD = {"", "MP", "SH", "S", "0", "EP_MECH"}
AUTO_NET = ("Net-(", "unconnected-(")  # names KiCad derives from a pin; they change with connectivity


def _members(pin_net, pcb):
    """{schematic net: {(ref, pin)}} and {PCB net: {(ref, pad)}} over the schematic's parts."""
    sch, brd = {}, {}
    for (ref, pin), net in pin_net.items():
        sch.setdefault(norm_net(net), set()).add((ref, pin))
    refs = {ref for ref, _ in pin_net}
    for ref, fp in pcb.items():
        if ref in refs:
            for p in fp["pads"]:
                if p["net"]:
                    brd.setdefault(norm_net(p["net"]), set()).add((ref, p["number"]))
    return sch, brd


def run(board, nl, label):
    if nl is None:
        return [not_verifiable("PCB-PARITY-001", f"{label}: no schematic netlist"),
                not_verifiable("PCB-PINMAP-001", f"{label}: no schematic netlist")]
    comps, pin_net = nl["components"], nl["pin_net"]
    pcb = {fp["ref"]: fp for fp in board["footprints"]}
    sch_members, pcb_members = _members(pin_net, pcb)
    diffs, pinmap, n_comp, n_pins = [], [], 0, 0
    for ref, c in comps.items():
        if ref.startswith("#") or c.get("exclude_from_board"):
            continue
        n_comp += 1
        n_pins += len(c["pins"])
        if ref not in pcb:
            diffs.append(f"{ref}: in the schematic, missing on the PCB")
            continue
        fp = pcb[ref]
        if c["footprint"] and c["footprint"] != fp["name"]:
            diffs.append(f"{ref}: footprint {fp['name']} on PCB vs {c['footprint']} in schematic")
        if not same_value(c["value"], fp["value"]):
            diffs.append(f"{ref}: value {fp['value'] or '(empty)'} on PCB vs {c['value'] or '(empty)'} in schematic")
        padnet = {}
        for p in fp["pads"]:
            padnet.setdefault(p["number"], p["net"])
        sym_pins = set(c["pins"])
        for pin in sym_pins:
            want = pin_net.get((ref, pin))
            if pin not in padnet:
                # a symbol drawing one contact as several pins on the same net (a 4-pin switch on a
                # 2-contact footprint) is fine when another pin of that net has a pad carrying it; a
                # pad that is not a symbol pin (a renumbered pad) does not count
                if want and not want.startswith("unconnected-") and any(
                        q != pin and q in padnet and pin_net.get((ref, q)) == want and padnet[q]
                        and norm_net(padnet[q]) == norm_net(want) for q in sym_pins):
                    continue
                pinmap.append(f"{ref}: symbol pin {pin} ({c['pins'][pin]['name'] or '-'}) has no pad in {fp['name']}")
                continue
            got = padnet.get(pin)
            if want is None:
                continue
            if got is None:
                if not want.startswith("unconnected-"):
                    diffs.append(f"{ref}.{pin}: pad has no net on the PCB (schematic: {want})")
            elif norm_net(got) != norm_net(want):
                # a name KiCad derived from a pin is not design intent: the same pins on both sides
                # are the same net (an 'unconnected-(J3-VBUS-PadA4)' of four stacked pins in the
                # schematic becomes 'Net-(J3-VBUS-PadA4)' once a track joins them on the PCB)
                if (got.startswith(AUTO_NET) or want.startswith(AUTO_NET)) and \
                        sch_members.get(norm_net(want)) == pcb_members.get(norm_net(got)):
                    continue
                diffs.append(f"{ref}.{pin}: net {got} on PCB vs {want} in schematic")
        extra = sorted({p["number"] for p in fp["pads"] if p["type"] != "np_thru_hole"} - sym_pins - NON_ELECTRICAL_PAD)
        unused = [n for n in extra if not padnet.get(n)]
        if unused and sym_pins:
            pinmap.append(f"{ref}: footprint pads {', '.join(unused)} have no symbol pin "
                          "(wrong footprint or pin numbering?)")
    for ref, fp in pcb.items():
        pcb_only = ("board_only" in fp["attr"] or fp["name"].split(":")[-1].startswith(PCB_ONLY)
                    or ("exclude_from_bom" in fp["attr"] and not any(p["net"] for p in fp["pads"])))
        if ref not in comps and not ref.startswith("#") and not pcb_only:
            diffs.append(f"{ref}: on the PCB, not in the schematic")
    return [
        Result("PCB-PARITY-001", FAIL if diffs else PASS, f"{label}: {count(len(diffs), 'schematic/PCB difference')}",
               violations=[{"key": vkey("parity", d), "text": d} for d in diffs],
               coverage=coverage("schematic components", n_comp)),
        Result("PCB-PINMAP-001", FAIL if any("has no pad" in p for p in pinmap) else WARN if pinmap else PASS,
               f"{label}: {count(len(pinmap), 'symbol-pin/footprint-pad mismatch')}",
               violations=[{"key": vkey("pinmap", p), "text": p} for p in pinmap],
               coverage=coverage("symbol pins", n_pins)),
    ]
