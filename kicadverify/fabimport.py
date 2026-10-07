"""Fab capability profiles from the design rules a fab publishes for KiCad.

Many fabs publish their limits as a KiCad project template or a custom-rules file. Reading those is
better than any built-in table kept by hand: the values are the fab's own, for the process the user
orders. `kicadverify profiles --from FILE` turns one into a profile for params.fab.profile.

Accepted inputs:
  .kicad_pro   KiCad 6+ project: Board Setup > Constraints (board.design_settings.rules)
  .kicad_pcb   KiCad 5 board (most published templates): setup minima and the smallest net-class
               clearance (templates put the fab minimum in a net class)
  .kicad_dru   custom rules: constraints of rules without a condition (they apply to everything)

A limit the file does not state is left out, so FAB-DFM-001 reports it as unchecked instead of
assuming it. Values of 0 mean "not set" in KiCad and are ignored.
"""
import json
import re
from pathlib import Path

from . import sexp

PRO_RULES = {  # KiCad 6+ Board Setup > Constraints -> profile key(s)
    "min_clearance": ["min_spacing_mm"],
    "min_track_width": ["min_track_mm"],
    "min_via_diameter": ["min_via_diameter_mm"],
    "min_through_hole_diameter": ["min_via_drill_mm", "min_pth_drill_mm"],
    "min_via_annular_width": ["min_via_annular_mm"],
    "min_hole_to_hole": ["min_hole_to_hole_mm"],
    "min_copper_edge_clearance": ["min_copper_to_edge_mm"],
}
DRU_RULES = {  # custom-rule constraint -> profile key(s)
    "clearance": ["min_spacing_mm"],
    "track_width": ["min_track_mm"],
    "via_diameter": ["min_via_diameter_mm"],
    "hole_size": ["min_via_drill_mm", "min_pth_drill_mm"],
    "annular_width": ["min_via_annular_mm", "min_pth_annular_mm"],
    "hole_to_hole": ["min_hole_to_hole_mm"],
    "edge_clearance": ["min_copper_to_edge_mm"],
}


def _put(out, keys, value, origin, notes):
    if value is None or value <= 0:
        return
    for k in keys:
        if k not in out or value < out[k]:
            out[k] = round(value, 4)
            notes[k] = origin


def _from_pro(path, out, notes):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rules = ((data.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    for key, targets in PRO_RULES.items():
        _put(out, targets, rules.get(key), f"Board Setup > Constraints: {key}", notes)
    dru = Path(path).with_suffix(".kicad_dru")
    if dru.exists():
        _from_dru(dru, out, notes)


KICAD6_PCB = 20211014  # from this file version on, the board rules live in the .kicad_pro


def _from_pcb(path, out, notes):
    tree = sexp.parse(Path(path).read_text(encoding="utf-8"))
    version = sexp.num((sexp.child(tree, "version") or ["", "0"])[1], 0)
    if version >= KICAD6_PCB:
        pro = Path(path).with_suffix(".kicad_pro")
        if not pro.is_file():
            raise ValueError(f"{Path(path).name} is a KiCad 6+ board: its rules are in {pro.name}, which is "
                             "not next to it; pass the .kicad_pro")
        return _from_pro(pro, out, notes)
    setup = sexp.child(tree, "setup") or []

    def num(name):
        c = sexp.child(setup, name)
        return sexp.num(c[1]) if c and len(c) > 1 else None
    _put(out, ["min_track_mm"], num("trace_min"), "setup trace_min", notes)
    _put(out, ["min_via_diameter_mm"], num("via_min_size"), "setup via_min_size", notes)
    _put(out, ["min_via_drill_mm"], num("via_min_drill"), "setup via_min_drill", notes)
    _put(out, ["min_hole_to_hole_mm"], num("hole_to_hole_min"), "setup hole_to_hole_min", notes)
    _put(out, ["min_copper_to_edge_mm"], num("edge_clearance"), "setup edge_clearance", notes)
    size, drill = num("via_min_size"), num("via_min_drill")
    if size and drill and size > drill:
        _put(out, ["min_via_annular_mm"], (size - drill) / 2, "derived: (via_min_size - via_min_drill) / 2", notes)
    clearances = [sexp.num(c[1]) for nc in sexp.children(tree, "net_class")
                  for c in [sexp.child(nc, "clearance")] if c]
    _put(out, ["min_spacing_mm"], min(clearances) if clearances else None,
         "smallest net-class clearance", notes)
    # copper layers by type: templates rename them ("Front", "Back")
    layers = [ly for ly in (sexp.child(tree, "layers") or [])[1:]
              if isinstance(ly, list) and len(ly) > 2 and ly[2] in ("signal", "power", "mixed", "jumper")]
    if layers:
        out["layers_max"] = len(layers)
        notes["layers_max"] = "copper layers of the template"


def _from_dru(path, out, notes):
    text = Path(path).read_text(encoding="utf-8")
    for block in re.split(r"(?=\(rule\s)", text):
        if not block.lstrip().startswith("(rule") or "(condition" in block:
            continue  # a conditional rule applies to some items only, not a fab-wide minimum
        name = (re.match(r"\(rule\s+\"?([^\"\n)]*)", block.lstrip()) or [None, "?"])[1]
        for m in re.finditer(r"\(constraint\s+(\w+)\s+\(min\s+([\d.]+)\s*(mm|mil)?\)", block):
            targets = DRU_RULES.get(m.group(1))
            if targets:
                v = float(m.group(2)) * (0.0254 if m.group(3) == "mil" else 1)
                _put(out, targets, v, f"custom rule '{name.strip()}': {m.group(1)}", notes)


def profile_from(path, name=None):
    """(profile dict for params.fab.profile, {key: where it came from})."""
    path = Path(path)
    out, notes = {}, {}
    {".kicad_pro": _from_pro, ".kicad_pcb": _from_pcb, ".kicad_dru": _from_dru}[path.suffix.lower()](
        path, out, notes)
    prof = {"name": name or path.stem,
            "source": {"kind": "fab_capability", "ref": f"design rules in {path.name}", "confirmed": False}}
    prof.update(dict(sorted(out.items())))
    return prof, notes
