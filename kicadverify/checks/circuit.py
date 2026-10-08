"""Deterministic circuit checks on the schematic netlist (kicad-cli export, independent of the
kicad-happy parser). They cover gaps measured on seeded errors that kicad-happy v2.3.1 does not flag:
reversed LEDs and diodes, fixed-regulator output vs rail, LED and BJT base current, floating
enable/reset pins.

Net voltages come from net names (+3V3, 5V, VBUS, GND...), fixed-regulator outputs and the
`net_voltages` parameter. Anything that cannot be resolved is left unknown and never guessed.
"""
import re

from ..netlist import parse_value
from ..report import FAIL, PASS, WARN, Result, count, coverage, not_verifiable
from ..waivers import vkey

GND_RE = re.compile(r"^(/)?(A|D|P|S|C)?GND[A-Z0-9_]*$|^(/)?VSS[A-Z]?$|^(/)?0V$|^(/)?EARTH$", re.I)
NAMED_V = {"VBUS": 5.0, "USB_VBUS": 5.0, "VUSB": 5.0}
CONTROL_PIN = re.compile(r"^(~\{)?(N?RST|RESET|NRESET|N?EN|ENABLE|CHIP_?PU|CE|SHDN|~SHDN|N?MCLR|BOOT0?|PWRKEY)\}?$",
                         re.I)
VF = [(re.compile(r"\b(ir|infrared)\b", re.I), 1.2), (re.compile(r"red|rojo|amber|orange|yellow|amarillo", re.I), 2.0),
      (re.compile(r"blue|azul|white|blanco|uv|violet|pure ?green|verde puro", re.I), 3.0),
      (re.compile(r"green|verde", re.I), 2.2)]


def net_voltage_from_name(name):
    n = (name or "").split("/")[-1].strip()
    if not n:
        return None
    if GND_RE.match(n):
        return 0.0
    u = n.upper()
    for k, v in NAMED_V.items():
        if u == k or u.endswith("_" + k):
            return v
    # a minus sign right before the number is a negative rail: -12V, -3V3, V-12V (it was read as +12 V)
    m = re.search(r"(?:^|[_/])(?:VCC|VDD|VIN|V)_?(-?)(\d+)V(\d+)?$", u)  # VCC_3V3, V-12V, VDD_-5V
    if m:
        return float(m.group(1) + m.group(2) + ("." + m.group(3) if m.group(3) else ""))
    m = re.search(r"(?<![A-Z0-9])([+-]?)(\d+)V(\d+)(?![0-9])", u)          # 3V3, 1V8, +12V0, -3V3
    if m:
        return float(f"{m.group(1) if m.group(1) == '-' else ''}{m.group(2)}.{m.group(3)}")
    m = re.search(r"(?<![A-Z0-9.])([+-]?)(\d+(?:\.\d+)?)V(?![A-Z0-9])", u)  # +5V, 12V, 3.3V, -12V
    if m:
        return float(("-" if m.group(1) == "-" else "") + m.group(2))
    return None


REG_PATTERNS = [
    re.compile(r"(?:AMS|LM|LD|AP|TLV|MIC|NCP|SPX|RT|ME|HT|XC|LP|MCP|TPS|L)\w*?[-_](\d{1,2})\.(\d{1,2})\b", re.I),
    re.compile(r"(?:AMS|LM|LD|AP|TLV|MIC|NCP|SPX|RT|ME|LP|TPS)\w*?[-_](\d{1,2})(\d)\b", re.I),  # -33 -> 3.3, -50 -> 5.0
    re.compile(r"^(?:L|LM|MC)?78(?:L|M)?(\d{2})", re.I),                                      # 7805 -> 5
    re.compile(r"^HT7(\d)(\d)\d?", re.I),                                                     # HT7333 -> 3.3
    re.compile(r"^XC62\d{2}[A-Z](\d)(\d)\d", re.I),                                          # XC6206P332 -> 3.3
]
VOUT_PIN = re.compile(r"^(VO|VOUT|OUT|OUTPUT|V_?OUT)$", re.I)
VIN_PIN = re.compile(r"^(VI|VIN|IN|INPUT|V_?IN)$", re.I)


def fixed_regulator_vout(value):
    v = (value or "").strip()
    for i, pat in enumerate(REG_PATTERNS):
        m = pat.search(v)
        if not m:
            continue
        if i == 0:
            return float(f"{m.group(1)}.{m.group(2)}")
        if i == 1:
            return float(f"{m.group(1)}.{m.group(2)}")
        if i == 2:
            return float(m.group(1))
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


def _kind(c):
    lib, part, val, ref = c["lib"].lower(), c["part"].lower(), c["value"].lower(), c["ref"]
    if "led" in part or ref.startswith("LED") or (ref.startswith("D") and "led" in val):
        return "led"
    if any(k in part for k in ("zener", "tvs", "d_tvs", "esd")) or \
            any(k in val for k in ("zener", "tvs", "smaj", "smbj", "esd")):
        return "clamp"
    if ref.startswith("D") or part.startswith("d_") or part in ("d", "diode") or "diode" in lib:
        return "diode"
    if ref.startswith(("C", "CP")) and ("polar" in part or part in ("cp", "c_polarized", "cp_small") or "elec" in val):
        return "ecap"
    if ref.startswith("Q") and ("npn" in part or "npn" in val or "_bec" in part or "nbjt" in part):
        return "npn"
    if ref.startswith("R") and not ref.startswith("RV"):
        return "resistor"
    if ref.startswith("K") or "relay" in part:
        return "relay"
    if ref.startswith("L"):
        return "inductor"
    return "other"


class Circuit:
    def __init__(self, nl, params):
        self.c = nl["components"]
        self.nets = nl["nets"]
        self.pin_net = nl["pin_net"]
        self.kind = {r: _kind(c) for r, c in self.c.items()}
        self.v = {}
        overrides = params.get("net_voltages") or {}
        for n in self.nets:
            o = overrides.get(n, overrides.get(n.lstrip("/")))
            self.v[n] = float(o) if o is not None else net_voltage_from_name(n)
        self.reg_out = {}
        for r, c in self.c.items():
            vout = fixed_regulator_vout(c["value"])
            if vout is None:
                continue
            for num, p in c["pins"].items():
                if VOUT_PIN.match(p["name"] or ""):
                    n = self.pin_net.get((r, num))
                    if n:
                        self.reg_out[r] = (n, vout, num)
                        if self.v.get(n) is None:
                            self.v[n] = vout

    def pins(self, ref):
        return {num: (p["name"] or "", self.pin_net.get((ref, num))) for num, p in self.c[ref]["pins"].items()}

    def pin_by_name(self, ref, *names):
        for nm, net in self.pins(ref).values():
            if nm.upper() in names:
                return net
        return None

    def led_channels(self, ref):
        """[(colour, anode net, cathode net)] of an LED: one channel for A/K, one per colour for multi-colour
        packages with a common anode (A + RK/GK/BK) or cathode (RA/GA/BA + K). [] if the pins are not named."""
        an, ka = {}, {}
        for nm, net in self.pins(ref).values():
            m = LED_PIN.fullmatch(nm.upper())
            if m and net:
                col, side = m.group(1) or "", m.group(2)[0]
                (an if side in "A+" else ka)[col] = net
        if len(an) == 1 and len(ka) == 1:
            return [("", *an.values(), *ka.values())]
        if len(an) == 1 and "" in an:
            return [(col, an[""], net) for col, net in sorted(ka.items())]
        if len(ka) == 1 and "" in ka:
            return [(col, net, ka[""]) for col, net in sorted(an.items())]
        return [(col, an[col], ka[col]) for col in sorted(an) if col and col in ka] if set(an) == set(ka) else []

    def two_pins(self, ref):
        p = sorted(self.pins(ref).items(), key=lambda kv: kv[0])
        return (p[0][1][1], p[1][1][1]) if len(p) == 2 else (None, None)

    def others(self, net, exclude):
        return [n for n in self.nets.get(net, []) if n["ref"] != exclude]

    def through_resistor(self, net, exclude):
        """Nets reachable from `net` through exactly one series resistor: [(R ref, R ohms, far net)]."""
        out = []
        for node in self.others(net, exclude):
            if self.kind.get(node["ref"]) == "resistor":
                a, b = self.two_pins(node["ref"])
                far = b if a == net else a
                out.append((node["ref"], parse_value(self.c[node["ref"]]["value"]), far))
        return out

    def drive_level(self, net, exclude):
        """Best estimate of the voltage driving `net`: rail voltage, GND, or logic output."""
        if self.v.get(net) is not None:
            return self.v[net], "rail"
        for node in self.others(net, exclude):
            t = (node["type"] or "").lower()
            if t in ("output", "bidirectional", "tri_state") and self.kind.get(node["ref"]) == "other":
                return None, "logic"
            if self.kind.get(node["ref"]) == "npn" and (node["name"] or "").upper() == "C":
                return 0.2, "switch"
        return None, None


COLOUR = {"R": "red", "G": "green", "B": "blue", "W": "white", "Y": "yellow"}
LED_PIN = re.compile(r"([RGBWY])?[_-]?(A|K|ANODE|CATHODE|\+|-)")


def _vf(c, colour=""):
    txt = COLOUR.get(colour) or " ".join([c["value"], c["description"], c["fields"].get("Color", ""),
                                          c["fields"].get("Colour", "")])
    for pat, vf in VF:
        if pat.search(txt):
            return vf
    return 2.0


def run(nl, label, params):
    if nl is None:
        return [not_verifiable(c, f"{label}: no schematic netlist") for c in
                ("CIR-POL-001", "CIR-REG-001", "CIR-LED-001", "CIR-BJT-001", "CIR-FLOAT-001")], None
    cir = Circuit(nl, params)
    logic_v = float(params.get("logic_high_v", 3.3))
    led_max = float(params.get("led_max_ma", 20))
    led_min = float(params.get("led_min_ma", 0.3))
    gpio_max = float(params.get("gpio_max_ma", 12))
    res = []

    # ------------------------------------------------------------ polarity
    pol, pol_total, pol_gap = [], 0, []

    def side_v(net, ref):
        """Voltage of a pin's side: its net, or the far end of one series resistor; a logic output or a
        transistor switch counts as a known (non-negative) drive."""
        for n, excl in [(net, ref)] + [(f, rr) for rr, _, f in cir.through_resistor(net, ref)]:
            v, how = cir.drive_level(n, excl)
            if v is not None or how in ("logic", "switch"):
                return v if v is not None else logic_v
        return None

    for r, k in cir.kind.items():
        c = cir.c[r]
        if k in ("led", "diode", "clamp", "ecap"):
            pol_total += 1
        if k in ("led", "diode", "clamp"):
            chans = cir.led_channels(r) if k == "led" else \
                [("", cir.pin_by_name(r, "A", "ANODE", "+"), cir.pin_by_name(r, "K", "CATHODE", "-"))]
            if not chans or not all(a and kk for _, a, kk in chans):
                pol_gap.append({"key": vkey("polgap", r), "text": f"{r} ({c['value']}): pins not named A/K"})
                continue
            for ch, a, kk in chans:
                name = f"{r}.{ch}" if ch else r
                va, vk = cir.v.get(a), cir.v.get(kk)
                across = {n["ref"] for n in cir.others(a, r) if cir.kind.get(n["ref"]) in ("relay", "inductor")} & \
                    {n["ref"] for n in cir.others(kk, r)}
                if k == "led":
                    decided = side_v(a, r) is not None and side_v(kk, r) is not None
                elif k == "diode":
                    # a flyback across a coil with its cathode on a known rail is decided too
                    decided = va is not None or bool(across and vk is not None)
                else:
                    decided = va is not None and vk is not None
                if not decided:
                    pol_gap.append({"key": vkey("polgap", name),
                                    "text": f"{name} ({c['value']}): voltage of {a} / {kk} unknown "
                                            "(name the rail or set net_voltages)"})
                if k == "led":
                    # anode pulled to GND (directly or through its series resistor) => reversed / never lights
                    a_low = va == 0 or any(cir.v.get(f) == 0 for _, _, f in cir.through_resistor(a, r))
                    k_high = (vk or 0) > 0 or any((cir.v.get(f) or 0) > 0 for _, _, f in cir.through_resistor(kk, r))
                    if a_low or (k_high and (va == 0 or a_low)):
                        pol.append(f"{name} ({c['value']}): LED anode on {a} resolves to GND - reversed, "
                                   "it will never light")
                    elif va is not None and vk is not None and va < vk:
                        pol.append(f"{name} ({c['value']}): LED reverse-biased ({a}={va} V < {kk}={vk} V)")
                elif k == "diode":
                    if va and va > 0 and vk == 0:
                        pol.append(f"{name} ({c['value']}): anode on {a} ({va} V), cathode on GND - "
                                   "forward short across the supply")
                    coil = [n["ref"] for n in cir.others(a, r) if cir.kind.get(n["ref"]) in ("relay", "inductor")]
                    coil = [x for x in coil if x in {n["ref"] for n in cir.others(kk, r)}]
                    if coil and va and va > 0:
                        pol.append(f"{name} ({c['value']}): flyback diode across {coil[0]} is reversed "
                                   f"(anode on supply {a}); "
                                   "it shorts the supply when the switch turns on")
                elif k == "clamp":
                    if va and va > 0 and vk == 0:
                        pol.append(f"{name} ({c['value']}): clamp/zener forward-biased (anode {a}={va} V, cathode GND)")
        elif k == "ecap":
            pins = cir.pins(r)
            plus = next((net for num, (nm, net) in pins.items() if nm == "+" or (not nm and num == "1")), None)
            minus = next((net for num, (nm, net) in pins.items() if nm == "-" or (not nm and num == "2")), None)
            vp, vm = cir.v.get(plus), cir.v.get(minus)
            if vp is None or vm is None:
                pol_gap.append({"key": vkey("polgap", r),
                                "text": f"{r} ({c['value']}): voltage of {plus} / {minus} unknown"})
            if vp is not None and vm is not None and vp < vm:
                pol.append(f"{r} ({c['value']}): polarized capacitor reversed "
                           f"(+ on {plus}={vp} V, - on {minus}={vm} V)")
    res.append(Result("CIR-POL-001", FAIL if pol else PASS, f"{label}: {count(len(pol), 'polarity error')}",
                      violations=[{"key": vkey("pol", p.split(':')[0]), "text": p} for p in pol],
                      coverage=coverage("polarized parts", pol_total, pol_gap)))

    # ------------------------------------------------------------ fixed regulators vs rail
    reg, reg_gap = [], []
    for r, (net, vout, _) in cir.reg_out.items():
        named = net_voltage_from_name(net)
        over = (params.get("net_voltages") or {}).get(net)
        expected = float(over) if over is not None else named
        if expected is None:
            reg_gap.append({"key": vkey("reggap", r), "text": f"{r} ({cir.c[r]['value']}): rail {net} has no "
                                                              "voltage in its name or net_voltages"})
        if expected is not None and abs(expected - vout) > max(0.05 * expected, 0.1):
            reg.append(f"{r} ({cir.c[r]['value']}) outputs {vout} V on rail {net} ({expected} V)")
        vin_net = next((cir.pin_net.get((r, num)) for num, p in cir.c[r]["pins"].items()
                        if VIN_PIN.match(p["name"] or "")), None)
        vin = cir.v.get(vin_net) if vin_net else None
        if vin is not None and vin < vout + float(params.get("min_dropout_v", 0.3)):
            reg.append(f"{r} ({cir.c[r]['value']}): input {vin_net}={vin} V cannot regulate {vout} V (dropout)")
    res.append(Result("CIR-REG-001", FAIL if reg else PASS,
                      f"{label}: {count(len(reg), 'fixed-regulator output/input mismatch')} "
                      f"({count(len(cir.reg_out), 'regulator')} recognised)",
                      violations=[{"key": vkey("reg", x.split(' ')[0], x[-20:]), "text": x} for x in reg],
                      coverage=coverage("fixed regulators recognised", len(cir.reg_out), reg_gap)))

    # ------------------------------------------------------------ LED current
    led_issues, led_info, led_total, led_gap = [], [], 0, []
    for r, k in cir.kind.items():
        if k != "led":
            continue
        chans = cir.led_channels(r)
        led_total += len(chans) or 1
        if not chans:
            led_gap.append({"key": vkey("ledgap", r), "text": f"{r}: pins not named A/K"})
            continue
        for ch, a, kk in chans:
            name = f"{r}.{ch}" if ch else r
            vf = _vf(cir.c[r], ch)
            found = False
            for side, net, other in (("anode", a, kk), ("cathode", kk, a)):
                if cir.v.get(net) is not None:
                    continue  # pin straight on a rail: its other resistors (pull-ups...) are not in series
                for rref, ohms, far in cir.through_resistor(net, r):
                    if not ohms:
                        continue
                    if side == "anode":
                        vtop, how = cir.drive_level(far, rref)
                        vtop = logic_v if how == "logic" else vtop
                        vbot, how2 = cir.drive_level(other, r)
                        vbot = 0.0 if vbot is None and how2 in (None, "logic", "switch") else vbot
                    else:
                        vbot, how = cir.drive_level(far, rref)
                        vbot = 0.2 if how == "switch" else (0.0 if how == "logic" else vbot)
                        vtop, how2 = cir.drive_level(other, r)
                        vtop = logic_v if how2 == "logic" else vtop
                    if vtop is None or vbot is None:
                        continue
                    i_ma = (vtop - vbot - vf) / ohms * 1000
                    found = True
                    txt = f"{name}: {i_ma:.1f} mA through {rref} ({cir.c[rref]['value']}) from {vtop} V, Vf≈{vf} V"
                    led_info.append(txt)
                    if i_ma <= 0:
                        led_issues.append((WARN, f"{name}: no forward current through {rref} "
                                                 f"({vtop} V to {vbot} V, Vf≈{vf} V): not lit (see CIR-POL-001)"))
                    elif i_ma > led_max:
                        led_issues.append((FAIL, txt + f" > {led_max} mA"))
                    elif i_ma < led_min:
                        led_issues.append((WARN, txt + f" < {led_min} mA (too dim or not lit)"))
            near = cir.others(a, r) + cir.others(kk, r)
            if not found and not any(cir.kind.get(n["ref"]) == "resistor" for n in near):
                led_issues.append((WARN, f"{name}: no series resistor found (constant-current driver?)"))
            elif not found:
                led_gap.append({"key": vkey("ledgap", name), "text": f"{name}: current not computable (drive level or "
                                                                  "resistor value unknown)"})
    st = FAIL if any(s == FAIL for s, _ in led_issues) else WARN if led_issues else PASS
    res.append(Result("CIR-LED-001", st,
                      f"{label}: {count(len(led_issues), 'LED current problem')} "
                      f"({count(len(led_info), 'LED')} computed)",
                      violations=[{"key": vkey("led", t.split(':')[0], s), "text": t} for s, t in led_issues],
                      coverage=coverage("LEDs", led_total, led_gap)))

    # ------------------------------------------------------------ BJT base drive
    bjt, bjt_total, bjt_gap = [], 0, []
    for r, k in cir.kind.items():
        if k != "npn":
            continue
        bjt_total += 1
        b = cir.pin_by_name(r, "B")
        if not b:
            bjt_gap.append({"key": vkey("bjtgap", r), "text": f"{r}: no pin named B"})
            continue
        rs = cir.through_resistor(b, r)
        direct = [n for n in cir.others(b, r) if (n["type"] or "") in ("output", "bidirectional", "tri_state")
                  and cir.kind.get(n["ref"]) == "other"]
        if direct and not rs:
            drv = f"{direct[0]['ref']}.{direct[0]['name'] or direct[0]['pin']}"
            bjt.append(f"{r}: base driven directly by {drv} without a resistor")
        if not direct and not rs:
            bjt_gap.append({"key": vkey("bjtgap", r), "text": f"{r}: base drive not recognised"})
        for rref, ohms, far in rs:
            vdrv, how = cir.drive_level(far, rref)
            vdrv = logic_v if how == "logic" else vdrv
            if not ohms or vdrv is None:
                bjt_gap.append({"key": vkey("bjtgap", r), "text": f"{r}: base current through {rref} not "
                                                                        "computable (drive level or value unknown)"})
            if ohms and vdrv and vdrv > 0:
                ib = (vdrv - 0.7) / ohms * 1000
                if ib > gpio_max:
                    bjt.append(f"{r}: base current {ib:.1f} mA through {rref} ({cir.c[rref]['value']}) "
                               f"exceeds {gpio_max} mA")
    res.append(Result("CIR-BJT-001", FAIL if bjt else PASS,
                      f"{label}: {count(len(bjt), 'transistor base-drive problem')}",
                      violations=[{"key": vkey("bjt", x.split(':')[0], x[-25:]), "text": x} for x in bjt],
                      coverage=coverage("NPN transistors", bjt_total,
                                        list({g["text"].split(":")[0]: g for g in bjt_gap}.values()))))

    # ------------------------------------------------------------ floating control pins
    flo, ctl_total = [], 0
    for r, c in cir.c.items():
        if r.startswith("#") or cir.kind.get(r) != "other":
            continue
        for num, p in c["pins"].items():
            nm = (p["name"] or "").replace("~{", "").replace("}", "")
            if not CONTROL_PIN.match(nm) or (p["type"] or "") not in ("input", "bidirectional", "passive", ""):
                continue
            ctl_total += 1
            net = cir.pin_net.get((r, num))
            if net is None or net.startswith("unconnected-"):
                flo.append(f"{r}.{nm} (pin {num}) is not connected")
                continue
            if cir.v.get(net) is not None:
                continue
            others = cir.others(net, r)
            if not others:
                flo.append(f"{r}.{nm} on {net} has no other connection (floating)")
    res.append(Result("CIR-FLOAT-001", WARN if flo else PASS,
                      f"{label}: {count(len(flo), 'enable/reset pin')} floating",
                      violations=[{"key": vkey("float", x.split(' ')[0]), "text": x} for x in flo],
                      coverage=coverage("enable/reset/boot pins", ctl_total)))
    return res, cir
