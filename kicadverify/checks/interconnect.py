"""BRD-LINK-001: board-to-board connections declared in params.interconnects.

kicad-verify reviews one board. What it cannot see from that board alone is the board it plugs into,
and the classic errors live there: a mirrored connector, a +5 V pin meeting a 3.3 V rail, two outputs
driving each other, a signal that ends on an unconnected pin. Each declared link names a connector of
this board, the mating board (its .kicad_pro) and connector, and how the pins correspond:

    interconnects:
      - name: main to sensor board
        connector: J3
        mate: {board: ../sensor/sensor.kicad_pro, connector: P1}
        mapping: straight          # straight (1-1, 2-2...), reverse (1-N, 2-N-1...) or {1: 2, 2: 1, ...}

Every mated pin pair is compared on the two schematics (the design intent; PCB-PARITY-001 ties each
board to its schematic):
  - both nets have a known voltage (rail name, net_voltages of each board, fixed regulator output)
    and they differ: FAIL (ground meeting a rail is the same case)
  - the nets have different roles - ground, supply or signal, known from the voltage or from the pins
    of the parts on them (a chip's GND/VSS pin, VCC/VDD pin, or data pins) - : FAIL; this is what
    catches a mirrored or shifted connector when no voltage is known
  - a rail meets a net driven by an output pin of the other board: FAIL
  - each side has an output (or power output) pin driving the net: FAIL
  - a pin used on one side mates an unconnected pin on the other: WARN
  - both nets are named and their names do not match (SDA against SCL), except a TX/RX crossing: WARN
  - the connectors have a different number of electrical pins under straight or reverse: FAIL

Net names are only a hint across boards (each designer names nets their own way); voltages and pin
types are what decide.
"""
import re
from pathlib import Path

from .. import config, netlist
from ..report import FAIL, PASS, WARN, Result, count, coverage, not_verifiable
from ..waivers import vkey
from .circuit import Circuit
from .parity import NON_ELECTRICAL_PAD

CID = "BRD-LINK-001"
DRIVERS = {"output", "power_out"}
CROSSED = [{"TX", "RX"}, {"TXD", "RXD"}, {"TX", "RXD"}, {"TXD", "RX"}]


MECHANICAL = re.compile(r"(MP|SH|S|EP|PAD|MH|MOUNT\w*|SHIELD\w*)\d*", re.I)


def _electrical(comp):
    """Pin numbers of a connector symbol that carry a signal: not mounting or shield pins, known by
    their number (MP, SH...) or by their name (pin 5 named Shield on a shielded 4-pin connector)."""
    def key(p):
        return (0, int(p), p) if p.isdigit() else (1, 0, p)
    return sorted((p for p, info in comp["pins"].items() if p not in NON_ELECTRICAL_PAD
                   and not MECHANICAL.fullmatch(p) and not MECHANICAL.fullmatch(info.get("name") or "")), key=key)


def _pairs(a_pins, b_pins, mapping):
    """[(this pin, mate pin)] or (None, reason)."""
    if isinstance(mapping, dict):
        return [(str(k), str(v)) for k, v in mapping.items()], None
    if len(a_pins) != len(b_pins):
        return None, f"{len(a_pins)} electrical pins against {len(b_pins)} on the mate"
    if mapping in (None, "straight"):
        return list(zip(a_pins, b_pins, strict=True)), None
    if mapping == "reverse":
        return list(zip(a_pins, reversed(b_pins), strict=True)), None
    return None, f"unknown mapping {mapping!r} (straight, reverse or a pin table)"


def _short(net):
    return (net or "").rsplit("/", 1)[-1].upper()


def _unconnected(net, nl, ref):
    """No net, an `unconnected-` net, or a net with no other pin than this connector's."""
    if not net or net.startswith("unconnected-"):
        return True
    return all(n["ref"] == ref for n in nl["nets"].get(net, []))


GROUND_PIN = re.compile(r"^(GND|VSS|AGND|DGND|PGND|SGND|GNDA|GNDD|VSSA|VSSD|0V)\d*$", re.I)  # not V- or EP
SUPPLY_PIN = re.compile(r"^(VCC|VDD|VIN|VBAT|VBUS|VDDA|VDDIO|VCCA|VCCIO|AVDD|DVDD|V\+|V-|VEE|VNEG|VS|VM|PVIN|3V3|5V|"
                        r"VSYS)\w*$", re.I)
SIGNAL_TYPES = {"input", "output", "bidirectional", "tri_state", "open_collector", "open_emitter"}
# net names whose role is unambiguous, for boards whose nets carry only connector (passive) pins, as
# carrier boards for modules do; matched on the last path component, whole words
SIGNAL_NAME = re.compile(r"(^|[_\-.])(SDA\d*|SCL\d*|SD\d|SC\d|TXD?\d*|RXD?\d*|MOSI|MISO|SCK|SCLK|CS\d*|NCS|SS|"
                         r"D\d{1,2}|GPIO\d+|IO\d+|RST|RESET|NRST|INT\d*|IRQ|EN|PWM\d*|CLK|DATA|DQ|USB_D[PM+-])$",
                         re.I)
GROUND_NAME = re.compile(r"^(BAT[_\-]?NEG|BAT-|BATT-|VBAT-|GND\w*|\w*_GND)$", re.I)
SUPPLY_NAME = re.compile(r"^(VCC\w*|VDD\w*|VIN\w*|VBUS|VBAT\+?|VSYS|BAT[_\-]?POS|BAT\+|BATT\+|V_?SOURCE)$", re.I)


def _role(net, nl, ref, volts):
    """'ground', 'supply', 'signal' or None (unknown) for a net, from its voltage and from the pins of
    the other parts on it: a chip's power pin named GND/VSS makes a ground (/BATneg on the GND pins of
    the regulators is one), one named VCC/VDD/VIN a supply, and data pins without power pins a signal."""
    v = volts.get(net)
    if v is not None:
        return "ground" if abs(v) < 1e-9 else "supply"
    nodes = [n for n in nl["nets"].get(net, []) if n["ref"] != ref]
    power = [n for n in nodes if (n.get("type") or "").startswith("power")]
    names = [(n.get("name") or nl["components"].get(n["ref"], {}).get("pins", {}).get(n["pin"], {}).get("name") or "")
             for n in power]
    if any(GROUND_PIN.match(x) for x in names):
        return "ground"
    if any(SUPPLY_PIN.match(x) for x in names) or any((n.get("type") or "").startswith("power_out") for n in power):
        return "supply"  # a supply pin, or a regulator output driving the net
    if not power and any((n.get("type") or "").split("+")[0] in SIGNAL_TYPES for n in nodes):
        return "signal"
    short = (net or "").rsplit("/", 1)[-1]
    if GROUND_NAME.match(short):
        return "ground"
    if SUPPLY_NAME.match(short):
        return "supply"
    if SIGNAL_NAME.search(short):
        return "signal"
    return None


def _driven(net, nl, ref, kinds=DRIVERS):
    return any(n["ref"] != ref and (n.get("type") or "").split("+")[0] in kinds for n in nl["nets"].get(net, []))


def _auto(net):
    return _short(net).startswith(("NET-(", "UNCONNECTED-("))


def _names_differ(a, b):
    sa, sb = _short(a), _short(b)
    if sa == sb or _auto(a) or _auto(b):
        return False
    ta, tb = re.split(r"[_\-.]", sa)[-1], re.split(r"[_\-.]", sb)[-1]  # I2C_SDA against SDA
    return ta != tb and {ta, tb} not in CROSSED


def _mate(link, root, cache_dir):
    """(netlist, params, label) of the mating board, or (None, None, reason)."""
    mate = link.get("mate") or {}
    pro = (Path(root) / str(mate.get("board") or "")).resolve()
    if not mate.get("board") or pro.suffix != ".kicad_pro" or not pro.is_file():
        return None, None, f"mate board {mate.get('board')!r} is not an existing .kicad_pro"
    sch = pro.with_suffix(".kicad_sch")
    if not sch.is_file():
        return None, None, f"no schematic {sch.name} next to the mate board"
    try:
        nl = netlist.load(sch, Path(cache_dir) / "mates" / pro.stem)
    except Exception as e:  # kicad-cli missing or the export failed
        return None, None, f"netlist of {pro.name} not exported: {e}"
    own = config.load_yaml(pro.parent / config.DIRNAME / "requirements.yaml", {}) or {}
    return nl, own.get("params") or {}, pro.stem


def run(nl, root, label, params, cache_dir):
    links = params.get("interconnects") or []
    if not links:
        return []
    if nl is None:
        return [not_verifiable(CID, f"{label}: no schematic netlist")]
    here = Circuit(nl, params)
    bad, warn, gaps, pairs_total = [], [], [], 0
    for i, link in enumerate(links):
        name = link.get("name") or f"link {i + 1}"
        ref = str(link.get("connector") or "")
        mate_ref = str((link.get("mate") or {}).get("connector") or "")
        if ref not in nl["components"]:
            gaps.append({"key": vkey(CID, name, "ref"), "text": f"{name}: no connector {ref!r} on this board"})
            continue
        mnl, mparams, mlabel = _mate(link, root, cache_dir)
        if mnl is None:
            gaps.append({"key": vkey(CID, name, "mate"), "text": f"{name}: {mlabel}"})
            continue
        if mate_ref not in mnl["components"]:
            gaps.append({"key": vkey(CID, name, "mref"),
                         "text": f"{name}: no connector {mate_ref!r} on {mlabel}"})
            continue
        there = Circuit(mnl, mparams)
        a_pins, b_pins = _electrical(nl["components"][ref]), _electrical(mnl["components"][mate_ref])
        pairs, why = _pairs(a_pins, b_pins, link.get("mapping"))
        if pairs is None:
            bad.append({"key": vkey(CID, name, "pins"), "text": f"{name}: {ref} and {mlabel}:{mate_ref}: {why}"})
            continue
        missing = [f"{ref}.{a}" for a, _ in pairs if a not in a_pins] + \
            [f"{mlabel}:{mate_ref}.{b}" for _, b in pairs if b not in b_pins]
        if missing:  # a mistyped pin in a mapping table compares nothing: say so
            bad.append({"key": vkey(CID, name, "map"),
                        "text": f"{name}: the pin mapping names pins the connectors do not have: {', '.join(missing)}"})
            pairs = [(a, b) for a, b in pairs if a in a_pins and b in b_pins]
        for a, b in pairs:
            pairs_total += 1
            na, nb = nl["pin_net"].get((ref, a)), mnl["pin_net"].get((mate_ref, b))
            where = f"{name}: {ref}.{a} ({na or 'no net'}) mates {mlabel}:{mate_ref}.{b} ({nb or 'no net'})"
            ua, ub = _unconnected(na, nl, ref), _unconnected(nb, mnl, mate_ref)
            if ua and ub:
                continue
            if ua != ub:
                side = "this pin is" if ua else "the mating pin is"
                warn.append({"key": vkey(CID, name, a, "open"), "text": f"{where}: {side} not connected"})
                continue
            va, vb = here.v.get(na), there.v.get(nb)
            da, db = _driven(na, nl, ref), _driven(nb, mnl, mate_ref)
            # a rail fed by the other board's regulator (power output) is how boards share supplies:
            # only a logic output against a rail is an error
            sa, sb = _driven(na, nl, ref, {"output"}), _driven(nb, mnl, mate_ref, {"output"})
            ra, rb = _role(na, nl, ref, here.v), _role(nb, mnl, mate_ref, there.v)
            if va is not None and vb is not None and abs(va - vb) > 0.05:
                bad.append({"key": vkey(CID, name, a, "v"), "text": f"{where}: {va:g} V against {vb:g} V"})
            elif (va is not None and vb is None and sb) or (vb is not None and va is None and sa):
                rail = f"{va:g} V" if va is not None else f"{vb:g} V"
                bad.append({"key": vkey(CID, name, a, "rail"),
                            "text": f"{where}: a {rail} rail against a net driven by an output pin"})
            elif ra and rb and ra != rb:  # ground against a signal or a rail: a mirrored or shifted connector
                bad.append({"key": vkey(CID, name, a, "role"), "text": f"{where}: a {ra} against a {rb}"})
            elif da and db:
                bad.append({"key": vkey(CID, name, a, "drive"),
                            "text": f"{where}: an output pin drives the net on each board"})
            elif ra == rb and ra in ("ground", "supply") or (va is not None and vb is not None):
                continue  # grounds or rails named differently (GND and /BATneg, +3V3 fed from /VREG)
            elif _names_differ(na, nb):
                warn.append({"key": vkey(CID, name, a, "name"), "text": f"{where}: net names differ"})
    st = FAIL if bad else WARN if warn else PASS
    return [Result(CID, st, f"{label}: {count(len(bad), 'connection error')}, {count(len(warn), 'warning')} "
                   f"over {count(len(links), 'declared link')}", violations=bad + warn,
                   coverage=coverage("mated pin pairs", pairs_total + len(gaps), gaps))]
