"""Schematic netlist via `kicad-cli sch export netlist --format kicadxml` (pin names and electrical
types included), cached per schematic content."""
import hashlib
import json
import math
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import KICAD_CLI


def _sch_signature(sch):
    h = hashlib.sha1()
    for f in sorted(Path(sch).parent.glob("*.kicad_sch")):
        h.update(f.read_bytes())
    return h.hexdigest()


def export_xml(sch, cache_dir=None):
    sig = _sch_signature(sch)
    cached = Path(cache_dir) / f"netlist_{Path(sch).stem}.xml" if cache_dir else None
    meta = cached.with_suffix(".sig") if cached else None
    if cached and cached.exists() and meta.exists() and meta.read_text() == sig:
        return cached.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        out = Path(td) / "net.xml"
        subprocess.run([KICAD_CLI, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(out),
                        str(sch)], capture_output=True, text=True, timeout=300)
        if not out.exists():
            raise RuntimeError("kicad-cli did not produce a netlist")
        txt = out.read_text(encoding="utf-8")
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(txt, encoding="utf-8")
        meta.write_text(sig)
    return txt


def norm_net(name):
    """KiCad escapes '/' as {slash} in some places but not others."""
    return (name or "").replace("{slash}", "/")


def parse(xml_text):
    root = ET.fromstring(xml_text)
    libparts = {}
    for lp in root.iter("libpart"):
        pins = {p.get("num"): {"name": p.get("name") or "", "type": p.get("type") or ""}
                for p in lp.iter("pin")}
        libparts[(lp.get("lib"), lp.get("part"))] = {"pins": pins, "description": lp.findtext("description") or ""}
    comps = {}
    for c in root.iter("comp"):
        ls = c.find("libsource")
        lib, part = (ls.get("lib"), ls.get("part")) if ls is not None else ("", "")
        fields = {f.get("name"): (f.text or "").strip() for f in c.iter("field")}
        props = {p.get("name"): p.get("value") for p in c.findall("property")}
        comps[c.get("ref")] = {
            "ref": c.get("ref"), "value": (c.findtext("value") or "").strip(),
            "footprint": (c.findtext("footprint") or "").strip(), "lib": lib, "part": part,
            "datasheet": fields.get("Datasheet") or "", "fields": fields,
            "dnp": "dnp" in props,
            "exclude_from_board": "exclude_from_board" in props,
            "description": (ls.get("description") if ls is not None else "") or "",
            "pins": dict(libparts.get((lib, part), {}).get("pins", {})),
        }
    nets = {}
    pin_net = {}
    for n in root.iter("net"):
        name = norm_net(n.get("name"))
        nodes = []
        for node in n.iter("node"):
            ref, pin = node.get("ref"), node.get("pin")
            nodes.append({"ref": ref, "pin": pin, "name": node.get("pinfunction") or "",
                          "type": node.get("pintype") or ""})
            pin_net[(ref, pin)] = name
            if ref in comps:
                p = comps[ref]["pins"].setdefault(pin, {"name": "", "type": ""})
                p["name"] = p["name"] or (node.get("pinfunction") or "")
                p["type"] = (node.get("pintype") or p["type"]).split("+")[0]
        nets[name] = nodes
    return {"components": comps, "nets": nets, "pin_net": pin_net}


def load(sch, cache_dir=None):
    return parse(export_xml(sch, cache_dir))


# ------------------------------------------------------------------ value helpers
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1}


def parse_value(text, unit=""):
    """'4k7' -> 4700, '100nF' -> 1e-7, '10uF 10V' -> 1e-5, '220' -> 220. None if unparseable."""
    s = (text or "").strip().split()[0] if text else ""
    s = s.replace(",", ".")
    s = re.sub(r"(ohms?|Ω|F|H)$", "", s, flags=re.I) if not unit else re.sub(unit + "$", "", s, flags=re.I)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([pnuµmkKMGR]?)(\d*)", s)
    if not m:
        m2 = re.fullmatch(r"(\d*)([pnuµmkKMGR])(\d+)", s)  # R47, 4k7 handled above; 'k47'
        if not m2:
            return None
        return float(f"0.{m2.group(3)}") * _SI[m2.group(2)]
    num, pre, frac = m.groups()
    v = float(num + ("." + frac if frac else ""))
    return v * _SI.get(pre, 1) if pre else v


def same_value(a, b):
    """Same component value written two ways: '4k7' == '4.7k' == '4700', '100n' == '100nF', '220' == '220Ω',
    '100nF/50V' == '100n 50V'. The first word is compared as a number when both sides parse; the rest ('10V',
    'X7R') as text, ignoring case and spacing. Values that are not numbers (part names) are compared as text."""
    wa, wb = re.split(r"[\s/]+", (a or "").strip()), re.split(r"[\s/]+", (b or "").strip())
    wa, wb = [w for w in wa if w], [w for w in wb if w]
    if not wa or not wb:
        return wa == wb
    va, vb = parse_value(wa[0]), parse_value(wb[0])
    if va is not None and vb is not None:
        first = math.isclose(va, vb, rel_tol=1e-9, abs_tol=1e-15)
    else:
        first = wa[0].casefold() == wb[0].casefold()
    return first and "".join(wa[1:]).casefold() == "".join(wb[1:]).casefold()


def rated_voltage(comp):
    """Voltage rating written in the value or in a field ('22uF 10V', Voltage=25V)."""
    for src in [comp["value"], comp["fields"].get("Voltage", ""), comp["fields"].get("Rating", "")]:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*V(?:DC|AC)?\b", src or "", re.I)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def dump(data, path):
    Path(path).write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
